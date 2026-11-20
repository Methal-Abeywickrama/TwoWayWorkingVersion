"""
Embedded Python Block: Session Logger (v6.9) - writes compact performance logs of this flowgraph.

Folder: log_dir if given, else $CDP_DEBUG_DIR (set by cdp_daemon.py to its session folder), else
~/cdp_debug/<date>_<time>_node<id>. Files (CSV, one header line, times in seconds since t0 of flowgraph.json):
  flowgraph.json   t0 (epoch), node, every block's settings
  csma_txop.csv    one row per channel access (data burst, datagram burst, Block ACK): waits, contention, airtime
  csma_1s.csv      MAC state once a second while active (every 10 s when idle)
  arq_tx.csv       one row per Block ACK result / timeout (window movement, retransmissions, drops)
  arq_rx.csv       one row per reliable frame received (reordering, duplicates, window)
  udp.csv          one row per datagram sent / received (timestamps, one-way delay, gaps)
  msgs.csv         one row per reliable message (file / chat) sent or received at the socket bridge
  phy_1s.csv       receiver counters once a second while frames arrive (sync, CRC, squelch, noise floor)
  events.log       rare things worth reading (drops, late unmute, client connects, errors)
Columns are documented in LOGS.md (proc_cpu in csma_1s.csv = CPU of the whole flowgraph process, 100 = one core).
Wiring: the message input 'attach' must be connected (the .grc connects the bridge's mac_ctrl to it). GNU Radio only
runs blocks that sit on a stream or message connection; without one this block is built but never started, so it
writes nothing (and prints no "[LOG] performance logs -> ..." line). The messages themselves are ignored.
Nothing here changes how the link works.
"""
import json
import os
import socket
import threading

import numpy as np
import pmt
from gnuradio import gr

# ---- shared debug bus (one per flowgraph process; the Stress Test block writes it to disk) ----
import sys as _sys
import types as _types
import collections as _collections
import time as _time


def _bus():
    b = _sys.modules.get("cdp_dbg_bus")
    if b is None:
        nb = _types.ModuleType("cdp_dbg_bus")
        nb.events = _collections.deque(maxlen=400000)   # dicts; oldest dropped if nobody writes them
        nb.sources = {}                                 # name -> object with dbg_snapshot()
        b = _sys.modules.setdefault("cdp_dbg_bus", nb)
    return b


def _emit(blk, kind, /, **kw):
    kw["t"] = round(_time.time(), 6)
    kw["blk"] = blk
    kw["k"] = kind
    _bus().events.append(kw)
# ---- end debug bus ----

COLS = {
    "csma_txop.csv": ["t", "kind", "dst", "bsn", "n", "bytes", "retx", "q", "wait_ms", "att_ms", "p_ms", "p_draws",
                      "bo", "cw", "defer", "busy_ms", "nav_ms", "unmute_ms", "air_ms", "off_ms", "echo_ms", "res",
                      "acked", "rtt_ms", "bitmap", "sns"],
    "csma_1s.csv": ["t", "busy_pct", "nav_pct", "tx_pct", "floor_db", "thr_db", "queue", "udp_q", "ba_due", "cw", "k",
                    "phase", "awaiting_ba", "rate_pct", "echo_ms", "mute", "mute_wr_max_ms", "deferrals", "busy_on",
                    "nav_set", "tx_D", "tx_BA", "ba_rx", "ba_timeout", "retry_drop", "rx_data", "rx_dup", "windows",
                    "proc_cpu", "load1"],
    "arq_tx.csv": ["t", "dst", "what", "bsn", "acked", "nacked", "dropped", "ws0", "ws", "used", "backlog"],
    "arq_rx.csv": ["t", "src", "sn", "bsn", "idx", "cnt", "dup", "deliv", "buf", "gaveup", "rws", "retry", "fec"],
    "udp.csv": ["t", "dir", "peer", "useq", "bytes", "owd_ms", "iat_ms", "gap", "mac_sn"],
    "msgs.csv": ["t", "dir", "peer", "mid", "ref", "bytes", "segs", "got", "frames_tx", "resent", "dups", "wait_s",
                 "dur_s", "kBps", "owd_first_ms", "owd_max_ms", "res"],
    "phy_1s.csv": ["t", "corr_tags", "corr_best", "train", "train_ok", "locks", "crc_fail", "bad_len", "ok", "other",
                   "own", "sq_eob", "noise_db", "squelch_db"],
}
EVENT_KINDS = {"seg_resend", "cancel", "udp_drop", "drop", "retry_drop", "late_unmute", "ba_stray", "abort", "transitions_suppressed",
               "error", "client", "msg_submit_fail"}


