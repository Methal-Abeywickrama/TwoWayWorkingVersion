"""
Embedded Python Block: TX probe (sink, for diagnostics)
Connect a second output line from the TX chain (e.g. the Tag Gate output that feeds the Pluto Sink) to this block.
It only counts: samples handed to the radio sink, bursts (separated by > 50 ms without samples) and the time of
the last sample. The Telemetry block reads it: frames released by CSMA while no TX samples flow = TX path stalled.
"""
import time

import numpy as np
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


class TxProbe(gr.sync_block):
    def __init__(self, samp_rate=1.5e6, gap_s=0.05):
        gr.sync_block.__init__(self, name="TX Probe", in_sig=[np.complex64], out_sig=None)
        self.samp_rate = float(samp_rate)
        self.gap_s = float(gap_s)
        self._samples = 0
        self._bursts = 0
        self._last = 0.0
        self._peak = 0.0

    def start(self):
        _cdp_register("txprobe", self)
        return True

    def work(self, input_items, output_items):
        x = input_items[0]
        now = time.monotonic()
        if now - self._last > self.gap_s:
            self._bursts += 1
        self._last = now
        self._samples += len(x)
        if len(x):
            self._peak = max(self._peak, float(np.max(np.abs(x[::16]))))
        return len(x)

    def telemetry(self):
        now = time.monotonic()
        peak, self._peak = self._peak, 0.0
        return {"samples": self._samples, "bursts": self._bursts, "airtime_s": self._samples / self.samp_rate,
                "last_age_s": (now - self._last) if self._last else None, "peak": peak}
