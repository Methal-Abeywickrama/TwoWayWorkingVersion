"""
Embedded Python Block: CSMA/CA MAC with TX mute  (carrier sense + slotted random backoff, single frequency)

Wiring
  RX  : Pluto Source  -> in0 (this block) -> out0 -> Power Squelch -> ... (rest of the RX chain)
  TX  : Packet Framer TX pdu_out -> pdu_in (this block) ;  pdu_out -> PDU to Tagged Stream -> modulator -> Pluto Sink
  ctrl: optional messages "fail"/"collision" (double the contention window), "ok" (reset it), "flush" (empty the queue)

TX mute (tx_mute = 1)
  On one frequency the Pluto's own transmitter, powered but idle, leaks its carrier (and the noise around it)
  into its own receiver and raises the noise floor. This block keeps the TX attenuation at mute_att_db
  (89.75 dB = practically off) while idle and sets it to tx_att_db only around its own bursts:
      backoff done -> receiver off -> TX attenuation := tx_att_db -> (write confirmed) -> frame released
      -> RX-off window ends -> TX attenuation := mute_att_db -> (write confirmed) -> receiver on
  The attenuation is written through libiio (the C library GNU Radio's Pluto blocks use, via ctypes; falls back
  to the python 'iio' module) from a worker thread, so the stream never waits on the network. If neither is
  available, or the radio can't be opened at iio_uri, muting is switched off and the block works as v6.2.
  tx_att_db and tx_mute can be changed while running (slider / check box). The attenuation is re-written once a
  second, so a value written by the Pluto Sink's own slider callback is overridden within 1 s.
  With the mute on, the leak appears in our own receiver while we send, so our own echo is only accepted when it
  is echo_margin_db above the noise floor, and the RX-off window never uses less than tx_delay_s.
  If the write is not confirmed within unmute_timeout_s, the frame goes anyway (counted as "late").

Carrier sense (spike-proof)
  Every nfft samples: FFT, MEDIAN power of the bins inside +/- cs_bw_hz/2 (bins within dc_exclude_hz of 0 Hz left
  out), smoothed over sense_s, compared with a tracked noise floor:
      busy  when  cs > floor + cs_margin_db ;   idle again when  cs < floor + cs_margin_db - cs_hyst_db
  Floor = floor_percentile of cs over floor_window_s (own transmissions excluded); falls at once, rises at most
  floor_rise_db_s dB/s.

Access (CSMA/CA, as in 802.11 DCF without ACKs)
  Before every frame: backoff of 0 .. cw-1 slots. Slot boundaries: quiet start + difs_s, + slot_s, ... Each
  boundary reached while idle uses up one slot; the frame goes at the boundary where the count reaches zero.
  Busy freezes the count; after it, DIFS again. "fail" on ctrl doubles cw up to cw_max, "ok" resets it.

Own transmission (half duplex): receiver off from the release decision until
      release + tx_delay + airtime + gate_guard_s     (+ the time the mute write takes, if muting)
  tx_delay = measured echo delay (median, once 3 bursts were seen, if auto_delay = 1), else tx_delay_s.
  gate_mode = 0 (no mute only): wait until the own echo has ended instead.

All timing runs on the RX sample clock. The stats line also shows how many samples per second really arrive
(below ~99 % the PC is not keeping up) and the CPU share of this block.
Read-out for QT widgets (Function Probe): cs_db(), threshold_db(), floor_db(), is_busy(), is_tx(), queue_len(), is_muted()
"""
import collections
import os
import threading
import time

import numpy as np
import pmt
from gnuradio import gr


def _cdp_register(name, blk):
    """Shared registry (one per flowgraph process) read by the Telemetry block."""
    import sys as _sys, types as _types
    reg = _sys.modules.get("cdp_registry")
    if reg is None:
        reg = _types.ModuleType("cdp_registry")
        reg.blocks = {}
        _sys.modules["cdp_registry"] = reg
    reg.blocks[name] = blk


