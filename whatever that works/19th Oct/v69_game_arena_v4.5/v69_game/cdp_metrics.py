#!/usr/bin/env python3
"""
cdp_metrics.py - performance dashboard for the CDP link: reads the session logs the flowgraph writes (csma_*.csv,
phy_1s.csv, udp.csv, arq_tx.csv, msgs.csv, events.log - see LOGS.md) while they grow, and serves a live dashboard
page on this PC.

From the chat window: "📊 Performance dashboard" (cdp_daemon.py starts the server and opens the browser).
For an old session, or the other PC's logs:
    python3 cdp_metrics.py debug/<session>                   # live/replay server -> http://127.0.0.1:5311/
    python3 cdp_metrics.py debug/<session> --html report.html  # one self-contained page (send it, open anywhere)

The page (no internet needed, everything is inside it) shows:
  - status tiles for the last 10 s: link, throughput, channel busy, queueing delay, frame errors, datagram loss,
    Block ACK timeouts, PC load - each marked ok / warning / critical
  - charts over the last 1 / 5 / 15 min or the whole session, 1 s per point: throughput, frames, channel use,
    delays, errors, retries, queues, PC load, voice
  - the nodes heard (with their key fingerprints), the last reliable messages, the last events
"""
import argparse
import csv
import io
import json
import math
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

FILES = ("csma_1s.csv", "csma_txop.csv", "udp.csv", "phy_1s.csv", "arq_tx.csv", "arq_rx.csv", "msgs.csv")
KEEP_S = 4 * 3600                      # bins kept in memory


def _f(v, default=None):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _count_ranges(s):
    """'12-15 18' -> 5"""
    n = 0
    for part in (s or "").split():
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                n += int(b) - int(a) + 1
            except ValueError:
                pass
        elif part.strip():
            n += 1
    return n


def _pct(vals, q):
    if not vals:
        return None
    v = sorted(vals)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


