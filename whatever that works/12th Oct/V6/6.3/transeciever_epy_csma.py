"""
Embedded Python Block: CSMA/CA MAC  (carrier sense + slotted random backoff, single frequency)

Wiring
  RX  : Pluto Source  -> in0 (this block) -> out0 -> Power Squelch -> ... (rest of the RX chain)
  TX  : Packet Framer TX pdu_out -> pdu_in (this block) ;  pdu_out -> PDU to Tagged Stream -> modulator -> Pluto Sink
  ctrl: optional messages "fail"/"collision" (double the contention window), "ok" (reset it), "flush" (empty the queue)

Carrier sense (spike-proof)
  Every nfft samples the block takes an FFT and uses the MEDIAN power of the bins inside +/- cs_bw_hz/2
  (bins within dc_exclude_hz of 0 Hz are left out). A data burst raises every in-band bin, so the median
  rises; a narrow spike (LO leakage / carrier left after a burst) lifts one or two bins and does not move the
  median. The median is smoothed over sense_s and compared with a tracked noise floor:
      busy  when  cs > floor + cs_margin_db ;   idle again when  cs < floor + cs_margin_db - cs_hyst_db
  The floor is the floor_percentile of cs over the last floor_window_s (own transmissions excluded); it falls
  at once but rises at most floor_rise_db_s dB/s, so traffic cannot drag it up.

Access (CSMA/CA, as in 802.11 DCF without ACKs)
  Before every frame: draw a backoff of 0 .. cw-1 slots. Slot boundaries are counted from the moment the
  channel went quiet: quiet start + difs_s, + slot_s, + 2*slot_s ... Each boundary reached while idle uses up
  one slot; the frame is released at the boundary where the count reaches zero. A busy channel freezes the
  count; after it, DIFS again and the count continues. Because both nodes anchor their boundaries on the same
  burst end, two frames only collide if they picked the same boundary. cw starts at cw_min; "fail" messages
  on ctrl double it up to cw_max, "ok" resets it (wire these from the ARQ layer if available).

Own transmission (half duplex): receiver off while we send
  From the moment a frame is released the receiver is "off": no sensing, no floor update, and (blank_rx = 1)
  the RX output is zeroed so the squelch / AGC / FLL / equalizer / deframer never see our own burst.
  gate_mode = 1 (default): the receiver comes back at a fixed time,
        release + tx_delay + airtime + gate_guard_s
     where tx_delay is the TX + RX latency of the radios (tx_delay_s, replaced by the measured echo delay once
     3 own bursts have been seen, if auto_delay = 1). Nothing waits for the echo to end.
  gate_mode = 0: the receiver comes back when our own echo has ended (+ tail_s), or after
     echo_timeout_s + airtime + tail_s if no echo is seen.
  The echo delay = the window in which both nodes can still collide; slot_s must be longer than it.
  The measured value is printed in the stats line.

All timing runs on the RX sample clock (samples / samp_rate), not on the PC clock.
The stats line also shows how many samples per second really arrive from the radio (PC clock). Below ~99 % of
samp_rate the PC is not keeping up and the radio drops samples: frames then fail CRC although the train word is
found. It also shows how much of one CPU core this block uses.
Read-out for QT widgets (Function Probe): cs_db(), threshold_db(), floor_db(), is_busy(), is_tx(), queue_len()
"""
import collections
import threading
import time

import numpy as np
import pmt
from gnuradio import gr