class TxMuter:
    """Writes the Pluto TX attenuation (ad9361-phy, output voltage0, hardwaregain) from a worker thread."""

    def __init__(self, uri, verbose=1):
        self.uri = str(uri)
        self.verbose = int(verbose)
        self.ok = False
        self.failed = False
        self.reason = "connecting"
        self.backend = ""
        self.errors = 0
        self.writes = 0
        self.write_ms = collections.deque(maxlen=200)
        self._cv = threading.Condition()
        self._want = None
        self._seq = 0
        self._done_seq = 0
        self._stop = False
        self._last_write = 0.0
        self._write = None
        self._close = None
        self._thr = None

    # ---- called from the GNU Radio thread
    def start(self):
        self._thr = threading.Thread(target=self._run, name="tx-mute", daemon=True)
        self._thr.start()

    def request(self, att_db):
        with self._cv:
            self._want = float(att_db)
            self._seq += 1
            self._cv.notify()
            return self._seq

    def done(self, seq):
        return self._done_seq >= seq

    def stop(self, restore_att=None):
        with self._cv:
            self._stop = True
            self._cv.notify()
        if self._thr is not None:
            self._thr.join(timeout=2.0)
        if self.ok and restore_att is not None:
            try:
                self._write(float(restore_att))
            except Exception:
                pass
        if self._close:
            try:
                self._close()
            except Exception:
                pass

    # ---- worker thread
    def _open(self):
        errs = []
        try:
            import ctypes
            import ctypes.util
            name = ctypes.util.find_library("iio") or "libiio.so.0"
            lib = ctypes.CDLL(name)
            vp, cp = ctypes.c_void_p, ctypes.c_char_p
            lib.iio_create_context_from_uri.restype = vp
            lib.iio_create_context_from_uri.argtypes = [cp]
            lib.iio_context_find_device.restype = vp
            lib.iio_context_find_device.argtypes = [vp, cp]
            lib.iio_device_find_channel.restype = vp
            lib.iio_device_find_channel.argtypes = [vp, cp, ctypes.c_bool]
            lib.iio_channel_attr_write.restype = ctypes.c_ssize_t
            lib.iio_channel_attr_write.argtypes = [vp, cp, cp]
            lib.iio_context_set_timeout.restype = ctypes.c_int
            lib.iio_context_set_timeout.argtypes = [vp, ctypes.c_uint]
            lib.iio_context_destroy.restype = None
            lib.iio_context_destroy.argtypes = [vp]
            ctx = lib.iio_create_context_from_uri(self.uri.encode())
            if not ctx:
                raise RuntimeError(f"cannot open {self.uri}")
            lib.iio_context_set_timeout(ctx, 1000)
            dev = lib.iio_context_find_device(ctx, b"ad9361-phy")
            chn = lib.iio_device_find_channel(dev, b"voltage0", True) if dev else None
            if not chn:
                lib.iio_context_destroy(ctx)
                raise RuntimeError("ad9361-phy output voltage0 not found")

            def write(att):
                r = lib.iio_channel_attr_write(chn, b"hardwaregain", f"{-att:.2f}".encode())
                if r < 0:
                    raise OSError(-r, os.strerror(-r))

            self._write, self._close = write, (lambda: lib.iio_context_destroy(ctx))
            self.backend = f"libiio C library ({name})"
            return
        except Exception as e:
            errs.append(f"libiio C library: {e}")
        try:
            import iio
            ctx = iio.Context(self.uri)
            dev = ctx.find_device("ad9361-phy")
            chn = dev.find_channel("voltage0", True) if dev else None
            if chn is None:
                raise RuntimeError("ad9361-phy output voltage0 not found")
            attr = chn.attrs["hardwaregain"]

            def write(att):
                attr.value = f"{-att:.2f}"

            self._write, self._close = write, None
            self._ctx_keepalive = ctx
            self.backend = "python iio module"
            return
        except Exception as e:
            errs.append(f"python iio module: {e}")
        raise RuntimeError("; ".join(errs))

    def _run(self):
        try:
            self._open()
        except Exception as e:
            self.failed, self.reason = True, str(e)
            print(f"[CSMA] TX mute OFF: could not control the radio's TX attenuation ({e}). Install libiio "
                  f"(Fedora: sudo dnf install libiio python3-libiio) or check iio_uri. Running without mute.", flush=True)
            return
        self.ok, self.reason = True, "ok"
        if self.verbose:
            print(f"[CSMA] TX mute ready ({self.backend}, {self.uri})", flush=True)
        while True:
            with self._cv:
                while (not self._stop and self._seq == self._done_seq
                       and time.monotonic() - self._last_write < 1.0):
                    self._cv.wait(timeout=0.25)
                if self._stop:
                    return
                seq, att = self._seq, self._want
            if att is not None:
                t = time.perf_counter()
                try:
                    self._write(att)
                    self.writes += 1
                    self.write_ms.append(1e3 * (time.perf_counter() - t))
                except Exception as e:
                    self.errors += 1
                    if self.errors <= 3:
                        print(f"[CSMA] TX attenuation write failed: {e}", flush=True)
            self._last_write = time.monotonic()
            with self._cv:
                self._done_seq = max(self._done_seq, seq)