class _Tail:
    """New complete lines of a growing CSV (restarts when the file is re-created)."""

    def __init__(self, path):
        self.path = path
        self.ino = None
        self.off = 0
        self.part = b""
        self.cols = None

    def read(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return []
        if st.st_ino != self.ino or st.st_size < self.off:
            self.ino, self.off, self.part, self.cols = st.st_ino, 0, b"", None
        if st.st_size == self.off:
            return []
        with open(self.path, "rb") as f:
            f.seek(self.off)
            data = f.read(8 << 20)
        self.off += len(data)
        data = self.part + data
        lines = data.split(b"\n")
        self.part = lines.pop()
        rows = []
        for row in csv.reader(io.StringIO(b"\n".join(lines).decode("utf-8", "replace"))):
            if not row:
                continue
            if self.cols is None:
                self.cols = row
                continue
            if len(row) > len(self.cols) and "windows" in self.cols:      # a JSON cell with commas in it
                i, extra = self.cols.index("windows"), len(row) - len(self.cols)
                row = row[:i] + [",".join(row[i:i + extra + 1])] + row[i + extra + 1:]
            rows.append(dict(zip(self.cols, row)))
        return rows


class Metrics:
    """Per-second bins built from the session logs; snapshot() -> what the dashboard draws."""

    LOGGER_FILES = ("csma_1s.csv", "phy_1s.csv", "csma_txop.csv", "udp.csv", "msgs.csv", "events.log")
    FRESH_S = 30                       # a folder whose logger files changed this recently is being written

    def __init__(self, session_dir, extra=None, node=None, search=None):
        self.home = Path(session_dir)      # where this session's logs should be (CDP_DEBUG_DIR of the flowgraph)
        self.dir = self.home               # where they are read from (another folder if the flowgraph writes there)
        self.extra = extra                 # callable -> dict (the daemon: nodes, voice, encryption)
        self.node = node                   # this PC's node: only a flowgraph of this node counts as "ours"
        roots = [self.home.parent, Path(os.path.expanduser("~/cdp_debug"))] if search is None else search
        self.search = [Path(r) for r in roots]
        self.lock = threading.Lock()
        self.started = time.time()
        self.app = {}                      # epoch second -> {"chat_tx": kB, ...} sampled from the daemon
        self._sampler = None
        self._next_search = 0.0
        self.note = ""
        self._reset()

    def _reset(self):
        self.tails = {n: _Tail(self.dir / n) for n in FILES}
        self.ev_off = 0
        self.bins = {}
        self.t0 = None
        self.fg = {}
        self.msgs = []
        self.events = []
        self.owd_min = {}
        self.t_last = 0.0
        self.files = {n: {"rows": 0, "last_t": None} for n in FILES}

    def _bin(self, t):
        s = int(t)
        b = self.bins.get(s)
        if b is None:
            b = self.bins[s] = {"wait": [], "att": [], "owd": [], "tx_bytes": 0, "tx_frames": 0, "air_ms": 0.0,
                                "timeouts": 0, "retx": 0, "acks_sent": 0, "u_tx": 0, "u_tx_b": 0, "u_rx": 0,
                                "u_rx_b": 0, "u_gap": 0, "noba": 0, "dropped": 0, "udp_drop": 0, "rel_rx": 0}
        if t > self.t_last:
            self.t_last = t
        return b

    def update(self):
        with self.lock:
            self._update()

    def _rows(self, name):
        rows = self.tails[name].read()
        if rows:
            f = self.files[name]
            f["rows"] += len(rows)
            t = _f(rows[-1].get("t"))
            if t is not None:
                f["last_t"] = t
        return rows

    # ---------------------------------------------------------------- which folder the flowgraph writes to
    def _fresh(self, d, now):
        """-> newest mtime of the logger files in d if one changed in the last FRESH_S s, else None."""
        best = None
        for n in self.LOGGER_FILES:
            try:
                m = (d / n).stat().st_mtime
            except OSError:
                continue
            best = m if best is None else max(best, m)
        return best if best is not None and now - best <= self.FRESH_S else None

    def _node_of(self, d):
        try:
            return json.loads((d / "flowgraph.json").read_text()).get("node")
        except Exception:
            return None

    def _locate(self):
        """Follow the flowgraph's logs: this session's folder if they are written there, else the folder (under
        debug/ or ~/cdp_debug) where a flowgraph of this node writes right now - e.g. one started before this
        chat window, which keeps writing into its own session's folder."""
        now = time.time()
        if now < self._next_search:
            return
        self._next_search = now + 3.0
        if self._fresh(self.home, now) is not None:
            target = self.home
        elif self.dir != self.home and self._fresh(self.dir, now) is not None:
            return                                                     # keep following the folder found earlier
        else:
            target, best = None, None
            for root in self.search:
                try:
                    subs = [p for p in root.iterdir() if p.is_dir()]
                except OSError:
                    continue
                for d in subs:
                    m = self._fresh(d, now)
                    if m is None:
                        continue
                    if self.node is not None and self._node_of(d) not in (None, self.node):
                        continue
                    if best is None or m > best:
                        target, best = d, m
            if target is None:
                if self.dir != self.home and not self.bins:
                    target = self.home
                else:
                    self._set_note(now)
                    return
        if target != self.dir:
            self.dir = target
            self._reset()
        self._set_note(now)

    def _set_note(self, now):
        if self.dir != self.home:
            self.note = (f"The flowgraph writes its logs to {self.dir}, not to this chat session's folder "
                         f"({self.home.name}): it was started before this chat window (an older GRC / flowgraph still "
                         f"running). The dashboard follows it. To get both in one folder: close GRC, then press "
                         f"▶ Run in the GRC window the chat app opens.")
        elif self._fresh(self.home, now) is None and not self.bins:
            self.note = ("No flowgraph on this PC is writing performance logs right now (looked in this session's "
                         "folder, debug/ and ~/cdp_debug). Is the flowgraph running? Its console should print "
                         "'[LOG] performance logs -> <folder>'. The chat app's own traffic is still shown below.")
        else:
            self.note = ""

    def _update(self):
        self._locate()
        try:
            fg = json.loads((self.dir / "flowgraph.json").read_text())
        except Exception:
            fg = None
        if fg and self.t0 is not None and fg.get("t0") != self.t0:
            self._reset()                                     # the flowgraph was restarted: new time base
        if fg:
            self.fg, self.t0 = fg, fg.get("t0")
        for row in self._rows("csma_1s.csv"):
            t = _f(row.get("t"))
            if t is None:
                continue
            b = self._bin(t)
            for k in ("busy_pct", "tx_pct", "queue", "udp_q", "cw", "rate_pct", "proc_cpu", "ba_timeout",
                      "retry_drop", "tx_D", "tx_BA", "rx_data", "rx_dup", "floor_db", "deferrals", "load1"):
                v = _f(row.get(k))
                if v is not None:
                    b["m_" + k] = v
        for row in self._rows("csma_txop.csv"):
            t = _f(row.get("t"))
            if t is None:
                continue
            b = self._bin(t)
            kind = row.get("kind", "")
            if kind == "A":
                b["acks_sent"] += 1
                continue
            b["tx_bytes"] += int(_f(row.get("bytes"), 0))
            b["tx_frames"] += int(_f(row.get("n"), 0))
            b["air_ms"] += _f(row.get("air_ms"), 0.0)
            b["retx"] += int(_f(row.get("retx"), 0))
            for k, dst in (("wait_ms", "wait"), ("att_ms", "att")):
                v = _f(row.get(k))
                if v is not None:
                    b[dst].append(v)
            if row.get("res") == "timeout":
                b["timeouts"] += 1
        for row in self._rows("udp.csv"):
            t = _f(row.get("t"))
            if t is None:
                continue
            b = self._bin(t)
            nb = int(_f(row.get("bytes"), 0))
            if row.get("dir") == "tx":
                b["u_tx"] += 1
                b["u_tx_b"] += nb
            else:
                b["u_rx"] += 1
                b["u_rx_b"] += nb
                g = _f(row.get("gap"), 0)
                if g and g > 0:
                    b["u_gap"] += int(min(g, 1000))
                owd = _f(row.get("owd_ms"))
                if owd is not None:
                    peer = row.get("peer")
                    m = self.owd_min.get(peer)
                    self.owd_min[peer] = owd if m is None else min(m, owd)
                    b["owd"].append((peer, owd))
        for row in self._rows("phy_1s.csv"):
            t = _f(row.get("t"))
            if t is None:
                continue
            b = self._bin(t)
            for k in ("ok", "crc_fail", "locks", "noise_db", "own", "other"):
                v = _f(row.get(k))
                if v is not None:
                    b["p_" + k] = v
        for row in self._rows("arq_tx.csv"):
            t = _f(row.get("t"))
            if t is None:
                continue
            b = self._bin(t)
            if row.get("what") == "noba":
                b["noba"] += 1
            b["dropped"] += _count_ranges(row.get("dropped"))
        for row in self._rows("arq_rx.csv"):
            t = _f(row.get("t"))
            if t is None:
                continue
            if str(row.get("dup", "")).strip() not in ("1", "True", "true"):
                self._bin(t)["rel_rx"] += 1
        for row in self._rows("msgs.csv"):
            self.msgs.append({k: row.get(k) for k in ("t", "dir", "peer", "bytes", "segs", "dur_s", "kBps", "res",
                                                      "frames_tx", "resent")})
            self.msgs = self.msgs[-40:]
        self._read_events()
        if self.bins:
            low = self.t_last - KEEP_S
            for s in [s for s in self.bins if s < low]:
                del self.bins[s]

    def _read_events(self):
        p = self.dir / "events.log"
        try:
            size = p.stat().st_size
        except OSError:
            return
        if size < self.ev_off:
            self.ev_off = 0
        if size == self.ev_off:
            return
        with open(p, "rb") as f:
            f.seek(self.ev_off)
            data = f.read(1 << 20)
        cut = data.rfind(b"\n") + 1
        self.ev_off += cut
        for line in data[:cut].decode("utf-8", "replace").splitlines():
            parts = line.split(None, 3)
            if len(parts) >= 3:
                t = _f(parts[0])
                if t is not None and parts[2] == "udp_drop":
                    self._bin(t)["udp_drop"] += 1
                self.events.append(line[:200])
        self.events = self.events[-40:]

    APP_KEYS = ("chat_tx", "chat_rx", "voice_tx", "voice_rx")

    def start_sampler(self, counters, period=1.0):
        """counters() -> {"chat_tx": bytes, "chat_rx": bytes, "voice_tx": bytes, "voice_rx": bytes} (running totals,
        from the daemon). Sampled once a second, so the app's own traffic is charted even without flowgraph logs."""
        stop = threading.Event()

        def run():
            prev = None
            nxt = time.time()
            while not stop.is_set():
                nxt += period
                try:
                    c = counters() or {}
                except Exception:
                    c = None
                now = time.time()
                if c is not None:
                    if prev is not None:
                        d = {k: max(0.0, (c.get(k, 0) - prev.get(k, 0)) / 1000.0 / max(1e-3, period)) for k in self.APP_KEYS}
                        with self.lock:
                            self.app[int(now) - 1] = d
                            if len(self.app) > KEEP_S + 120:
                                for k in sorted(self.app)[:len(self.app) - KEEP_S]:
                                    del self.app[k]
                    prev = c
                stop.wait(max(0.05, nxt - time.time()))
        self._sampler = stop
        threading.Thread(target=run, name="dash-sampler", daemon=True).start()

    def stop(self):
        if self._sampler is not None:
            self._sampler.set()

    close = stop

    def note_voice(self, tx_bytes, rx_bytes):
        """(old interface) voice bytes of the last second."""
        with self.lock:
            cur = self.app.setdefault(int(time.time()) - 1, {k: 0.0 for k in self.APP_KEYS})
            cur["voice_tx"], cur["voice_rx"] = tx_bytes / 1000.0, rx_bytes / 1000.0

    @property
    def base(self):
        """epoch of t = 0 on the charts: the flowgraph's t0, or (no logs yet) when this dashboard started"""
        return self.t0 if self.t0 else self.started

    def _app_at(self, sec):
        a = self.app.get(int(self.base + sec))
        if a is None:
            return {k: None for k in self.APP_KEYS}
        return {k: round(a.get(k, 0.0), 3) for k in self.APP_KEYS}

    # ---------------------------------------------------------------- what the page gets
    def _row(self, s, b):
        def m(k):
            return b.get("m_" + k)
        ok, bad = b.get("p_ok"), b.get("p_crc_fail")
        crc = None if ok is None or bad is None or ok + bad == 0 else 100.0 * bad / (ok + bad)
        rx, gap = b["u_rx"], b["u_gap"]
        loss = 100.0 * gap / (rx + gap) if rx + gap > 0 else None
        jit = [owd - self.owd_min.get(peer, owd) for peer, owd in b["owd"]]
        return {
            "t": s,
            "tx_kBps": round(b["tx_bytes"] / 1000.0, 3),
            "udp_rx_kBps": round(b["u_rx_b"] / 1000.0, 3),
            "tx_frames": m("tx_D") if m("tx_D") is not None else b["tx_frames"],
            "rx_frames": m("rx_data"),
            "phy_ok": ok,
            "busy_pct": m("busy_pct"),
            "tx_pct": m("tx_pct"),
            "wait_p95": _pct(b["wait"], 0.95),
            "att_p50": _pct(b["att"], 0.5),
            "jitter_p95": _pct(jit, 0.95),
            "crc_pct": crc,
            "loss_pct": loss,
            "ba_timeouts": m("ba_timeout") if m("ba_timeout") is not None else b["timeouts"],
            "retry_drops": m("retry_drop") if m("retry_drop") is not None else b["dropped"],
            "udp_drops": b["udp_drop"],
            "queue": m("queue"),
            "udp_q": m("udp_q"),
            "cw": m("cw"),
            "cpu": m("proc_cpu"),
            "rate_pct": m("rate_pct"),
            "noise_db": b.get("p_noise_db"),
            "rx_rel": b.get("rel_rx", 0),
            **self._app_at(s),
        }

    GAUGES = ("busy_pct", "tx_pct", "queue", "udp_q", "cw", "cpu", "rate_pct", "noise_db", "rx_frames", "phy_ok")
    COUNTS = ("tx_kBps", "udp_rx_kBps", "tx_frames", "ba_timeouts", "retry_drops", "udp_drops", "rx_rel")
    HOLD_S = 11                         # the logger writes its per-second files only every 10 s while idle

    def _series(self, lo, hi, alive=False):
        """One row per second from lo to hi (wall clock): seconds without log rows are filled - counts with 0 while
        the flowgraph was logging, gauges with their last value (for up to HOLD_S s) - so the charts move on
        smoothly when the link is idle and stop (gap) when the flowgraph is not running."""
        keys = sorted(k for k in self.bins if lo - self.HOLD_S <= k <= hi)
        idx = 0
        last = {}
        # gauges known just before the window
        for k in keys:
            if k >= lo:
                break
            idx += 1
            r = self._row(k, self.bins[k])
            for g in self.GAUGES:
                if r[g] is not None:
                    last[g] = (k, r[g])
        alive_until = max((k for k in keys if k < lo), default=-10**9)
        if alive_until < -10**8 and any(k < lo for k in self.bins):
            alive_until = max(k for k in self.bins if k < lo) if len(self.bins) < 50000 else lo - self.HOLD_S - 1
        out = []
        for sec in range(lo, hi + 1):
            if idx < len(keys) and keys[idx] == sec:
                r = self._row(sec, self.bins[sec])
                idx += 1
                alive_until = sec
                for g in self.GAUGES:
                    if r[g] is not None:
                        last[g] = (sec, r[g])
                    elif g in last and sec - last[g][0] <= self.HOLD_S:
                        r[g] = last[g][1]
            else:
                r = {"t": sec}
                logging = sec - alive_until <= self.HOLD_S or any(sec - v[0] <= self.HOLD_S for v in last.values())
                for k in self.COUNTS:
                    r[k] = 0 if (logging or alive) and self.t0 is not None and (sec <= self.t_last + 2 or alive) else None
                for g in self.GAUGES:
                    r[g] = last[g][1] if g in last and sec - last[g][0] <= self.HOLD_S else None
                r.update(self._app_at(sec))
            out.append(r)
        return out

    @staticmethod
    def _downsample(rows, max_points=900):
        if len(rows) <= max_points:
            return rows
        k = -(-len(rows) // max_points)
        out = []
        peak = ("wait_p95", "jitter_p95", "queue", "udp_q", "cpu")
        for i in range(0, len(rows), k):
            grp = rows[i:i + k]
            r = {"t": grp[-1]["t"]}
            for key in {k2 for g in grp for k2 in g}:
                if key == "t":
                    continue
                vals = [g[key] for g in grp if g.get(key) is not None]
                if not vals:
                    r[key] = None
                elif key in peak:
                    r[key] = max(vals)
                elif key == "rate_pct":
                    r[key] = min(vals)
                else:
                    r[key] = sum(vals) / len(vals)
            out.append(r)
        return out

    def snapshot(self, window=300.0):
        self.update()
        with self.lock:
            now_rel = time.time() - self.base
            age = (now_rel - self.t_last) if self.t0 and self.bins else None
            if not self.bins:
                hi = int(now_rel)
                state = "waiting"
            elif age is not None and age < 120:
                hi = int(now_rel)                                  # running (or just stopped): follow the clock
                state = "live" if age < 15 else "stopped"
            else:
                hi = int(self.t_last)                              # an old session: show where it ended
                state = "ended"
            first = min(self.bins) if self.bins else hi - 300
            if self.app:
                first = min(first, int(min(self.app) - self.base))
            lo = max(first, hi - int(window)) if window and window > 0 else first
            rows = self._series(lo, hi, alive=state == "live")
            kpi = _kpis(rows[-10:])
            traffic = any((r.get(k) or 0) > 0 for r in rows[-10:]
                          for k in ("tx_kBps", "udp_rx_kBps", "rx_rel", "chat_tx", "chat_rx", "voice_tx", "voice_rx"))
            if state == "live" and not traffic:
                state = "idle"
            out = {"session": self.dir.name, "dir": str(self.dir), "home": str(self.home), "note": self.note,
                   "node": self.fg.get("node"),
                   "host": self.fg.get("host"), "t0": self.base, "t_last": self.t_last, "age_s": age, "state": state,
                   "lo": lo, "hi": hi, "rows": [{k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()}
                                                for r in self._downsample(rows)], "kpi": kpi,
                   "msgs": self.msgs[-12:], "events": self.events[-12:], "files": self.files,
                   "server_time": time.time(),
                   "settings": {k: v for k, v in (self.fg.get("env") or {}).items() if k.startswith("CDP_")}}
        if self.extra:
            try:
                out.update(self.extra())
            except Exception as e:
                out["extra_error"] = str(e)
        return out


def _kpis(rows):
    def avg(k):
        v = [r[k] for r in rows if r.get(k) is not None]
        return sum(v) / len(v) if v else None

    def mx(k):
        v = [r[k] for r in rows if r.get(k) is not None]
        return max(v) if v else None

    def tot(k):
        v = [r[k] for r in rows if r.get(k) is not None]
        return sum(v) if v else None
    return {"tx_kBps": avg("tx_kBps"), "udp_rx_kBps": avg("udp_rx_kBps"), "busy_pct": avg("busy_pct"),
            "tx_pct": avg("tx_pct"), "wait_p95": mx("wait_p95"), "att_p50": avg("att_p50"), "crc_pct": avg("crc_pct"),
            "loss_pct": avg("loss_pct"), "ba_timeouts": tot("ba_timeouts"), "retry_drops": tot("retry_drops"),
            "queue": mx("queue"), "cpu": mx("cpu"), "rate_pct": min((r["rate_pct"] for r in rows
                                                                      if r.get("rate_pct") is not None), default=None),
            "jitter_p95": mx("jitter_p95"), "voice_tx": avg("voice_tx"), "voice_rx": avg("voice_rx"),
            "chat_tx": avg("chat_tx"), "chat_rx": avg("chat_rx"), "rx_rel": tot("rx_rel"),
            "seconds": len(rows)}


# ---------------------------------------------------------------- web server
class _Handler(BaseHTTPRequestHandler):
    metrics = None

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            self._send(200, page().encode(), "text/html; charset=utf-8")
        elif u.path == "/api/metrics":
            q = parse_qs(u.query)
            try:
                w = float(q.get("window", ["300"])[0])
            except ValueError:
                w = 300.0
            try:
                body = json.dumps(self.metrics.snapshot(w), separators=(",", ":"), default=str).encode()
                self._send(200, body, "application/json")
            except Exception:
                import traceback
                self._send(500, json.dumps({"error": traceback.format_exc()[-1500:]}).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")


class DashboardServer:
    def __init__(self, metrics, port=5310, host="127.0.0.1"):
        handler = type("H", (_Handler,), {"metrics": metrics})
        self.srv = None
        self.error = ""
        for p in range(port, port + 10):
            try:
                self.srv = ThreadingHTTPServer((host, p), handler)
                break
            except OSError as e:
                self.error = str(e)
        if self.srv is None:
            return
        self.url = f"http://{host}:{self.srv.server_address[1]}/"
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, name="dashboard", daemon=True).start()

    @property
    def ok(self):
        return self.srv is not None

    def close(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()


def page(embedded=None):
    """The dashboard page. embedded = a snapshot to show without a server (--html)."""
    data = "null" if embedded is None else json.dumps(embedded, separators=(",", ":"), default=str).replace("</", "<\\/")
    return _PAGE.replace("/*__EMBEDDED__*/null", data)


_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CDP Link Performance</title>
<style>
:root{color-scheme:light;--bg:#f4f3f0;--surface:#fcfcfb;--line:#e4e2dc;--grid:#ecebe6;--text:#0b0b0b;--text2:#52514e;
--muted:#8a8984;--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--good:#0ca30c;--warn:#fab219;--serious:#ec835a;--crit:#d03b3b;
--good-ink:#006300;--crit-ink:#b42828;--warn-ink:#8a5a00}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#121211;--surface:#1a1a19;
--line:#2e2e2b;--grid:#262624;--text:#f0efec;--text2:#c3c2b7;--muted:#8d8c86;--s1:#3987e5;--s2:#d95926;--s3:#199e70;
--good-ink:#5fd35f;--crit-ink:#ff8a8a;--warn-ink:#ffcf5c}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#121211;--surface:#1a1a19;--line:#2e2e2b;--grid:#262624;--text:#f0efec;
--text2:#c3c2b7;--muted:#8d8c86;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--good-ink:#5fd35f;--crit-ink:#ff8a8a;--warn-ink:#ffcf5c}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;padding:16px 20px 8px}
h1{font-size:18px;margin:0;font-weight:650}
.sub{color:var(--text2);font-size:13px}
.pill{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);background:var(--surface);
border-radius:999px;padding:3px 10px;font-size:12px;color:var(--text2)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:var(--surface)}
.seg button{border:0;background:none;color:var(--text2);padding:6px 11px;font:inherit;font-size:12.5px;cursor:pointer}
.seg button[aria-pressed="true"]{background:var(--text);color:var(--surface)}
main{padding:4px 20px 28px;max-width:1500px;margin:0 auto}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(136px,1fr));gap:10px;margin:8px 0 16px}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:10px 12px;min-height:88px}
.tile .k{font-size:12px;color:var(--text2);display:flex;justify-content:space-between;gap:6px}
.tile .v{font-size:24px;font-weight:650;margin-top:4px;font-variant-numeric:tabular-nums}
.tile .v small{font-size:13px;font-weight:500;color:var(--text2);margin-left:3px}
.tile .n{font-size:12px;color:var(--muted);margin-top:2px}
.st{font-size:11.5px;font-weight:600;display:inline-flex;align-items:center;gap:4px;white-space:nowrap}
.st i{font-style:normal}
.st.good{color:var(--good-ink)}.st.warn{color:var(--warn-ink)}.st.crit{color:var(--crit-ink)}.st.none{color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:12px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px 14px 8px;position:relative}
.card h2{font-size:13.5px;margin:0;font-weight:620}
.card .note{font-size:12px;color:var(--muted);margin:1px 0 4px}
.legend{display:flex;gap:12px;flex-wrap:wrap;font-size:12px;color:var(--text2);margin:2px 0 2px}
.legend span{display:inline-flex;align-items:center;gap:5px}
.legend b{display:inline-block;width:14px;height:3px;border-radius:2px}
svg{display:block;width:100%;height:170px;overflow:visible}
svg text{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
.tip{position:absolute;pointer-events:none;background:var(--surface);border:1px solid var(--line);border-radius:8px;
padding:6px 8px;font-size:12px;box-shadow:0 4px 14px rgba(0,0,0,.12);display:none;white-space:nowrap;z-index:5}
.tip .tt{color:var(--muted);margin-bottom:2px}
.tip .row{display:flex;gap:6px;align-items:center}
.tip .row b{width:10px;height:3px;border-radius:2px;display:inline-block}
.tables{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:12px;margin-top:12px}
table{width:100%;border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums}
th{text-align:left;color:var(--text2);font-weight:600;border-bottom:1px solid var(--line);padding:5px 6px}
td{border-bottom:1px solid var(--grid);padding:5px 6px;color:var(--text)}
td.m{color:var(--muted)}
.empty{color:var(--muted);font-size:12.5px;padding:8px 0}
code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;color:var(--text2);white-space:pre-wrap;
word-break:break-all;margin:0}
.notice{display:none;background:var(--surface);border:1px solid var(--warn);border-left-width:4px;border-radius:10px;
padding:10px 12px;margin:8px 0 4px;font-size:13px;color:var(--text)}
.notice.err{border-color:var(--crit)}
.notice pre{white-space:pre-wrap;word-break:break-all;margin:6px 0 0;font-size:11.5px;color:var(--text2)}
@media (max-width:520px){.grid,.tables{grid-template-columns:1fr}main{padding:4px 16px 24px}header{padding:14px 16px 6px}}
</style></head><body>
<header>
 <div><h1>CDP link · performance</h1><div class="sub" id="sub">waiting for data…</div></div>
 <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
  <span class="pill" id="live"><span class="dot" id="livedot"></span><span id="livetxt">connecting</span></span>
  <div class="seg" role="group" aria-label="time window" id="win">
   <button data-w="60">1 min</button><button data-w="300" aria-pressed="true">5 min</button>
   <button data-w="900">15 min</button><button data-w="0">all</button></div>
 </div>