def ranges(xs):
    """[3,4,5,9] -> '3-5 9' (compact list of sequence numbers)"""
    xs = sorted(int(x) for x in xs)
    out, i = [], 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[j + 1] == xs[j] + 1:
            j += 1
        out.append(str(xs[i]) if i == j else f"{xs[i]}-{xs[j]}")
        i = j + 1
    return " ".join(out)


def cell(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.3f}".rstrip("0").rstrip(".") if abs(v) < 1e6 else f"{v:.0f}"
    if isinstance(v, (list, tuple)):
        return ranges(v) if all(isinstance(x, (int, np.integer)) for x in v) else " ".join(map(str, v))
    if isinstance(v, dict):
        return json.dumps(v, separators=(",", ":"))
    s = str(v)
    return '"' + s.replace('"', "'") + '"' if ("," in s or '"' in s) else s


class SessionLogger(gr.basic_block):
    def __init__(self, my_id=1, log_dir="", flush_s=0.5, idle_every_s=10.0):
        gr.basic_block.__init__(self, name="Session Logger (performance logs)", in_sig=None, out_sig=None)
        self.my_id = int(my_id)
        self.log_dir = str(log_dir)
        self.flush_s = float(flush_s)
        self.idle_every_s = float(idle_every_s)
        self._stop = threading.Event()
        self._thr = None
        self._f = {}
        self._t0 = None
        self._cfg = {}
        self._last_csma_row = 0.0
        self._last_phy_row = 0.0
        self.dir = None
        self._cpu_last = None
        self._ev_sec = {}                    # (blk, kind) -> [second, count] : at most 5 identical lines per second
        # connected only so that GNU Radio runs this block (see Wiring above); what arrives is ignored
        self.message_port_register_in(pmt.intern("attach"))
        self.set_msg_handler(pmt.intern("attach"), self._on_attach)

    def _on_attach(self, msg):
        pass

    def start(self):
        d = self.log_dir or os.environ.get("CDP_DEBUG_DIR", "")
        if not d:
            d = os.path.join(os.path.expanduser("~/cdp_debug"),
                             _time.strftime("%Y%m%d_%H%M%S") + f"_node{self.my_id}")
        d = os.path.expanduser(d)
        os.makedirs(d, exist_ok=True)
        self.dir = d
        self._t0 = _time.time()
        for name, cols in COLS.items():
            f = open(os.path.join(d, name), "w", buffering=1 << 16)
            f.write(",".join(cols) + "\n")
            self._f[name] = f
        self._f["events.log"] = open(os.path.join(d, "events.log"), "w", buffering=1 << 14)
        self._cfg = {"t0": round(self._t0, 3), "t0_local": _time.strftime("%Y-%m-%d %H:%M:%S"), "node": self.my_id,
                     "host": socket.gethostname(), "env": {k: v for k, v in os.environ.items() if k.startswith("CDP_")},
                     "blocks": {}}
        self._write_cfg()
        _bus().sources["logger"] = self
        self._stop.clear()
        self._thr = threading.Thread(target=self._run, name="session-logger", daemon=True)
        self._thr.start()
        print(f"[LOG] performance logs -> {d}", flush=True)
        return True

    def stop(self):
        self._stop.set()
        if self._thr is not None:
            self._thr.join(timeout=3.0)
        self._drain()
        self._snapshots(final=True)
        self._line("events.log", f"{self._rel():.3f} flowgraph stopped")
        for f in self._f.values():
            try:
                f.close()
            except Exception:
                pass
        return True

    # ------------------------------------------------------------------ helpers
    def _rel(self, t=None):
        return (t if t is not None else _time.time()) - self._t0

    def _row(self, name, d):
        try:
            self._f[name].write(",".join(cell(d.get(c)) for c in COLS[name]) + "\n")
        except Exception:
            pass

    def _event(self, t, blk, k, e):
        key, sec = (blk, k), int(t)
        cur = self._ev_sec.get(key)
        if cur is None or cur[0] != sec:
            if cur is not None and cur[1] > 5:
                self._line("events.log", f"{cur[0]}.999 {blk} {k} ... {cur[1] - 5} more in that second")
            cur = self._ev_sec[key] = [sec, 0]
        cur[1] += 1
        if cur[1] <= 5:
            rest = " ".join(f"{kk}={cell(v)}" for kk, v in e.items() if kk not in ("t", "blk", "k", "ts"))
            self._line("events.log", f"{t:.3f} {blk} {k} {rest}")

    def _line(self, name, txt):
        try:
            self._f[name].write(txt + "\n")
        except Exception:
            pass

    def _write_cfg(self):
        try:
            with open(os.path.join(self.dir, "flowgraph.json"), "w") as f:
                json.dump(self._cfg, f, indent=1, default=str)
        except Exception:
            pass

    # ------------------------------------------------------------------ main loop
    def _run(self):
        nxt = _time.monotonic() + 1.0
        while not self._stop.wait(self.flush_s):
            self._drain()
            if _time.monotonic() >= nxt:
                nxt += 1.0
                self._snapshots()
            for f in self._f.values():
                try:
                    f.flush()
                except Exception:
                    pass

    def _drain(self):
        ev = _bus().events
        cfg_changed = False
        while True:
            try:
                e = ev.popleft()
            except IndexError:
                break
            try:
                cfg_changed |= self._handle(e)
            except Exception as ex:
                self._line("events.log", f"{self._rel():.3f} logger error {ex!r} on {e.get('blk')}/{e.get('k')}")
        if cfg_changed:
            self._write_cfg()

    def _handle(self, e):
        blk, k = e.get("blk", ""), e.get("k", "")
        t = round(self._rel(e.get("t")), 3)
        if k == "config":
            self._cfg["blocks"][blk] = {kk: v for kk, v in e.items() if kk not in ("t", "blk", "k")}
            return True
        if blk == "csma":
            if k == "txop":
                self._row("csma_txop.csv", {**e, "t": t})
            elif k == "arq":
                self._row("arq_tx.csv", {"t": t, "dst": e.get("dst"), "what": e.get("what"), "bsn": e.get("bsn"),
                                         "acked": e.get("acked"), "nacked": e.get("nacked"),
                                         "dropped": e.get("dropped"), "ws0": e.get("ws_before"), "ws": e.get("ws"),
                                         "used": e.get("used"), "backlog": e.get("backlog")})
            elif k == "rx_data" and not e.get("noack"):
                self._row("arq_rx.csv", {"t": t, "src": e.get("src"), "sn": e.get("seq"), "bsn": e.get("bsn"),
                                         "idx": e.get("idx"), "cnt": e.get("cnt"), "dup": e.get("dup"),
                                         "deliv": e.get("delivered"), "buf": e.get("buffered"),
                                         "gaveup": e.get("gave_up"), "rws": e.get("rws"), "retry": e.get("retry"),
                                         "fec": e.get("fec")})
            elif k == "ba_rx" and e.get("late"):
                self._line("events.log", f"{t:.3f} csma late Block ACK burst {e.get('bsn')} from {e.get('src')} "
                                         f"bitmap {e.get('bitmap')}")
            elif k in EVENT_KINDS:
                self._event(t, blk, k, e)
            return False
        if blk == "bridge":
            if k == "udp_tx":
                self._row("udp.csv", {"t": t, "dir": "tx", "peer": e.get("dst"), "useq": e.get("useq"),
                                      "bytes": e.get("bytes")})
            elif k == "udp_rx":
                self._row("udp.csv", {"t": t, "dir": "rx", "peer": e.get("src"), "useq": e.get("useq"),
                                      "bytes": e.get("bytes"), "owd_ms": e.get("owd_ms"), "iat_ms": e.get("iat_ms"),
                                      "gap": e.get("gap"), "mac_sn": e.get("mac_sn")})
            elif k == "msg_tx":
                self._row("msgs.csv", {**e, "t": t, "dir": "tx", "peer": e.get("dst"), "got": e.get("acked")})
            elif k == "msg_rx":
                self._row("msgs.csv", {**e, "t": t, "dir": "rx", "peer": e.get("src")})
            elif k in EVENT_KINDS or k == "msg_submit":
                if k != "msg_submit":
                    self._event(t, blk, k, e)
            return False
        if k in EVENT_KINDS or k == "warning":
            self._event(t, blk, k, e)
        return False

    # ------------------------------------------------------------------ once a second
    def _snapshots(self, final=False):
        now = _time.time()
        t = round(self._rel(now), 3)
        src = _bus().sources
        cs = src.get("csma")
        if cs is not None:
            try:
                s = cs.dbg_snapshot()
            except Exception:
                s = None
            if s:
                active = any(s.get(x) for x in ("tx_D", "tx_BA", "ba_rx", "rx_data", "deferrals", "busy_on")) or \
                    s.get("queue") or s.get("ba_due") or (s.get("busy_pct") or 0) > 1
                cpu = _time.process_time()
                proc_cpu = None
                if self._cpu_last is not None:
                    proc_cpu = round(100.0 * (cpu - self._cpu_last[0]) / max(1e-6, now - self._cpu_last[1]), 1)
                self._cpu_last = (cpu, now)
                try:
                    load1 = round(os.getloadavg()[0], 2)
                except (OSError, AttributeError):
                    load1 = None
                if active or final or now - self._last_csma_row >= self.idle_every_s or (proc_cpu or 0) > 90:
                    self._last_csma_row = now
                    s["proc_cpu"], s["load1"] = proc_cpu, load1
                    w = s.get("windows") or {}
                    self._row("csma_1s.csv", {**s, "t": t, "tx_pct": s.get("self_tx_pct"), "echo_ms": s.get("echo_med_ms"),
                                              "mute_wr_max_ms": s.get("mute_write_ms_max"),
                                              "udp_q": sum((s.get("udp_q") or {}).values()),
                                              "windows": " ".join(f"{d}:{v['ws']}/{v['used']}/{v['backlog']}"
                                                                  for d, v in w.items())})
        df, co, no = src.get("deframer"), src.get("corr"), src.get("noise")
        row = {"t": t}
        try:
            if df is not None:
                d = df.dbg_snapshot()
                row.update({"train": d.get("train"), "train_ok": d.get("train_clean"), "locks": d.get("lock"),
                            "crc_fail": d.get("crc_fail"), "bad_len": d.get("bad_len"), "ok": d.get("ok"),
                            "other": d.get("other"), "own": d.get("own"), "sq_eob": d.get("sq_eob")})
            if co is not None:
                c = co.dbg_snapshot()
                row.update({"corr_tags": c.get("tags"), "corr_best": c.get("best")})
            if no is not None:
                z = no.dbg_snapshot()
                row.update({"noise_db": z.get("floor_db"), "squelch_db": z.get("squelch_thr_db")})
        except Exception:
            pass
        active = any(row.get(x) for x in ("corr_tags", "train", "locks", "crc_fail", "ok", "sq_eob"))
        if active or final or now - self._last_phy_row >= self.idle_every_s:
            self._last_phy_row = now
            self._row("phy_1s.csv", row)
