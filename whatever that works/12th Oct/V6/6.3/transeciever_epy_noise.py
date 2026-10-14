"""
Embedded Python Block: Noise-floor tracker -> automatic squelch threshold
- Sink block: connect it to the SDR source output (BEFORE any squelch/AGC).
- Splits the stream into short slices (default ~1 ms), keeps the mean power of each slice for the
  last `window_s` seconds, and takes a low percentile of them as the noise floor (bursts only occupy
  the top of the distribution, so they are ignored as long as the channel is idle > `percentile` % of the time).
- level() returns the squelch threshold in dB:  noise floor (dB) + margin_db, clamped to
  [thr_min, thr_max]. The estimate falls immediately but rises at most `rise_db_s` dB per second, so a
  long burst or a back-to-back transmission cannot drag the threshold up and close the squelch.
- Use with a "Function Probe" variable named power_threshold:  block id = this block, function = level,
  poll rate ~ 5 Hz. The Pluto/squelch power unit is the same: dB of mean |x|^2.
"""
import threading
import time
from collections import deque

import numpy as np
from gnuradio import gr


class NoiseFloor(gr.sync_block):
    def __init__(self, samp_rate=1.5e6, margin_db=6.0, window_s=1.0, percentile=10.0,
                 rise_db_s=1.0, thr_min=-100.0, thr_max=-20.0, init_db=-40.0, verbose=1):
        gr.sync_block.__init__(self, name="Noise Floor Tracker", in_sig=[np.complex64], out_sig=None)
        self.samp_rate = float(samp_rate)
        self.margin_db = float(margin_db)
        self.window_s = float(window_s)
        self.percentile = float(percentile)
        self.rise_db_s = float(rise_db_s)
        self.thr_min = float(thr_min)
        self.thr_max = float(thr_max)
        self.init_db = float(init_db)
        self.verbose = int(verbose)

        self._slice = max(64, int(self.samp_rate * 1e-3))            # ~1 ms
        n_slices = max(16, int(self.window_s * self.samp_rate / self._slice))
        self._ring = deque(maxlen=n_slices)
        self._min_slices = max(8, n_slices // 4)                       # wait for 1/4 window before trusting
        self._carry = np.zeros(0, np.float32)
        self._lock = threading.Lock()
        self._est_db = None
        self._t_last = None
        self._out = self.init_db
        self._printed = None

    def work(self, input_items, output_items):
        x = input_items[0]
        n = len(x)
        if n == 0:
            return 0
        p = x.real.astype(np.float32) ** 2 + x.imag.astype(np.float32) ** 2
        if self._carry.size:
            p = np.concatenate((self._carry, p))
        k = len(p) // self._slice
        if k:
            means = p[: k * self._slice].reshape(k, self._slice).mean(axis=1, dtype=np.float64)
            with self._lock:
                self._ring.extend(means)
        self._carry = p[k * self._slice:]
        return n

    def noise_db(self):
        return self._est_db

    def level(self):
        """Called by the Function Probe variable: returns the squelch threshold in dB."""
        with self._lock:
            arr = np.fromiter(self._ring, dtype=np.float64, count=len(self._ring))
        if arr.size < self._min_slices:
            return self._out
        raw_db = 10.0 * np.log10(max(float(np.percentile(arr, self.percentile)), 1e-20))
        now = time.monotonic()
        if self._est_db is None:
            self._est_db = raw_db
        else:
            dt = max(0.0, now - self._t_last)
            if raw_db <= self._est_db:
                self._est_db = raw_db
            else:
                self._est_db = min(raw_db, self._est_db + self.rise_db_s * dt)
        self._t_last = now
        self._out = float(min(self.thr_max, max(self.thr_min, self._est_db + self.margin_db)))
        if self.verbose and (self._printed is None or abs(self._out - self._printed) >= 1.0):
            print(f"[SQL] noise floor {self._est_db:.1f} dB -> squelch threshold {self._out:.1f} dB", flush=True)
            self._printed = self._out
        return self._out
