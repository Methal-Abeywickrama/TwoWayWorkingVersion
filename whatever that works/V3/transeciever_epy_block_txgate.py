"""
Embedded Python Block: TX Burst Gate (place between the Constellation Modulator and the
PlutoSDR Sink). It acts on the burst tags of the buffer-aligned framer:

  tx_sob + burst_data + burst_len   (on the first sample of every burst)

1. Before the first sample of a burst is passed on, the transmitter is switched ON
   (synchronously, so no burst sample can reach the radio while it is still off).
2. The samples after `burst_data` (the pad that fills the last Pluto buffer) are set to 0,
   with a short ramp, so nothing is radiated there. The burst stays n * tx_buffer samples
   long, so the sink still sends it out immediately.
3. A sample-accurate `tx_eob` tag is put on the last signal sample (plus `tx_sob` on the
   first one) for anything downstream that understands them.
4. When `tx_eob` passes, the transmitter is switched OFF after `hold` seconds:
       hold = (kernel_buffers + 3) * tx_buffer / samp_rate + hold_ms / 1000
   That covers every sample that can still be queued between this block and the antenna
   (GNU Radio buffer ~2 x tx_buffer, the sink's own buffer, libiio's kernel buffers).
   A new burst cancels a pending OFF. GNU Radio never says when the radio has really sent
   the last sample, so this delay is an upper bound; during it only zeros are sent.

mode (how "off" is done, through libiio on the same URI as the Pluto sink):
  'atten'  TX attenuation -> off_atten (89.75 dB = the AD9361 maximum); on -> on_atten
           (keep on_atten = the Pluto sink's attenuation, e.g. CH_GAIN)
  'lo'     power down the TX LO (no carrier/LO leakage at all); power up again for a burst.
           The 512+ byte preamble covers the LO lock time.
  'both'   both of the above
  'none'   only blanking + tags (no radio control)
Needs the libiio Python bindings (`import iio`; Ubuntu/Debian: sudo apt install python3-libiio).
Without them it prints a warning and keeps working in 'none' mode.

If bursts arrive WITHOUT tags (e.g. a Tag Gate in front of this block), the gate switches the
TX on as soon as non-zero samples show up and off again after `hold`, and warns once.
"""
import threading
import time

import numpy as np
import pmt
from gnuradio import gr

AD9361_MAX_ATTEN = 89.75