</header>
<main>
 <div class="notice" id="notice" role="status"></div>
 <div class="tiles" id="tiles"></div>
 <div class="grid" id="charts"></div>
 <div class="tables">
  <div class="card"><h2>Nodes</h2><div class="note">heard by this PC · fingerprints of their public keys</div><div id="nodes"></div></div>
  <div class="card"><h2>Reliable messages</h2><div class="note">the last chat items and files (msgs.csv)</div><div id="msgs"></div></div>
  <div class="card"><h2>Encryption &amp; voice</h2><div class="note">this PC</div><div id="sec"></div></div>
  <div class="card"><h2>Events</h2><div class="note">the last rare events (events.log)</div><pre class="mono" id="events"></pre></div>
 </div>
 <p class="mono" id="files" style="margin-top:14px;color:var(--muted)"></p>
</main>
<script>
const EMBEDDED = /*__EMBEDDED__*/null;
let WIN = 300;
try { const w = localStorage.getItem("cdp_dash_win"); if (w !== null) WIN = +w; } catch (e) {}
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const fmt = (v, d = 1) => v === null || v === undefined || Number.isNaN(v) ? "–" :
  (Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(d));
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let T0 = null;
const clock = t => { if (T0 === null) return t.toFixed(0) + " s";
  const d = new Date((T0 + t) * 1000); return d.toTimeString().slice(0, 8); };

