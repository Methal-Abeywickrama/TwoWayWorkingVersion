"""
Embedded Python Block: Training-word correlator (drop-in for digital_corr_est_cc)
- Input : complex stream at 2 samples/symbol (symbol sync output)
- Output: the same stream delayed by a fixed EVEN number of samples (so tags can be placed in the past
  without shifting the symbol / midpoint sample phase)
- Slides the reference (TRAIN_REF) over the stream and computes the NORMALIZED correlation
      rho = |sum(conj(ref) * x)|^2 / (E_ref * E_window)      (0..1, independent of signal amplitude)
  Where rho has a local maximum above thr it adds a 'corr_est' tag (and 'corr_start') on the
  first training symbol, shifted by mark_delay samples. Random data gives rho ~ 0.1-0.25; a real
  training word gives ~0.8-1.0, so thr ~ 0.6-0.7 is a good start. The tag value is rho.
- Pure numpy, bounds-checked: no use of GNU Radio's corr_est_cc.
- Stress-test build: every tag and a 1 s snapshot (tags, strongest correlation) go to the debug bus (src "corr").
"""
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

# ---- C++ kernels (cdp_fast.py / cdp_fast.cpp next to the flowgraph); None -> the NumPy code of this block ----
def _load_fast():
    import os as _os
    if _os.environ.get("CDP_FAST", "1").strip().lower() in ("0", "no", "off", "false"):
        return None
    try:
        import cdp_fast
        return cdp_fast
    except ImportError:
        pass
    for d in (_os.environ.get("CDP_HOME"), _os.getcwd(),
              _os.path.dirname(_os.path.abspath(_sys.argv[0])) if _sys.argv and _sys.argv[0] else None):
        if d and _os.path.isfile(_os.path.join(d, "cdp_fast.py")):
            if d not in _sys.path:
                _sys.path.insert(0, d)
            try:
                import cdp_fast
                return cdp_fast
            except Exception:
                return None
    return None
# ---- end C++ kernels ----


class TrainCorr(gr.sync_block):
    def __init__(self, ref=None, thr=0.7, mark_delay=0, verbose=1):
        gr.sync_block.__init__(
            self,
            name="Training-word Correlator",
            in_sig=[np.complex64],
            out_sig=[np.complex64],
        )
        if ref is None:
            ref = [1.0]
        self._ref = np.asarray(ref, dtype=np.complex64)
        self._L = len(self._ref)
        self._E = float(np.sum(np.abs(self._ref) ** 2))
        self.thr = float(thr)
        self.mark_delay = max(0, int(mark_delay))
        self.verbose = int(verbose)
        # Output delay: >= L + 3 so every peak is examined exactly once, and a MULTIPLE OF 2 (samples per
        # symbol) so the symbol-timed samples stay on even stream indices -- the linear equalizer takes
        # the first sample of every 2-sample block, so an odd delay would make it sample the midpoints.
        self._D = self._L + 8
        self._D += self._D % 2
        self.set_history(self._D + 1)
        self._next_p = 0               # first absolute input index not yet examined
        self._last_p = -10**12         # last tagged absolute input index
        self._seen = 0                 # samples since last status line
        self._best = 0.0               # strongest normalized correlation since last status line
        self._dbg_tags = 0
        self._dbg_best = 0.0
        self._dbg_n = 0
        self._fast = None              # C++ correlation (set in start)

    def start(self):
        F = _load_fast()
        if F is not None:
            cm = F.CorrMetric(self._ref, self._E)
            self._fast = cm if cm.ok else None
        if self.verbose:
            print(F.status("correlator") if F is not None else "[FAST] correlator: original Python code", flush=True)
        _bus().sources["corr"] = self
        _emit("corr", "config", thr=self.thr, mark_delay=self.mark_delay, ref_len=self._L, delay=self._D)
        return True

    def dbg_snapshot(self):
        out = {"tags": self._dbg_tags, "best": round(self._dbg_best, 3), "n": self._dbg_n, "thr": self.thr}
        self._dbg_tags, self._dbg_best, self._dbg_n = 0, 0.0, 0
        return out

    def work(self, input_items, output_items):
        x = input_items[0]
        out = output_items[0]
        n_out = len(out)
        out[:] = x[:n_out]             # in[i] <-> absolute input index a-D+i  ==  output index a+i

        a = self.nitems_written(0)
        base = a - self._D             # absolute input index of x[0]
        if len(x) < self._L + 5:
            return n_out
        if self._fast is not None:
            m = self._fast(x)                                    # C++: the same metric, without the GIL
        else:
            c = np.correlate(x, self._ref, "valid")                  # window starting at x[n]
            w = np.convolve(np.abs(x).astype(np.float64) ** 2, np.ones(self._L), "valid")
            m = (np.abs(c).astype(np.float64) ** 2) / (self._E * w + 1e-12)    # normalized, 0..1
        n0 = max(self._next_p - base, 2, -base)       # first window start to examine (p >= 0)
        n1 = len(m) - 3                               # last start with 2 neighbours on the right
        if self._fast is not None:                    # C++: the same comparisons on the same values
            hits, m_max = self._fast.peaks(m, n0, n1, self.thr)
        else:
            m_max = float(m.max())
        self._best = max(self._best, m_max)
        self._dbg_best = max(self._dbg_best, m_max)
        self._dbg_n += n_out
        self._seen += n_out

        if n1 >= n0:
            mid = m[n0:n1 + 1]
            if self._fast is None:
                seg = m[n0 - 2 : n1 + 3]
                mid = seg[2:-2]
                thr_abs = self.thr
                cond = (
                    (mid > thr_abs)
                    & (mid > seg[0:-4]) & (mid > seg[1:-3])
                    & (mid >= seg[3:-1]) & (mid >= seg[4:])
                )
                hits = np.flatnonzero(cond)
            for j in hits:
                p = base + n0 + int(j)
                if p - self._last_p < self._L:
                    continue
                self._last_p = p
                ratio = float(mid[j])
                off = int(p + self._D + self.mark_delay)    # output index; always >= a
                val = pmt.from_double(ratio)
                self.add_item_tag(0, off, pmt.intern("corr_start"), val)
                self.add_item_tag(0, off, pmt.intern("corr_est"), val)
                self._dbg_tags += 1
                _emit("corr", "tag", rho=round(ratio, 3), off=off)
                self._best = 0.0
                self._seen = 0
                if self.verbose:
                    print(f"[CORR] train word found: correlation = {ratio:.2f} (thr {self.thr:.2f})", flush=True)
            self._next_p = base + n1 + 1

        if self.verbose and self._seen > 400000:
            if self._best > 0.35:
                print(f"[CORR] no tag; strongest correlation in last window = {self._best:.2f} (thr {self.thr:.2f})", flush=True)
            self._best = 0.0
            self._seen = 0
        return n_out