class CsmaMac(gr.sync_block):
    def __init__(self, samp_rate=1.5e6, samples_per_byte=32, slot_s=0.015, difs_s=0.020,
                 cw_min=8, cw_max=128, cs_margin_db=6.0, cs_hyst_db=2.0, cs_bw_hz=400e3,
                 dc_exclude_hz=10e3, nfft=256, sense_s=0.001, floor_window_s=2.0,
                 floor_percentile=10.0, floor_rise_db_s=1.0, echo_timeout_s=0.100, tail_s=0.002,
                 blank_rx=1, max_queue=64, max_defer_s=0.0, verbose=1, stats_every_s=10.0,
                 gate_mode=1, tx_delay_s=0.009, auto_delay=1, gate_guard_s=0.002):
        gr.sync_block.__init__(self, name="CSMA/CA MAC", in_sig=[np.complex64], out_sig=[np.complex64])
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

        # ---- carrier-sense front end
        self._nfft = int(nfft)
        self._dt = self._nfft / self.samp_rate                          # time per FFT block
        f = np.fft.fftfreq(self._nfft, 1.0 / self.samp_rate)
        sel = (np.abs(f) <= float(cs_bw_hz) / 2) & (np.abs(f) >= float(dc_exclude_hz))
        self._bins = np.flatnonzero(sel)
        if self._bins.size < 8:
            raise ValueError("cs_bw_hz / dc_exclude_hz / nfft leave fewer than 8 bins to sense")
        self._win = np.hanning(self._nfft).astype(np.float32)
        # median of |X|^2 of white noise = ln2 * nfft * mean(w^2) * power  -> scale to "white-equivalent" power
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
        self._n_abs = 0                       # absolute input sample index of the next new sample
        self._t = 0.0                         # current time on the RX sample clock, s
        self._self_tx = False
        self._tx = {}

        # ---- statistics
        self._st = collections.Counter()
        self._echo_delays = collections.deque(maxlen=200)
        self._access_delays = collections.deque(maxlen=200)
        self._busy_time = 0.0
        self._stat_t0 = 0.0
        self._warned_no_echo = False
        self._wall0 = None                    # PC clock at the start of the stats window
        self._n_wall0 = 0                     # samples received before that
        self._cpu = 0.0                       # seconds spent in work() in this stats window
        self._windows = 0

        self.message_port_register_in(pmt.intern("pdu_in"))
        self.set_msg_handler(pmt.intern("pdu_in"), self._on_pdu)
        self.message_port_register_in(pmt.intern("ctrl"))
        self.set_msg_handler(pmt.intern("ctrl"), self._on_ctrl)
        self.message_port_register_out(pmt.intern("pdu_out"))

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

    def _release(self):
        with self._lock:
            if not self._queue:
                return
            msg, t_enq = self._queue.popleft()
        nbytes = pmt.length(pmt.cdr(msg))
        airtime = nbytes * self.samples_per_byte / self.samp_rate
        self.message_port_pub(pmt.intern("pdu_out"), msg)
        self._self_tx = True
        delay = self._tx_delay()
        self._tx = {"t0": self._t, "air": airtime, "echo_t": None, "low_since": None,
                    "hard_end": self._t + self.echo_timeout_s + airtime + self.tail_s + 0.02,
                    "win_end": self._t + delay + airtime + self.gate_guard_s}
        self._backoff, self._idle_run = None, 0.0
        self._st["tx"] += 1
        self._access_delays.append(self._t - t_enq)
        if self.verbose >= 2:
            print(f"[CSMA] TX {nbytes} B frame ({airtime * 1e3:.1f} ms) after {1e3 * (self._t - t_enq):.1f} ms "
                  f"in queue, cw {self._cw}", flush=True)

    def _tx_delay(self):
        """TX + RX latency used for the fixed receive-off window: measured echo delay if available."""
        if self.auto_delay and len(self._echo_delays) >= 3:
            return float(np.median(self._echo_delays))
        return self.tx_delay_s

    def _end_self_tx(self, reason):
        self._self_tx = False
        # the channel went quiet when our echo dropped (tail_s ago): count idle time from there, so our slot
        # grid lines up with the other node's, which saw the same burst end
        if reason == "echo_end":
            self._idle_run = self.tail_s
        elif reason == "window":
            # window end = predicted end of our echo + guard; the other node notices the end about sense_s
            # after the echo stops, so start our idle count so that both slot grids line up
            self._idle_run = self.gate_guard_s - self._sense_s
        else:
            self._idle_run = 0.0
        if reason == "timeout" and self._tx.get("echo_t") is None:
            self._st["no_echo"] += 1
            if not self._warned_no_echo:
                self._warned_no_echo = True
                print("[CSMA] own burst not seen on our receiver: RX blanking falls back to the timer "
                      "(echo_timeout_s + airtime). Check the TX, or raise echo_timeout_s.", flush=True)

    # ------------------------------------------------------------------ one sense step (nfft samples)
    def _step(self, cs_db):
        dt = self._dt
        thr_on = (self._floor_db if self._floor_db is not None else 1e9) + self.cs_margin_db
        loud = cs_db > thr_on

        if self._self_tx and self.gate_mode == 1:           # fixed receive-off window
            tx = self._tx
            if tx["echo_t"] is None and loud and self._t < tx["win_end"]:
                tx["echo_t"] = self._t                      # measure TX + RX latency for auto_delay
                self._echo_delays.append(self._t - tx["t0"])
            if self._t >= tx["win_end"]:
                if tx["echo_t"] is None:
                    self._st["no_echo"] += 1
                self._end_self_tx("window")
            return

        if self._self_tx:                                   # gate_mode 0: wait until our own echo has ended
            tx = self._tx
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

        if self._backoff is None:                           # new frame at the head: draw its backoff
            self._backoff = int(self._rng.integers(0, self._cw))

        if self._busy:
            if self._idle_run > 0.0:
                self._st["deferrals"] += 1
            self._idle_run = 0.0                            # countdown frozen; DIFS again after the busy period
            return

        # Slot boundaries sit at idle_start + difs + m * slot (m = 0, 1, 2 ...). Both nodes see the same burst end,
        # so their boundaries coincide and only frames released at the SAME boundary can collide.
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
        if self._wall0 is None:
            self._wall0, self._n_wall0 = time.monotonic(), self._n_abs
        x = input_items[0]
        out = output_items[0]
        n = len(x)
        out[:] = x

        first_abs = self._n_abs - len(self._carry)           # absolute index of buf[0]
        buf = np.concatenate((self._carry, x)) if len(self._carry) else x
        nb = len(buf) // self._nfft
        blank = []                                           # (start, stop) absolute sample ranges
        if nb:
            blocks = buf[:nb * self._nfft].reshape(nb, self._nfft) * self._win
            p = np.abs(np.fft.fft(blocks, axis=1)[:, self._bins]) ** 2
            med = np.median(p, axis=1) * self._norm
            for i in range(nb):
                self._smooth.append(float(med[i]))                 # sliding mean over sense_s
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

        if self.blank_rx and self._self_tx:                  # samples not yet sensed, but already ours
            blank.append((self._n_abs - len(self._carry), self._n_abs))
        x0 = self._n_abs - n
        for s, e in blank:
            a, b = max(s, x0) - x0, min(e, self._n_abs) - x0
            if b > a:
                out[a:b] = 0

        self._cpu += time.perf_counter() - t_cpu
        if self.verbose and self.stats_every_s > 0 and self._t - self._stat_t0 >= self.stats_every_s:
            self._print_stats()
        return n

    def _print_stats(self):
        span = max(1e-9, self._t - self._stat_t0)
        ed = list(self._echo_delays)
        ad = list(self._access_delays)
        echo = (f"own echo after {1e3 * np.median(ed):.1f} ms (max {1e3 * max(ed):.1f}) -> keep slot_s above it"
                if ed else "own echo not measured yet")
        if self.gate_mode == 1:
            echo += f" | RX off for {1e3 * self._tx_delay():.1f} ms + airtime + {1e3 * self.gate_guard_s:.1f} ms"
        floor = f"{self._floor_db:.1f}" if self._floor_db is not None else "learning"
        print(f"[CSMA] floor {floor} dB, threshold +{self.cs_margin_db:g} dB, now {self._cs_db:.1f} dB, "
              f"busy {100 * self._busy_time / span:.1f} % | tx {self._st['tx']}, deferrals {self._st['deferrals']}, "
              f"queue {len(self._queue)}, cw {self._cw}, dropped {self._st['dropped']}, forced {self._st['forced']}, "
              f"no-echo {self._st['no_echo']} | access delay median "
              f"{(1e3 * np.median(ad)) if ad else 0:.0f} ms | {echo}", flush=True)
        wall = max(1e-9, time.monotonic() - self._wall0)
        rate = (self._n_abs - self._n_wall0) / wall
        share = 100.0 * rate / self.samp_rate
        print(f"[CSMA] radio delivers {rate / 1e6:.4f} Msps = {share:.1f} % of samp_rate | this block uses "
              f"{100.0 * self._cpu / wall:.0f} % of one CPU core", flush=True)
        self._windows += 1
        if self._windows >= 2 and share < 99.0:
            print(f"[CSMA] WARNING: only {share:.1f} % of the samples arrive. The PC is not keeping up and the radio "
                  f"drops samples, which breaks frames (train word found, CRC fails). Close GUI sinks you do not "
                  f"need, run without the GUI, or lower samp_rate.", flush=True)
        self._wall0, self._n_wall0, self._cpu = time.monotonic(), self._n_abs, 0.0
        self._stat_t0 = self._t
        self._busy_time = 0.0