// ---- status thresholds: [key, warn, crit, higher-is-worse]
const TILES = [
 {k:"link", label:"Link"},
 {k:"tx_kBps", label:"Sent on air", unit:"kB/s", d:2, note:r=>`datagrams in ${fmt(r.udp_rx_kBps,2)} kB/s`},
 {k:"chat_tx", label:"Chat app", unit:"kB/s", d:2, note:r=>`received ${fmt(r.chat_rx,2)} kB/s · reliable frames in ${fmt(r.rx_rel,0)}/10 s`},
 {k:"busy_pct", label:"Channel busy", unit:"%", warn:70, crit:90, note:r=>`own airtime ${fmt(r.tx_pct)} %`},
 {k:"wait_p95", label:"Queueing delay p95", unit:"ms", warn:500, crit:2000, d:0, note:r=>`access ${fmt(r.att_p50,0)} ms median`},
 {k:"crc_pct", label:"Frame errors (CRC)", unit:"%", warn:5, crit:20, note:()=>"of frames received"},
 {k:"loss_pct", label:"Datagram loss", unit:"%", warn:5, crit:20, note:r=>`jitter p95 ${fmt(r.jitter_p95,0)} ms`},
 {k:"ba_timeouts", label:"Block ACK timeouts", unit:"/10 s", warn:5, crit:30, d:0, note:r=>`gave up ${fmt(r.retry_drops,0)} frames`},
 {k:"cpu", label:"PC load", unit:"% CPU", warn:85, crit:98, d:0, note:r=>`samples ${fmt(r.rate_pct,1)} % of rate`,
  extra:r=> r.rate_pct!==null && r.rate_pct!==undefined && r.rate_pct<95 ? "crit" : (r.rate_pct<99 ? "warn" : null)},
];
const ST = {good:["good","✓","ok"], warn:["warn","!","high"], crit:["crit","✕","critical"], none:["none","·","no data"]};
function status(t, v, r) {
  if (v === null || v === undefined) return "none";
  let s = "good";
  if (t.crit !== undefined && v >= t.crit) s = "crit"; else if (t.warn !== undefined && v >= t.warn) s = "warn";
  const e = t.extra ? t.extra(r) : null;
  if (e === "crit" || (e === "warn" && s === "good")) s = e;
  return s;
}
function stHtml(s, label) { const [c, ic, txt] = ST[s]; return `<span class="st ${c}"><i aria-hidden="true">${ic}</i>${label || txt}</span>`; }

