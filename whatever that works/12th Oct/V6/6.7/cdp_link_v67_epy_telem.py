"""
Embedded Python Block: Telemetry + watchdog (reads the other blocks through a shared registry)

Port: mac_in (message) - connect the ARQ block's mac_ctrl output here. Besides counting the ARQ's ok/fail
signals, this connection is what makes GNU Radio run this block at all (unconnected blocks are never started).

Every period_s it takes a snapshot of every registered block (csma, deframer, arq, udp, noise, corr, txprobe) and of
the process (CPU, per-thread CPU by GNU Radio thread name, memory, how late this thread wakes up = GIL stalls),
checks it for problems (stalled receive stream, stuck CSMA state, stalled TX path, stuck ARQ messages, frames
waiting in the deframer, dropped samples, TX mute errors ...), and
  - appends it to <log_dir>/telemetry_node<ID>.jsonl (one JSON line per snapshot; rotated at 50 MB)
  - sends it as one JSON line to every client of tcp://bind_addr:port (the dashboard of cdp_link_app.py)
  - prints problems as [WATCHDOG] lines.
When the receive stream, the ARQ thread or a CSMA state stalls, it writes the Python stack of every thread to
<log_dir>/stacks_node<ID>.txt. "kill -USR1 <pid>" (or the dashboard's "Dump stacks") writes them too, even if
the process is frozen (faulthandler).

Commands (one JSON object per line from a client):
  {"cmd": "set", "target": "arq", "key": "max_rto_s", "value": 4}   change a parameter while running
  {"cmd": "stacks"}                                                  dump all Python thread stacks now
  {"cmd": "snapshot"}                                                send a snapshot now
"""
import collections
import json
import os
import selectors
import socket
import sys
import threading
import time
import traceback
from datetime import datetime

import pmt
from gnuradio import gr

LIVE = {
    "arq": {"mss": int, "window": int, "ack_every": int, "ack_delay_s": float, "init_rto_s": float,
            "min_rto_s": float, "max_rto_s": float, "max_retries": int},
    "csma": {"slot_s": float, "difs_s": float, "cw_min": int, "cw_max": int, "cs_margin_db": float,
             "gate_guard_s": float, "tx_delay_s": float, "tx_att_db": float, "tx_mute": int},
    "noise": {"margin_db": float},
    "deframer": {"debug": int},
    "corr": {"thr": float},
}


def _registry():
    reg = sys.modules.get("cdp_registry")
    return reg.blocks if reg is not None else {}