class TxBurstGate(gr.sync_block):
    def __init__(self, uri="ip:192.168.1.10", samp_rate=1.5e6, tx_buffer=4096,
                 on_atten=20.0, off_atten=89.75, mode="atten", kernel_buffers=4,
                 hold_ms=5.0, ramp=64, verbose=True):
        gr.sync_block.__init__(self, name="TX Burst Gate",
                               in_sig=[np.complex64], out_sig=[np.complex64])
        self.set_tag_propagation_policy(gr.TPP_DONT)   # tags are re-made below, sample exact
        self.uri = str(uri)
        self.samp_rate = float(samp_rate)
        self.tx_buffer = int(tx_buffer)
        self.on_atten = float(on_atten)
        self.off_atten = float(off_atten)
        self.mode = str(mode).lower()
        self.kernel_buffers = int(kernel_buffers)
        self.hold_ms = float(hold_ms)
        self.ramp = max(0, int(ramp))
        self.verbose = bool(verbose)
        if self.mode not in ("atten", "lo", "both", "none"):
            raise ValueError("mode must be 'atten', 'lo', 'both' or 'none'")

        self._lock = threading.RLock()
        self._bursts = []          # [start, data_end, end, eob_done] absolute sample offsets
        self._timer = None
        self._gen = 0              # bumped by every burst start; a stale OFF timer sees it
        self._on = None            # None = unknown, True = transmitting, False = off
        self._phy = None
        self._tx_chans = []
        self._lo = None
        self._warned_untagged = False
        self.n_bursts = 0
        self.n_off = 0

    # ------------------------------------------------------------- radio control
    def _hold_s(self):
        return (self.kernel_buffers + 3) * self.tx_buffer / self.samp_rate + self.hold_ms / 1000.0

    def _open(self):
        if self.mode == "none":
            return
        try:
            import iio
        except ImportError:
            print("[TXGATE] libiio Python bindings not found (sudo apt install python3-libiio) "
                  "- TX on/off control DISABLED, blanking + tags still work", flush=True)
            self.mode = "none"
            return
        try:
            ctx = iio.Context(self.uri) if self.uri else iio.Context()
            phy = ctx.find_device("ad9361-phy")
            if phy is None:
                raise RuntimeError("no ad9361-phy device at " + self.uri)
            self._ctx, self._phy = ctx, phy
            self._tx_chans = [c for c in (phy.find_channel("voltage0", True),
                                          phy.find_channel("voltage1", True))
                              if c is not None and "hardwaregain" in c.attrs]
            self._lo = phy.find_channel("altvoltage1", True)        # TX_LO
            if self._lo is not None and "powerdown" not in self._lo.attrs:
                self._lo = None
            print(f"[TXGATE] radio control on {self.uri}: mode={self.mode}, "
                  f"off={self.off_atten} dB, hold={self._hold_s() * 1000:.1f} ms", flush=True)
        except Exception as e:
            print(f"[TXGATE] could not open {self.uri} for TX control ({e}) - "
                  "TX on/off control DISABLED, blanking + tags still work", flush=True)
            self.mode = "none"

    def _write(self, chan, attr, value):
        try:
            chan.attrs[attr].value = value
        except Exception as e:
            print(f"[TXGATE] write {attr}={value} failed: {e}", flush=True)

    def _set_tx(self, on):
        """Switch the transmitter on/off. Caller holds self._lock."""
        if self._on is on:
            return
        t0 = time.time()
        if self.mode in ("lo", "both") and self._lo is not None:
            self._write(self._lo, "powerdown", "0" if on else "1")
        if self.mode in ("atten", "both"):
            att = self.on_atten if on else min(abs(self.off_atten), AD9361_MAX_ATTEN)
            for c in self._tx_chans:
                self._write(c, "hardwaregain", f"{-abs(att):.2f}")
        self._on = on
        if not on:
            self.n_off += 1
        if self.verbose and self.mode != "none" and self.n_bursts <= 3:
            print(f"[TXGATE] TX {'ON ' if on else 'OFF'} ({(time.time() - t0) * 1000:.1f} ms)",
                  flush=True)

    def _off_later(self, delay):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            gen = self._gen
            self._timer = threading.Timer(delay, self._off_timer, args=[gen])
            self._timer.daemon = True
            self._timer.start()

    def _off_timer(self, gen):
        with self._lock:
            if gen == self._gen and not self._bursts_pending():
                self._set_tx(False)

    def _bursts_pending(self):
        # a burst whose signal part has not fully passed yet keeps the TX on
        return any(not b[3] for b in self._bursts)

    def start(self):
        self._open()
        with self._lock:
            self._set_tx(False)          # idle = off
        return True

    def stop(self):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            if self.mode != "none":
                # leave the radio as the Pluto sink configured it
                self._on = None
                self._set_tx(True)
        return True

    # ------------------------------------------------------------- stream
    def _ramp(self, n):
        return (0.5 - 0.5 * np.cos(np.pi * (np.arange(n) + 0.5) / n)).astype(np.float32)

    def work(self, input_items, output_items):
        inp, out = input_items[0], output_items[0]
        n = len(inp)
        base = self.nitems_read(0)
        out[:] = inp

        # 1) new bursts from the framer's tags (all three sit on the burst's first sample)
        found = {}
        for t in self.get_tags_in_window(0, 0, n):
            key = pmt.symbol_to_string(t.key)
            if key in ("burst_data", "burst_len", "tx_sob"):
                d = found.setdefault(t.offset, {})
                if key != "tx_sob":
                    d[key] = pmt.to_long(t.value)
        for off in sorted(found):
            d = found[off]
            if "burst_len" not in d:
                continue
            data = min(int(d.get("burst_data", d["burst_len"])), int(d["burst_len"]))
            with self._lock:
                self._gen += 1                       # cancels any pending OFF
                if self._timer is not None:
                    self._timer.cancel()
                    self._timer = None
                self._set_tx(True)                   # ON before the first sample leaves here
                self._bursts.append([off, off + data, off + int(d["burst_len"]), False])
                self.n_bursts += 1
            if off % self.tx_buffer and self.verbose:
                print(f"[TXGATE] warning: burst starts at sample {off}, not on a "
                      f"{self.tx_buffer}-sample boundary (tx_buffer must equal the sink's "
                      "buffer size, and nothing else may feed the modulator)", flush=True)
            self.add_item_tag(0, off, pmt.intern("tx_sob"), pmt.PMT_T)

        # 2) blank the pad, ramp the edges, tag the end, schedule OFF
        r = self._ramp(self.ramp) if self.ramp else None
        done = []
        with self._lock:
            tagged = bool(self._bursts)
            for b in self._bursts:
                start, dend, end, eob = b
                lo, hi = base, base + n
                if r is not None:
                    # ramp up over the first `ramp` samples, ramp down over the last `ramp` signal samples
                    for (a, w) in ((start, r), (dend - self.ramp, r[::-1])):
                        s, e = max(a, lo), min(a + len(w), hi)
                        if s < e:
                            out[s - lo:e - lo] *= w[s - a:e - a]
                s, e = max(dend, lo), min(end, hi)
                if s < e:
                    out[s - lo:e - lo] = 0
                if not eob and lo <= dend - 1 < hi:
                    self.add_item_tag(0, dend - 1, pmt.intern("tx_eob"), pmt.PMT_T)
                    b[3] = True
                    if self.mode != "none":
                        self._off_later(self._hold_s())
                if end <= hi and b[3]:
                    done.append(b)
            for b in done:
                self._bursts.remove(b)

        # 3) safety net: signal without tags (tags stripped upstream)
        if not tagged and self.mode != "none" and np.any(inp != 0):
            if not self._warned_untagged:
                print("[TXGATE] signal without burst tags (is a Tag Gate in front of this "
                      "block?) - switching TX on for it, no blanking", flush=True)
                self._warned_untagged = True
            with self._lock:
                self._gen += 1
                self._set_tx(True)
            self._off_later(self._hold_s())
        return n