function tiles(d) {
  const r = d.kpi || {}, el = document.getElementById("tiles");
  const nodes = d.nodes || [];
  const on = nodes.filter(n => n.online).length;
  const linkOk = d.link_connected;
  let html = `<div class="tile"><div class="k">Link ${stHtml(linkOk === false ? "crit" : (age(d) > 15 ? "warn" : "good"),
     linkOk === false ? "no flowgraph" : (age(d) > 15 ? "stale" : "live"))}</div>
     <div class="v">${on}<small>of ${nodes.length} node${nodes.length === 1 ? "" : "s"} online</small></div>
     <div class="n">${d.node !== undefined && d.node !== null ? "this PC: node " + esc(d.node) : ""}</div></div>`;
  for (const t of TILES.slice(1)) {
    const v = r[t.k], s = status(t, v, r);
    const idle = s === "none" && (d.state === "idle" || d.state === "live");
    html += `<div class="tile"><div class="k">${t.label} ${stHtml(s, idle ? "no traffic" : undefined)}</div>
      <div class="v">${fmt(v, t.d ?? 1)}<small>${t.unit}</small></div><div class="n">${t.note ? t.note(r) : ""}</div></div>`;
  }
  el.innerHTML = html;
}
const age = d => d.age_s === null || d.age_s === undefined ? 0 : d.age_s;