class Telemetry(gr.basic_block):
    def __init__(self, node_id=1, port=5300, bind_addr="127.0.0.1", period_s=1.0, stall_s=3.0,
                 log_dir="~/.cdp_link", verbose=1):
        gr.basic_block.__init__(self, name="Telemetry + watchdog", in_sig=None, out_sig=None)
        self.node_id = int(node_id)
        self.port = int(port)
        self.bind_addr = str(bind_addr)
        self.period_s = max(0.2, float(period_s))
        self.stall_s = float(stall_s)
        self.log_dir = os.path.expanduser(str(log_dir))
        self.verbose = int(verbose)
        self._run = False
        self._thr = None
        self._sel = None
        self._lsock = None
        self._clients = {}
        self._events = collections.deque(maxlen=300)
        self._new_events = []
        self._active = {}                 # episode key -> start time (a problem is reported once per episode)
        self._prev = {}
        self._t0 = time.monotonic()
        self._log = None
        self._log_path = None
        self._stack_path = None
        self._stack_file = None
        self._threads_prev = {}
        self._proc_prev = None
        self._late_max = 0.0
        self.snapshots = 0
        self._mac = collections.Counter()
        self.message_port_register_in(pmt.intern("mac_in"))
        self.set_msg_handler(pmt.intern("mac_in"), self._on_mac)

    def _on_mac(self, msg):
        try:
            self._mac[str(pmt.symbol_to_string(msg))] += 1
        except Exception:
            self._mac["other"] += 1

    # ------------------------------------------------------------------ GNU Radio side
    def start(self):
        os.makedirs(self.log_dir, exist_ok=True)
        self._log_path = os.path.join(self.log_dir, f"telemetry_node{self.node_id}.jsonl")
        self._stack_path = os.path.join(self.log_dir, f"stacks_node{self.node_id}.txt")
        try:
            import faulthandler
            import signal
            self._stack_file = open(self._stack_path, "a")
            faulthandler.register(signal.SIGUSR1, file=self._stack_file, all_threads=True)
        except Exception as e:
            print(f"[TELEM] SIGUSR1 stack dumps unavailable: {e}", flush=True)
        self._sel = selectors.DefaultSelector()
        self._lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._lsock.bind((self.bind_addr, self.port))
            self._lsock.listen(4)
            self._lsock.setblocking(False)
            self._sel.register(self._lsock, selectors.EVENT_READ, None)
        except OSError as e:
            print(f"[TELEM] cannot listen on {self.bind_addr}:{self.port}: {e} (dashboard can't connect)", flush=True)
            self._lsock.close()
            self._lsock = None
        self._run = True
        self._t0 = time.monotonic()
        self._thr = threading.Thread(target=self._loop, name="telemetry", daemon=True)
        self._thr.start()
        print(f"[TELEM] node {self.node_id}: telemetry on tcp://{self.bind_addr}:{self.port}, log {self._log_path}, "
              f"pid {os.getpid()} (kill -USR1 {os.getpid()} dumps all Python stacks to {self._stack_path})", flush=True)
        return True

    def stop(self):
        self._run = False
        if self._thr is not None:
            self._thr.join(timeout=2.0)
        for c in list(self._clients):
            try:
                c.close()
            except OSError:
                pass
        for f in (self._lsock, self._log):
            try:
                if f is not None:
                    f.close()
            except OSError:
                pass
        return True

    # ------------------------------------------------------------------ worker
    def _loop(self):
        next_t = time.monotonic() + self.period_s
        tick = 0.1
        last = time.monotonic()
        while self._run:
            for key, _ in self._sel.select(timeout=tick):
                if key.data is None:
                    self._accept()
                else:
                    self._client_read(key.fileobj)
            now = time.monotonic()
            late = now - last - tick
            if late > 0:
                self._late_max = max(self._late_max, late)
            last = now
            if now >= next_t:
                next_t = now + self.period_s
                try:
                    self._snapshot_and_check()
                except Exception as e:
                    print(f"[TELEM] snapshot error: {e!r}", flush=True)
                last = time.monotonic()                      # don't count our own work as lateness

    def _accept(self):
        try:
            s, _ = self._lsock.accept()
        except OSError:
            return
        s.setblocking(False)
        self._clients[s] = bytearray()
        self._sel.register(s, selectors.EVENT_READ, "client")

    def _drop(self, s):
        try:
            self._sel.unregister(s)
        except Exception:
            pass
        try:
            s.close()
        except OSError:
            pass
        self._clients.pop(s, None)

    def _client_read(self, s):
        try:
            data = s.recv(65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if not data:
            return self._drop(s)
        buf = self._clients[s]
        buf += data
        while b"\n" in buf:
            line, _, rest = bytes(buf).partition(b"\n")
            buf[:] = rest
            try:
                reply = self._command(json.loads(line.decode()))
            except Exception as e:
                reply = {"reply": "error", "error": repr(e)}
            self._send(s, reply)

    def _send(self, s, obj):
        data = (json.dumps(obj, default=str) + "\n").encode()
        try:
            s.setblocking(True)
            s.settimeout(0.5)
            s.sendall(data)
            s.setblocking(False)
        except OSError:
            self._drop(s)

    def _command(self, c):
        cmd = c.get("cmd")
        if cmd == "set":
            target, key, value = c.get("target"), c.get("key"), c.get("value")
            blk = _registry().get(target)
            caster = LIVE.get(target, {}).get(key)
            if blk is None or caster is None:
                return {"reply": "set", "ok": False, "error": f"{target}.{key} cannot be changed while running"}
            v = caster(value)
            if target == "arq" and key == "window":
                v = max(1, min(32, v))
            setattr(blk, key, v)
            if target == "csma" and key == "cw_min":
                blk._cw = v
            self._event("info", f"set_{target}_{key}", f"{target}.{key} set to {v} while running")
            return {"reply": "set", "ok": True, "target": target, "key": key, "value": v}
        if cmd == "stacks":
            path, text = self.dump_stacks("requested from the dashboard")
            return {"reply": "stacks", "path": path, "text": text}
        if cmd == "snapshot":
            return self._snapshot_and_check(send=False)
        return {"reply": "error", "error": f"unknown command {cmd!r}"}

    # ------------------------------------------------------------------ measurements
    def _proc(self, dt):
        tck = os.sysconf("SC_CLK_TCK")
        threads = []
        cur = {}
        try:
            for tid in os.listdir("/proc/self/task"):
                try:
                    with open(f"/proc/self/task/{tid}/stat") as f:
                        st = f.read()
                    with open(f"/proc/self/task/{tid}/comm") as f:
                        name = f.read().strip()
                except OSError:
                    continue
                fields = st[st.rfind(")") + 2:].split()
                ticks = int(fields[11]) + int(fields[12])
                cur[tid] = ticks
                if tid in self._threads_prev and dt > 0:
                    threads.append((name, 100.0 * (ticks - self._threads_prev[tid]) / tck / dt))
        except OSError:
            pass
        self._threads_prev = cur
        total = sum(cur.values())
        cpu = None
        if self._proc_prev is not None and dt > 0:
            cpu = 100.0 * (total - self._proc_prev) / tck / dt
        self._proc_prev = total
        rss = None
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss = int(line.split()[1]) / 1024.0
        except OSError:
            pass
        try:
            load1 = os.getloadavg()[0]
        except OSError:
            load1 = None
        threads.sort(key=lambda x: -x[1])
        return {"cpu_pct": cpu, "rss_mb": rss, "threads": len(cur), "load1": load1, "cores": os.cpu_count(),
                "top_threads": [[n, round(p, 1)] for n, p in threads[:8]]}

    def dump_stacks(self, reason):
        names = {t.ident: t.name for t in threading.enumerate()}
        out = [f"===== {datetime.now():%Y-%m-%d %H:%M:%S} node {self.node_id}: {reason}"]
        for tid, frame in sys._current_frames().items():
            out.append(f"--- thread {names.get(tid, tid)}")
            out.extend(line.rstrip("\n") for line in traceback.format_stack(frame))
        text = "\n".join(out) + "\n"
        try:
            with open(self._stack_path, "a") as f:
                f.write(text)
        except (OSError, TypeError):
            pass
        return self._stack_path, text

    def _event(self, severity, key, text, data=None, stacks=False):
        ev = {"t": time.time(), "severity": severity, "key": key, "text": text, "data": data or {}}
        if stacks:
            path, _ = self.dump_stacks(text)
            ev["stacks"] = path
        self._events.append(ev)
        self._new_events.append(ev)
        if self.verbose:
            print(f"[WATCHDOG] {severity.upper()}: {text}", flush=True)

    def _episode(self, key, active, severity, text, data=None, stacks=False):
        """Report a problem once when it starts; report again only after it has cleared."""
        if active and key not in self._active:
            self._active[key] = time.time()
            self._event(severity, key, text, data, stacks)
        elif not active and key in self._active:
            dur = time.time() - self._active.pop(key)
            self._event("info", key + "_cleared", f"cleared after {dur:.0f} s: {key}")

    def _snapshot_and_check(self, send=True):
        now = time.monotonic()
        dt = now - getattr(self, "_t_last_snap", now - self.period_s)
        self._t_last_snap = now
        blocks = {}
        for name, blk in list(_registry().items()):
            try:
                blocks[name] = blk.telemetry()
            except Exception as e:
                blocks[name] = {"error": repr(e)}
        snap = {"type": "snapshot", "t": time.time(), "node": self.node_id, "uptime_s": now - self._t0,
                "pid": os.getpid(), "proc": self._proc(dt), "late_ms_max": 1e3 * self._late_max, "blocks": blocks,
                "arq_mac": dict(self._mac)}
        self._late_max = 0.0
        self._check(snap)
        snap["events_new"], self._new_events = self._new_events, []
        snap["active_problems"] = sorted(self._active)
        self.snapshots += 1
        self._write_log(snap)
        if send:
            for s in list(self._clients):
                self._send(s, snap)
        return snap

    def _write_log(self, snap):
        try:
            if self._log is None or self._log.closed:
                self._log = open(self._log_path, "a")
            self._log.write(json.dumps(snap, default=str) + "\n")
            self._log.flush()
            if self._log.tell() > 50e6:
                self._log.close()
                os.replace(self._log_path, self._log_path + ".1")
                self._log = None
        except OSError:
            pass

    # ------------------------------------------------------------------ watchdog rules
    def _check(self, s):
        b, prev = s["blocks"], self._prev
        cs, ar, de, tp = b.get("csma", {}), b.get("arq", {}), b.get("deframer", {}), b.get("txprobe", {})
        age = cs.get("last_work_age_s")
        self._episode("rx_stream_stalled", age is not None and age > self.stall_s, "critical",
                      f"receive stream stopped: no samples from the radio for {age or 0:.1f} s (radio reset, "
                      f"USB/network link lost, or the flowgraph scheduler is blocked)", {"age_s": age}, stacks=True)
        pct = cs.get("rx_rate_pct")
        self._episode("samples_dropped", pct is not None and pct < 99.0, "warning",
                      f"only {pct or 0:.1f} % of the radio's samples arrive: the PC is not keeping up "
                      f"(top threads: {s['proc']['top_threads'][:3]})", {"pct": pct})
        self._episode("csma_tx_phase_stuck", cs.get("self_tx") and (cs.get("phase_age_s") or 0) > 1.0, "critical",
                      f"CSMA stuck in its own transmission (phase {cs.get('phase')}, "
                      f"{cs.get('phase_age_s', 0):.1f} s): receiver is off meanwhile", dict(cs), stacks=True)
        self._episode("csma_queue_waiting", (cs.get("queue") or 0) > 0 and (cs.get("oldest_queued_s") or 0) > 5,
                      "warning", f"{cs.get('queue')} frames wait in the CSMA queue, oldest {cs.get('oldest_queued_s', 0):.0f} s "
                      f"(channel busy {100 * (cs.get('busy_frac') or 0):.0f} %, floor {cs.get('floor_db')} dB)")
        tx_up = cs.get("tx", 0) > prev.get("csma_tx", cs.get("tx", 0))
        tp_up = tp.get("samples", 0) > prev.get("tp_samples", tp.get("samples", 0))
        self._episode("tx_path_stalled", bool(tp) and tx_up and not tp_up and (tp.get("last_age_s") or 0) > self.stall_s,
                      "critical", "CSMA released frames but no samples reach the radio sink: TX path stalled",
                      stacks=True)
        if cs.get("no_echo", 0) > prev.get("csma_no_echo", cs.get("no_echo", 0)):
            self._event("warning", "no_echo", "own burst not heard on own receiver (TX not transmitting?)")
        mu = cs.get("mute") or {}
        if mu.get("errors", 0) > prev.get("mute_errors", mu.get("errors", 0)):
            self._event("warning", "mute_errors", f"TX attenuation writes failing ({mu.get('errors')} so far)")
        self._episode("mute_off", bool(mu) and mu.get("enabled") and mu.get("failed"), "warning",
                      f"TX mute could not start ({mu.get('reason')}): own transmitter leaks into own receiver")
        la = ar.get("loop_age_s")
        self._episode("arq_thread_stalled", la is not None and la > 1.0, "critical",
                      f"ARQ worker thread has not run for {la or 0:.1f} s", stacks=True)
        for dest, p in (ar.get("tx") or {}).items():
            self._episode(f"arq_stuck_{dest}", (p.get("oldest_msg_s") or 0) > 30, "warning",
                          f"message to node {dest} open for {p.get('oldest_msg_s', 0):.0f} s (rto {p.get('rto_s')} s, "
                          f"cwnd {p.get('cwnd')}, {p.get('max_tries_now')} tries on the oldest segment)", p)
        self._episode("deframer_waiting", (de.get("waiting_s") or 0) > 2.0, "warning",
                      f"deframer has waited {de.get('waiting_s', 0):.1f} s for the rest of a frame "
                      f"({de.get('buffer_bits')} bits buffered): a frame tail is held up")
        lm = s["late_ms_max"]
        self._episode("python_stall", lm > 250, "warning",
                      f"Python threads were blocked for {lm:.0f} ms (GIL held by a busy block; top threads: "
                      f"{s['proc']['top_threads'][:3]})")
        hot = [t for t in s["proc"]["top_threads"] if t[1] > 90]
        self._episode("hot_thread", bool(hot), "warning", f"thread at {hot[0][1] if hot else 0:.0f} % of a core: "
                      f"{hot[0][0] if hot else ''} is the bottleneck")
        self._prev = {"csma_tx": cs.get("tx", 0), "tp_samples": tp.get("samples", 0),
                      "csma_no_echo": cs.get("no_echo", 0), "mute_errors": mu.get("errors", 0)}