class CsmaMac(gr.sync_block):
    def __init__(self, samp_rate=1.5e6, samples_per_byte=32, slot_s=0.015, difs_s=0.020,
                 cw_min=8, cw_max=128, cs_margin_db=6.0, cs_hyst_db=2.0, cs_bw_hz=400e3,
                 dc_exclude_hz=10e3, nfft=256, sense_s=0.001, floor_window_s=2.0,
                 floor_percentile=10.0, floor_rise_db_s=1.0, echo_timeout_s=0.100, tail_s=0.002,
                 blank_rx=1, max_queue=64, max_defer_s=0.0, verbose=1, stats_every_s=10.0,
                 gate_mode=1, tx_delay_s=0.012, auto_delay=1, gate_guard_s=0.005,
                 tx_mute=1, iio_uri="ip:192.168.1.10", tx_att_db=20.0, mute_att_db=89.75,
                 unmute_timeout_s=0.1, echo_margin_db=25.0):
        gr.sync_block.__init__(self, name="CSMA/CA MAC + TX mute", in_sig=[np.complex64], out_sig=[np.complex64])
        self.samp_rate = float(samp_rate)
        self.samples_per_byte = float(samples_per_byte)
        self.slot_s = float(slot_s)
        self.difs_s = float(difs_s)
        self.cw_min = max(1, int(cw_min))
        self.cw_max = max(self.cw_min, int(cw_max))
        self.cs_margin_db = float(cs_margin_db)
        self.cs_hyst_db = float(cs_hyst_db)
        self.floor_percentile = float(floor_percentile)
        self.floor_rise_db_s = float(floor_rise_db_s)
        self.echo_timeout_s = float(echo_timeout_s)
        self.tail_s = float(tail_s)
        self.blank_rx = int(blank_rx)
        self.gate_mode = int(gate_mode)
        self.tx_delay_s = float(tx_delay_s)
        self.auto_delay = int(auto_delay)
        self.gate_guard_s = float(gate_guard_s)
        self._sense_s = float(sense_s)
        self.max_queue = max(1, int(max_queue))
        self.max_defer_s = float(max_defer_s)
        self.verbose = int(verbose)
        self.stats_every_s = float(stats_every_s)
        # TX mute (tx_mute and tx_att_db are plain attributes: GRC sliders / check boxes update them live)
        self.tx_mute = int(tx_mute)
        self.iio_uri = str(iio_uri)
        self.tx_att_db = float(tx_att_db)
        self.mute_att_db = float(mute_att_db)
        self.unmute_timeout_s = float(unmute_timeout_s)
        self.echo_margin_db = float(echo_margin_db)
        self._muter = None
        self._req_att = None
        self._mute_seq = 0
        self._seen = (None, None)
        self._unmute_delays = collections.deque(maxlen=200)

        # ---- carrier-sense front end
        self._nfft = int(nfft)
        self._dt = self._nfft / self.samp_rate
        f = np.fft.fftfreq(self._nfft, 1.0 / self.samp_rate)
        sel = (np.abs(f) <= float(cs_bw_hz) / 2) & (np.abs(f) >= float(dc_exclude_hz))
        self._bins = np.flatnonzero(sel)
        if self._bins.size < 8:
            raise ValueError("cs_bw_hz / dc_exclude_hz / nfft leave fewer than 8 bins to sense")
        self._win = np.hanning(self._nfft).astype(np.float32)
        self._norm = 1.0 / (np.log(2.0) * self._nfft * float(np.mean(self._win ** 2)))
        self._smooth = collections.deque(maxlen=max(1, int(round(float(sense_s) / self._dt))))
        self._carry = np.zeros(0, np.complex64)
        self._cs_db = -200.0
        n_hist = max(64, int(float(floor_window_s) / self._dt))
        self._hist = collections.deque(maxlen=n_hist)
        self._floor_db = None
        self._floor_next = 0.0
        self._busy = True

        # ---- MAC state
        self._lock = threading.Lock()
        self._queue = collections.deque()
        self._cw = self.cw_min
        self._backoff = None
        self._idle_run = 0.0
        self._rng = np.random.default_rng()
        self._n_abs = 0
        self._t = 0.0
        self._self_tx = False
        self._tx = {}

        # ---- statistics
        self._st = collections.Counter()
        self._echo_delays = collections.deque(maxlen=200)
        self._access_delays = collections.deque(maxlen=200)
        self._busy_time = 0.0
        self._stat_t0 = 0.0
        self._warned_no_echo = False
        self._wall0 = None
        self._n_wall0 = 0
        self._cpu = 0.0
        self._windows = 0
        self._last_work = 0.0                 # telemetry: PC time of the last work() call
        self._busy_total = 0.0
        self._tel_prev = None
        self._cpu_total = 0.0

        self.message_port_register_in(pmt.intern("pdu_in"))
        self.set_msg_handler(pmt.intern("pdu_in"), self._on_pdu)
        self.message_port_register_in(pmt.intern("ctrl"))
        self.set_msg_handler(pmt.intern("ctrl"), self._on_ctrl)
        self.message_port_register_out(pmt.intern("pdu_out"))

    # ------------------------------------------------------------------ start / stop (radio access only here,
    # never in __init__: GRC builds the block once just to read its ports)
    def start(self):
        self._muter = TxMuter(self.iio_uri, self.verbose)
        self._muter.start()
        _cdp_register("csma", self)
        return True

    def telemetry(self):
        """Snapshot for the Telemetry block (called from its thread, read-only)."""
        now = time.monotonic()
        prev, cur = self._tel_prev, (now, self._n_abs, self._t, self._busy_total, self._cpu_total)
        self._tel_prev = cur
        rate = busy = cpu = None
        if prev is not None and cur[0] > prev[0]:
            rate = (cur[1] - prev[1]) / (cur[0] - prev[0])
            dts = cur[2] - prev[2]
            busy = (cur[3] - prev[3]) / dts if dts > 0 else None
            cpu = (cur[4] - prev[4]) / (cur[0] - prev[0])
        with self._lock:
            q = len(self._queue)
            oldest = (self._t - self._queue[0][1]) if q else 0.0
        ad, ed = list(self._access_delays), list(self._echo_delays)
        m = self._muter
        tx = self._tx
        phase_t = tx.get("t_dec", tx.get("t0")) if self._self_tx else None
        return {
            "floor_db": self._floor_db, "cs_db": float(self._cs_db), "margin_db": self.cs_margin_db,
            "busy": bool(self._busy), "busy_frac": busy, "self_tx": bool(self._self_tx), "phase": tx.get("phase"),
            "phase_age_s": (self._t - phase_t) if phase_t is not None else 0.0,
            "tx": self._st["tx"], "deferrals": self._st["deferrals"], "dropped": self._st["dropped"],
            "forced": self._st["forced"], "no_echo": self._st["no_echo"], "late_unmute": self._st["late_unmute"],
            "queue": q, "oldest_queued_s": oldest, "cw": self._cw,
            "access_med_ms": 1e3 * float(np.median(ad)) if ad else None,
            "access_p95_ms": 1e3 * float(np.percentile(ad, 95)) if ad else None,
            "echo_med_ms": 1e3 * float(np.median(ed)) if ed else None,
            "echo_max_ms": 1e3 * float(max(ed)) if ed else None, "tx_delay_ms": 1e3 * self._tx_delay(),
            "slot_ms": 1e3 * self.slot_s, "difs_ms": 1e3 * self.difs_s, "guard_ms": 1e3 * self.gate_guard_s,
            "rx_rate": rate, "rx_rate_pct": 100.0 * rate / self.samp_rate if rate else None, "cpu_frac": cpu,
            "last_work_age_s": (now - self._last_work) if self._last_work else None,
            "mute": {"enabled": bool(self.tx_mute), "ok": bool(m and m.ok), "failed": bool(m and m.failed),
                     "reason": m.reason if m else "not started", "errors": m.errors if m else 0,
                     "writes": m.writes if m else 0,
                     "write_med_ms": float(np.median(list(m.write_ms))) if m and m.write_ms else None,
                     "write_max_ms": float(max(m.write_ms)) if m and m.write_ms else None,
                     "tx_att_db": self.tx_att_db, "idle_att_db": self.mute_att_db},
        }

    def stop(self):
        if self._muter is not None:
            self._muter.stop(restore_att=self.tx_att_db)
        return True

    # ------------------------------------------------------------------ read-outs (Function Probe)
    def cs_db(self):
        return float(self._cs_db)

    def floor_db(self):
        return float(self._floor_db) if self._floor_db is not None else -200.0

    def threshold_db(self):
        return self.floor_db() + self.cs_margin_db

    def is_busy(self):
        return int(self._busy or self._self_tx)

    def is_tx(self):
        return int(self._self_tx)

    def queue_len(self):
        return len(self._queue)

    def is_muted(self):
        return int(self._mute_on() and self._req_att == self.mute_att_db)

    # ------------------------------------------------------------------ TX mute helpers
    def _mute_on(self):
        return bool(self.tx_mute) and self._muter is not None and self._muter.ok

    def _desired_att(self):
        if self._muter is None or not self._muter.ok:
            return None
        if not self.tx_mute:
            return self.tx_att_db
        if self._self_tx and self._tx.get("phase") in ("unmute", "air"):
            return self.tx_att_db
        return self.mute_att_db

    def _sync_mute(self, force=False):
        att = self._desired_att()
        if att is not None and (force or att != self._req_att):
            self._mute_seq = self._muter.request(att)
            self._req_att = att
        return self._mute_seq

    # ------------------------------------------------------------------ messages
    def _on_pdu(self, msg):
        if not pmt.is_pair(msg) or not pmt.is_u8vector(pmt.cdr(msg)):
            print("[CSMA] pdu_in expects a PDU (meta . u8vector) from the framer; message ignored", flush=True)
            return
        with self._lock:
            if len(self._queue) >= self.max_queue:
                self._queue.popleft()
                self._st["dropped"] += 1
                print(f"[CSMA] queue full ({self.max_queue}): oldest frame dropped", flush=True)
            self._queue.append((msg, self._t))

    def _on_ctrl(self, msg):
        try:
            txt = str(pmt.to_python(pmt.car(msg) if pmt.is_pair(msg) else msg)).lower()
        except Exception:
            txt = ""
        if "fail" in txt or "collision" in txt or "nack" in txt:
            self._cw = min(self.cw_max, self._cw * 2)
        elif "ok" in txt or "success" in txt or "ack" in txt:
            self._cw = self.cw_min
        elif "flush" in txt:
            with self._lock:
                self._queue.clear()

    # ------------------------------------------------------------------ carrier sense helpers
    def _update_floor(self):
        if len(self._hist) < self._hist.maxlen // 4:
            return
        raw = float(np.percentile(np.fromiter(self._hist, float, len(self._hist)), self.floor_percentile))
        if self._floor_db is None or raw <= self._floor_db:
            self._floor_db = raw
        else:
            self._floor_db = min(raw, self._floor_db + self.floor_rise_db_s * 0.05)

    # ------------------------------------------------------------------ own transmission
    def _release(self):
        """Backoff done: receiver off; unmute the TX first if muting, then send."""
        with self._lock:
            if not self._queue:
                return
            msg, t_enq = self._queue.popleft()
        nbytes = pmt.length(pmt.cdr(msg))
        airtime = nbytes * self.samples_per_byte / self.samp_rate
        self._backoff, self._idle_run = None, 0.0
        self._self_tx = True
        if self._mute_on():
            self._tx = {"phase": "unmute", "msg": msg, "nbytes": nbytes, "air": airtime, "t_enq": t_enq,
                        "t_dec": self._t, "mute": True}
            self._tx["seq"] = self._sync_mute()
        else:
            self._tx = {"mute": False}
            self._publish(msg, nbytes, airtime, t_enq)

    def _publish(self, msg, nbytes, airtime, t_enq):
        self.message_port_pub(pmt.intern("pdu_out"), msg)
        delay = self._tx_delay()
        self._tx.update({"phase": "air", "t0": self._t, "air": airtime, "echo_t": None, "low_since": None,
                         "hard_end": self._t + self.echo_timeout_s + airtime + self.tail_s + 0.02,
                         "win_end": self._t + delay + airtime + self.gate_guard_s})
        self._tx.pop("msg", None)
        self._st["tx"] += 1
        self._access_delays.append(self._t - t_enq)
        if self.verbose >= 2:
            print(f"[CSMA] TX {nbytes} B frame ({airtime * 1e3:.1f} ms) after {1e3 * (self._t - t_enq):.1f} ms "
                  f"in queue, cw {self._cw}", flush=True)

    def _tx_delay(self):
        if self.auto_delay and len(self._echo_delays) >= 3:
            d = float(np.median(self._echo_delays))
            # with the mute on, the leak can fake an early echo: never go below tx_delay_s then
            return max(d, self.tx_delay_s) if self._mute_on() else d
        return self.tx_delay_s

    def _end_self_tx(self, reason):
        self._self_tx = False
        if reason == "echo_end":
            self._idle_run = self.tail_s
        elif reason == "window":
            # our burst ended at about win_end - guard; the other node noticed it about sense_s later. Count idle
            # time from there (including any wait for the mute write) so both slot grids line up.
            # A long wait (> 5 ms) is not counted as idle: the other node may have started meanwhile, unseen, so
            # start a fresh DIFS instead.
            extra = max(0.0, self._t - self._tx.get("win_end", self._t))
            self._idle_run = self.gate_guard_s - self._sense_s + extra if extra <= 0.005 else 0.0
        else:
            self._idle_run = 0.0
        if reason == "timeout" and self._tx.get("echo_t") is None:
            self._st["no_echo"] += 1
            if not self._warned_no_echo:
                self._warned_no_echo = True
                print("[CSMA] own burst not seen on our receiver: RX blanking falls back to the timer "
                      "(echo_timeout_s + airtime). Check the TX, or raise echo_timeout_s.", flush=True)
        self._tx = {}

    # ------------------------------------------------------------------ one sense step (nfft samples)
    def _step(self, cs_db):
        dt = self._dt
        thr_on = (self._floor_db if self._floor_db is not None else 1e9) + self.cs_margin_db
        loud = cs_db > thr_on

        if self._self_tx:
            tx = self._tx
            phase = tx.get("phase")
            if phase == "unmute":                            # waiting for the TX attenuation write
                if self._muter.done(tx["seq"]) or self._t - tx["t_dec"] >= self.unmute_timeout_s:
                    if not self._muter.done(tx["seq"]):
                        self._st["late_unmute"] += 1
                    self._unmute_delays.append(self._t - tx["t_dec"])
                    self._publish(tx["msg"], tx["nbytes"], tx["air"], tx["t_enq"])
                return
            if phase == "mute":                              # waiting for the mute write before listening again
                if self._muter.done(tx["seq"]) or self._t - tx["win_end"] >= self.unmute_timeout_s:
                    self._end_self_tx("window")
                return
            if self.gate_mode == 1 or tx.get("mute"):        # fixed receive-off window
                echo_thr = thr_on if not tx.get("mute") else \
                    (self._floor_db if self._floor_db is not None else 1e9) + max(self.cs_margin_db, self.echo_margin_db)
                if (tx["echo_t"] is None and cs_db > echo_thr and self._t < tx["win_end"]
                        and self._t - tx["t0"] >= 0.002):    # < 2 ms is not our burst (leak / noise)
                    tx["echo_t"] = self._t
                    self._echo_delays.append(self._t - tx["t0"])
                if self._t >= tx["win_end"]:
                    if tx["echo_t"] is None:
                        self._st["no_echo"] += 1
                    if self._mute_on():
                        tx["phase"] = "mute"
                        tx["seq"] = self._sync_mute()
                    else:
                        self._end_self_tx("window")
                return
            # gate_mode 0 (no mute): wait until our own echo has ended
            if tx["echo_t"] is None:
                if loud:
                    tx["echo_t"] = self._t
                    self._echo_delays.append(self._t - tx["t0"])
                elif self._t - tx["t0"] > self.echo_timeout_s + tx["air"] + self.tail_s:
                    self._end_self_tx("timeout")
                    return
            else:
                if loud:
                    tx["low_since"] = None
                elif tx["low_since"] is None:
                    tx["low_since"] = self._t
                elif self._t - tx["low_since"] >= self.tail_s:
                    self._end_self_tx("echo_end")
                    return
            if self._t >= tx["hard_end"]:
                self._end_self_tx("timeout")
            return

        # channel state with hysteresis (busy until the floor is known)
        self._hist.append(cs_db)
        if self._floor_db is None:
            self._busy = True
        elif self._busy:
            self._busy = cs_db > thr_on - self.cs_hyst_db
        else:
            self._busy = loud
        if self._busy:
            self._busy_time += dt
            self._busy_total += dt

        with self._lock:
            pending = len(self._queue)
            t_head = self._queue[0][1] if pending else None
        if not pending:
            self._idle_run = 0.0 if self._busy else self._idle_run + dt
            return

        if self.max_defer_s > 0 and self._t - t_head > self.max_defer_s and self._floor_db is not None:
            self._st["forced"] += 1
            self._release()
            return

        if self._backoff is None:
            self._backoff = int(self._rng.integers(0, self._cw))

        if self._busy:
            if self._idle_run > 0.0:
                self._st["deferrals"] += 1
            self._idle_run = 0.0
            return

        p_prev = self._idle_run - self.difs_s
        self._idle_run += dt
        p_now = self._idle_run - self.difs_s
        if p_now < 0:
            return
        crossings = int(np.floor(p_now / self.slot_s)) - int(np.floor(p_prev / self.slot_s))
        for _ in range(crossings):
            if self._backoff == 0:
                self._release()
                return
            self._backoff -= 1

    # ------------------------------------------------------------------ stream
    def work(self, input_items, output_items):
        t_cpu = time.perf_counter()
        self._last_work = time.monotonic()
        if self._wall0 is None:
            self._wall0, self._n_wall0 = time.monotonic(), self._n_abs
        if self._muter is not None:
            seen = (self.tx_mute, self.tx_att_db)
            self._sync_mute(force=(seen != self._seen))      # slider / check box changed: write again
            self._seen = seen
        x = input_items[0]
        out = output_items[0]
        n = len(x)
        out[:] = x

        first_abs = self._n_abs - len(self._carry)
        buf = np.concatenate((self._carry, x)) if len(self._carry) else x
        nb = len(buf) // self._nfft
        blank = []
        if nb:
            blocks = buf[:nb * self._nfft].reshape(nb, self._nfft) * self._win
            p = np.abs(np.fft.fft(blocks, axis=1)[:, self._bins]) ** 2
            med = np.median(p, axis=1) * self._norm
            for i in range(nb):
                self._smooth.append(float(med[i]))
                self._cs_db = 10.0 * np.log10(max(sum(self._smooth) / len(self._smooth), 1e-20))
                self._t = (first_abs + (i + 1) * self._nfft) / self.samp_rate
                was_tx = self._self_tx
                self._step(self._cs_db)
                if self.blank_rx and (was_tx or self._self_tx):
                    s = first_abs + i * self._nfft
                    blank.append((s, s + self._nfft))
                if self._t >= self._floor_next:
                    self._floor_next = self._t + 0.05
                    self._update_floor()
        self._carry = buf[nb * self._nfft:].copy()
        self._n_abs += n

        if self.blank_rx and self._self_tx:
            blank.append((self._n_abs - len(self._carry), self._n_abs))
        x0 = self._n_abs - n
        for s, e in blank:
            a, b = max(s, x0) - x0, min(e, self._n_abs) - x0
            if b > a:
                out[a:b] = 0

        dcpu = time.perf_counter() - t_cpu
        self._cpu += dcpu
        self._cpu_total += dcpu
        if self.verbose and self.stats_every_s > 0 and self._t - self._stat_t0 >= self.stats_every_s:
            self._print_stats()
        return n

    def _print_stats(self):
        span = max(1e-9, self._t - self._stat_t0)
        ed = list(self._echo_delays)
        ad = list(self._access_delays)
        echo = (f"own echo after {1e3 * np.median(ed):.1f} ms (max {1e3 * max(ed):.1f}) -> keep slot_s above it"
                if ed else "own echo not measured yet")
        if self.gate_mode == 1 or self._mute_on():
            echo += f" | RX off for {1e3 * self._tx_delay():.1f} ms + airtime + {1e3 * self.gate_guard_s:.1f} ms"
        floor = f"{self._floor_db:.1f}" if self._floor_db is not None else "learning"
        print(f"[CSMA] floor {floor} dB, threshold +{self.cs_margin_db:g} dB, now {self._cs_db:.1f} dB, "
              f"busy {100 * self._busy_time / span:.1f} % | tx {self._st['tx']}, deferrals {self._st['deferrals']}, "
              f"queue {len(self._queue)}, cw {self._cw}, dropped {self._st['dropped']}, forced {self._st['forced']}, "
              f"no-echo {self._st['no_echo']} | access delay median "
              f"{(1e3 * np.median(ad)) if ad else 0:.0f} ms | {echo}", flush=True)
        m = self._muter
        if m is None or m.failed:
            mute = f"TX mute OFF ({m.reason if m else 'not started'})"
        elif not m.ok:
            mute = "TX mute: connecting to the radio"
        elif not self.tx_mute:
            mute = f"TX mute switched off: TX always at {self.tx_att_db:g} dB"
        else:
            wm = list(m.write_ms)
            ud = list(self._unmute_delays)
            mute = (f"TX mute on: idle {self.mute_att_db:g} dB, sending {self.tx_att_db:g} dB | attenuation write "
                    f"{np.median(wm) if wm else 0:.1f} ms median (max {max(wm) if wm else 0:.1f}), unmute wait "
                    f"{1e3 * np.median(ud) if ud else 0:.1f} ms median, late {self._st['late_unmute']}, "
                    f"write errors {m.errors}")
        wall = max(1e-9, time.monotonic() - self._wall0)
        rate = (self._n_abs - self._n_wall0) / wall
        share = 100.0 * rate / self.samp_rate
        print(f"[CSMA] {mute} | radio delivers {rate / 1e6:.4f} Msps = {share:.1f} % of samp_rate | this block "
              f"uses {100.0 * self._cpu / wall:.0f} % of one CPU core", flush=True)
        self._windows += 1
        if self._windows >= 2 and share < 99.0:
            print(f"[CSMA] WARNING: only {share:.1f} % of the samples arrive. The PC is not keeping up and the radio "
                  f"drops samples, which breaks frames (train word found, CRC fails). Close GUI sinks you do not "
                  f"need, run without the GUI, or lower samp_rate.", flush=True)
        self._wall0, self._n_wall0, self._cpu = time.monotonic(), self._n_abs, 0.0
        self._stat_t0 = self._t
        self._busy_time = 0.0