// ---- charts: one y-axis each, 1-3 series, crosshair tooltip
const CHARTS = [
 {id:"thr", title:"Throughput", note:"payload handed to the air, and datagrams received (kB/s)", unit:"kB/s",
  s:[["tx_kBps","sent","--s1"],["udp_rx_kBps","datagrams received","--s2"]]},
 {id:"frm", title:"Frames per second", note:"data frames sent and received", unit:"/s",
  s:[["tx_frames","sent","--s1"],["rx_frames","received","--s2"],["rx_rel","reliable received (new)","--s3"]]},
 {id:"chan", title:"Channel use", note:"share of each second", unit:"%", max:100,
  s:[["busy_pct","medium busy","--s1"],["tx_pct","own transmissions","--s2"]]},
 {id:"del", title:"Delays", note:"queueing p95, channel access median, datagram jitter p95 (ms)", unit:"ms",
  s:[["wait_p95","queueing p95","--s1"],["att_p50","access median","--s2"],["jitter_p95","jitter p95","--s3"]]},
 {id:"err", title:"Errors", note:"frames with a CRC error, datagrams lost (%)", unit:"%",
  s:[["crc_pct","CRC errors","--s1"],["loss_pct","datagrams lost","--s2"]]},
 {id:"rty", title:"Retries & drops", note:"per second", unit:"/s",
  s:[["ba_timeouts","Block ACK timeouts","--s1"],["retry_drops","given up","--s2"],["udp_drops","datagram queue full","--s3"]]},
 {id:"que", title:"Queues", note:"frames waiting in the MAC", unit:"frames",
  s:[["queue","all","--s1"],["udp_q","datagrams","--s2"]]},
 {id:"pc", title:"PC load", note:"flowgraph CPU (100 = one core) and samples delivered (% of the sample rate)", unit:"%",
  s:[["cpu","CPU","--s1"],["rate_pct","samples","--s2"]]},
 {id:"app", title:"Chat app", note:"this app's messages and files: delivered (acknowledged) and received (kB/s)", unit:"kB/s",
  s:[["chat_tx","sent, delivered","--s1"],["chat_rx","received","--s2"]]},
 {id:"voice", title:"Voice", note:"live voice on the air (kB/s)", unit:"kB/s",
  s:[["voice_tx","sent","--s1"],["voice_rx","received","--s2"]]},
];
const charts = document.getElementById("charts");
for (const c of CHARTS) {
  const div = document.createElement("div");
  div.className = "card"; div.id = "c_" + c.id;
  div.innerHTML = `<h2>${c.title}</h2><div class="note">${c.note}</div>
    <div class="legend">${c.s.map(([k, n, col]) => `<span><b style="background:var(${col})"></b>${n}</span>`).join("")}</div>
    <svg role="img" aria-label="${c.title} over time"></svg><div class="tip"></div>`;
  charts.appendChild(div);
}
function niceStep(v) { if (!(v > 0)) return 0.25; const p = Math.pow(10, Math.floor(Math.log10(v))); const m = v / p;
  return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10) * p; }
