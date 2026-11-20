#!/usr/bin/env python3
"""
cdp_metrics.py - the data behind the performance dashboard: reads the session logs the flowgraph writes
(csma_*.csv, phy_1s.csv, udp.csv, arq_tx.csv, msgs.csv, events.log - see LOGS.md) while they grow, and turns them
into per-second rows and 10-second key figures (Metrics.snapshot).

v6.9.3: the dashboard is a window of the app (cdp_dashboard.py, PyQt5); the web page and its server are gone.
    python3 cdp_dashboard.py debug/<session>         # an old session, or the other PC's logs
"""
import csv
import io
import json
import math
import os
import threading
import time
from pathlib import Path

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


def main():
    """the dashboard window for a session folder (cdp_dashboard.py)"""
    import cdp_dashboard
    cdp_dashboard.main()


if __name__ == "__main__":
    main()