const HOVER = {};
function draw(c, rows, lo, hi) {
  const div = document.getElementById("c_" + c.id), svg = div.querySelector("svg"), tip = div.querySelector(".tip");
  const W = svg.clientWidth || 400, H = 170, L = 38, R = 8, Tp = 8, B = 20;
  let ymax = 0;
  for (const r of rows) for (const [k] of c.s) if (r[k] !== null && r[k] !== undefined) ymax = Math.max(ymax, r[k]);
  const step = c.max && ymax <= c.max ? c.max / 4 : niceStep(ymax / 4);
  ymax = step * 4;
  const span = Math.max(1, hi - lo);
  const x = t => L + (t - lo) / span * (W - L - R), y = v => Tp + (1 - v / ymax) * (H - Tp - B);
  let g = "";
  for (let i = 0; i <= 4; i++) { const v = ymax * i / 4, yy = y(v);
    g += `<line x1="${L}" x2="${W - R}" y1="${yy}" y2="${yy}" stroke="var(--grid)" stroke-width="1"/>`;
    g += `<text x="${L - 6}" y="${yy + 3.5}" text-anchor="end">${+v.toFixed(2)}</text>`; }
  const ticks = Math.max(2, Math.min(4, Math.floor((W - L - R) / 90)));
  for (let i = 0; i <= ticks; i++) { const t = lo + span * i / ticks;
    g += `<text x="${x(t)}" y="${H - 4}" text-anchor="${i === 0 ? "start" : i === ticks ? "end" : "middle"}">${clock(t)}</text>`; }
  g += `<line x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}" stroke="var(--line)" stroke-width="1"/>`;
  let any = false;
  for (const [k, , col] of c.s) {
    let d = "", prev = null;
    for (const r of rows) { const v = r[k];
      if (v === null || v === undefined) continue;
      any = true;
      const gap = prev !== null && r.t - prev > 12;
      d += (d === "" || gap ? "M" : "L") + x(r.t).toFixed(1) + " " + y(Math.min(v, ymax)).toFixed(1);
      prev = r.t; }
    if (d) g += `<path d="${d}" fill="none" stroke="var(${col})" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
  }
  if (!any) g += `<text x="${(L + W) / 2}" y="${H / 2}" text-anchor="middle">no data in this window</text>`;
  g += `<line class="xh" x1="0" x2="0" y1="${Tp}" y2="${H - B}" stroke="var(--muted)" stroke-width="1" visibility="hidden"/>`;
  g += `<rect x="${L}" y="${Tp}" width="${W - L - R}" height="${H - Tp - B}" fill="transparent"/>`;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = g;
  const xh = svg.querySelector(".xh");
  const show = clientX => {
    if (!rows.length) return;
    const bx = svg.getBoundingClientRect(), px = (clientX - bx.left) * W / bx.width;
    const t = lo + (px - L) / (W - L - R) * span;
    let best = rows[0]; for (const r of rows) if (Math.abs(r.t - t) < Math.abs(best.t - t)) best = r;
    xh.setAttribute("x1", x(best.t)); xh.setAttribute("x2", x(best.t)); xh.setAttribute("visibility", "visible");
    tip.innerHTML = `<div class="tt">${clock(best.t)}</div>` + c.s.map(([k, n, col]) =>
      `<div class="row"><b style="background:var(${col})"></b>${n}: <strong>${fmt(best[k], 2)}</strong> ${c.unit}</div>`).join("");
    tip.style.display = "block";
    const left = clientX - div.getBoundingClientRect().left;
    tip.style.left = Math.max(6, Math.min(left + 12, div.clientWidth - tip.offsetWidth - 6)) + "px";
    tip.style.top = "56px"; };
  svg.onmousemove = e => { HOVER[c.id] = e.clientX; show(e.clientX); };
  svg.onmouseleave = () => { delete HOVER[c.id]; xh.setAttribute("visibility", "hidden"); tip.style.display = "none"; };
  if (HOVER[c.id] !== undefined) show(HOVER[c.id]);       // keep the tooltip while the data refreshes
}
function tables(d) {
  const nodes = d.nodes || [];
  document.getElementById("nodes").innerHTML = nodes.length ? `<table><tr><th>node</th><th>computer</th><th>state</th>
    <th>last heard</th><th>key fingerprint</th></tr>` + nodes.map(n => `<tr><td>${esc(n.node)}</td><td>${esc(n.host || "–")}</td>
    <td>${n.online ? stHtml("good", "online") : stHtml("none", "away")}</td><td class="m">${n.last_heard_s === null ? "–" : fmt(n.last_heard_s, 0) + " s ago"}</td>
    <td><code>${esc(n.fingerprint || "no key yet")}</code></td></tr>`).join("") + "</table>"
    : `<div class="empty">${d.nodes === undefined ? "only shown when opened from the chat window" : "no other node heard yet"}</div>`;
  const m = (d.msgs || []).slice().reverse();
  document.getElementById("msgs").innerHTML = m.length ? `<table><tr><th>time</th><th>dir</th><th>node</th><th>size</th>
    <th>took</th><th>kB/s</th><th>result</th></tr>` + m.map(r => `<tr><td class="m">${clock(+r.t)}</td><td>${esc(r.dir)}</td>
    <td>${esc(r.peer)}</td><td>${fmt(+r.bytes / 1000, 1)} kB</td><td>${r.dur_s ? fmt(+r.dur_s, 1) + " s" : "–"}</td>
    <td>${r.kBps ? fmt(+r.kBps, 2) : "–"}</td><td>${/^(sent|ok)/.test(r.res || "") ? stHtml("good", esc(r.res)) : stHtml("crit", esc(r.res))}</td></tr>`).join("") + "</table>"
    : `<div class="empty">no reliable message yet</div>`;
  const c = d.crypto, v = d.voice;
  let s = "";
  if (c) s += `<table><tr><th colspan="2">encryption</th></tr><tr><td>state</td><td>${c.on ? stHtml("good", "on") : stHtml("warn", "off")}</td></tr>
    <tr><td>method</td><td>${esc(c.method)}</td></tr><tr><td>my fingerprint</td><td><code>${esc(c.fingerprint)}</code></td></tr>
    <tr><td>nodes with a key</td><td>${esc(c.peers_keyed)}</td></tr><tr><td>sent / received encrypted</td><td>${esc(c.sealed)} / ${esc(c.opened)}${c.failed ? " · " + c.failed + " failed" : ""}</td></tr></table>`;
  if (v) s += `<table style="margin-top:8px"><tr><th colspan="2">voice</th></tr><tr><td>audio</td><td>${esc(v.audio)}</td></tr>
    <tr><td>frames sent / received</td><td>${esc(v.tx_frames)} / ${esc(v.rx_frames)}</td></tr>
    <tr><td>lost / too late / filled in</td><td>${esc(v.lost)} / ${esc(v.late)} / ${esc(v.plc)}</td></tr>
    <tr><td>rejected (bad tag / no key)</td><td>${esc(v.auth_fail)} / ${esc(v.no_key)}</td></tr></table>`;
  document.getElementById("sec").innerHTML = s || `<div class="empty">only shown when opened from the chat window</div>`;
  document.getElementById("events").textContent = (d.events || []).slice().reverse().map(l => {
    const sp = l.indexOf(" "), t = parseFloat(l.slice(0, sp));
    return Number.isFinite(t) ? clock(t) + l.slice(sp) : l; }).join("\n") || "none";
}
function render(d) {
  T0 = d.t0;
  document.getElementById("sub").textContent =
    `node ${d.node ?? "?"} · ${d.host || ""} · session ${d.session}` + (d.settings && d.settings.CDP_SAMP_RATE ? ` · ${d.settings.CDP_SAMP_RATE} S/s` : "");
  const STATE = {live: ["var(--good)", "live"], idle: ["var(--good)", "live · link idle"],
    stopped: ["var(--warn)", `flowgraph stopped? no log rows for ${fmt(age(d), 0)} s`],
    waiting: ["var(--warn)", "waiting for the flowgraph's logs - press ▶ Run in GRC"],
    ended: ["var(--muted)", "session ended"]};
  const [col, txt] = EMBEDDED ? ["var(--muted)", "saved report"] : (STATE[d.state] || STATE.waiting);
  document.getElementById("livedot").style.background = col;
  document.getElementById("livetxt").textContent = txt + (EMBEDDED ? "" : ` · updated ${new Date().toTimeString().slice(0, 8)}`);
  const rows = d.rows || [];
  const hi = d.hi ?? (rows.length ? rows[rows.length - 1].t : 0);
  const lo = d.lo ?? (WIN > 0 ? hi - WIN : (rows.length ? rows[0].t : 0));
  const vis = rows.filter(r => r.t >= lo);
  const f = d.files || {};
  document.getElementById("files").textContent = d.dir ? `reading ${d.dir}  ·  ` + Object.entries(f).map(([n, v]) =>
    `${n} ${v.rows} rows${v.last_t !== null && v.last_t !== undefined ? " (last " + clock(v.last_t) + ")" : ""}`).join("  ·  ") : "";
  showNotice(d.note ? esc(d.note) : "", false);
  tiles(d);
  for (const c of CHARTS) draw(c, vis, lo, hi);
  tables(d);
}
function showNotice(html, err) {
  const n = document.getElementById("notice");
  n.className = "notice" + (err ? " err" : ""); n.innerHTML = html; n.style.display = html ? "block" : "none";
}
let LAST = null, TIMER = null, BUSY = false;
async function poll() {
  if (EMBEDDED) { LAST = EMBEDDED; render(EMBEDDED); return; }
  if (BUSY) return;                                   // never two requests at once
  BUSY = true;
  const ctl = new AbortController(), to = setTimeout(() => ctl.abort(), 8000);
  let d = null;
  try {
    const r = await fetch(`/api/metrics?window=${WIN > 0 ? WIN : 0}&_=${Date.now()}`, {cache: "no-store", signal: ctl.signal});
    d = await r.json();
    if (!r.ok || d.error) throw {server: d.error || ("HTTP " + r.status)};
  } catch (e) {
    clearTimeout(to); BUSY = false;
    document.getElementById("livedot").style.background = "var(--crit)";
    if (e && e.server) {
      document.getElementById("livetxt").textContent = "dashboard error (details above the tiles)";
      showNotice(`The dashboard could not read the logs. Please send this text:<pre>${esc(e.server)}</pre>`, true);
    } else {
      document.getElementById("livetxt").textContent = "dashboard server not answering (chat window closed?) · " +
        new Date().toTimeString().slice(0, 8);
    }
    return;
  }
  clearTimeout(to); BUSY = false;
  LAST = d;
  try { render(d); }
  catch (e) {
    showNotice(`The page could not draw this data. Please send this text:<pre>${esc(e && e.stack || e)}</pre>`, true);
  }
}
function schedule() { clearTimeout(TIMER); TIMER = setTimeout(async () => { await poll(); schedule(); }, 1000); }
document.addEventListener("visibilitychange", () => { if (!document.hidden) { poll(); schedule(); } });
for (const b of document.querySelectorAll("#win button")) {
  b.setAttribute("aria-pressed", String(+b.dataset.w === WIN));
  b.onclick = () => { WIN = +b.dataset.w;
    try { localStorage.setItem("cdp_dash_win", String(WIN)); } catch (e) {}
    for (const x of document.querySelectorAll("#win button")) x.setAttribute("aria-pressed", String(x === b));
    if (LAST && EMBEDDED) render(LAST); else { BUSY = false; poll(); } };
}
window.addEventListener("resize", () => LAST && render(LAST));
poll();
if (!EMBEDDED) schedule();
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session", help="a session folder, e.g. debug/20261019_153000_node0")
    ap.add_argument("--port", type=int, default=5311)
    ap.add_argument("--html", default=None, help="write one self-contained page instead of serving")
    a = ap.parse_args()
    m = Metrics(a.session)
    if a.html:
        Path(a.html).write_text(page(m.snapshot(0)))
        print(f"dashboard written to {a.html}")
        return
    srv = DashboardServer(m, a.port)
    if not srv.ok:
        raise SystemExit(f"cannot open a port near {a.port}: {srv.error}")
    print(f"dashboard: {srv.url}  (Ctrl-C to stop)")
    try:
        import webbrowser
        webbrowser.open(srv.url)
    except Exception:
        pass
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.close()


if __name__ == "__main__":
    main()
