#!/usr/bin/env python3
"""
bench_blocks.py - how much CPU the flowgraph's Python blocks need, with and without the C++ kernels, and how well
they run side by side (GNU Radio gives every block its own thread; Python code holds the GIL, C++ code does not).

    python3 tools/bench_blocks.py [seconds of signal, default 10]

Prints, for 1 MS/s (the default CDP_SAMP_RATE):
  - CPU per block, one after the other (% of one core)
  - all four blocks at once, each in its own thread (as GNU Radio runs them): how long they take for the signal
"""
import contextlib
import io
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import epy_harness as H  # noqa: E402

SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0


_CLS = {}


def _cls(name):
    if name not in _CLS:                       # parse the .grc once (slow), outside the timed part
        _CLS[name] = H.load(name)
    return _CLS[name]


def jobs(fast, sig):
    """-> [(name, callable)]: the blocks are built and started here; the callables only run them"""
    out = []
    b = H.make_csma(_cls("epy_csma"))
    b._rng = np.random.default_rng(5)
    H.start_with(b, fast)
    out.append(("MAC (epy_csma)", lambda b=b: H.run_sync(b, sig["mac"], out_dtype=np.complex64)))
    b = H.make_corr(_cls("epy_block_corr"))
    H.start_with(b, fast)
    out.append(("correlator", lambda b=b: H.run_sync(b, sig["corr"], out_dtype=np.complex64)))
    b = H.make_deframer(_cls("epy_block_0_0_0"))
    H.start_with(b, fast)
    out.append(("deframer", lambda b=b: H.run_sync(b, sig["bits"])))
    b = H.make_noise(_cls("epy_noise"))
    H.start_with(b, fast)
    out.append(("noise floor", lambda b=b: H.run_sync(b, sig["mac"])))
    return out


def main():
    import cdp_fast
    have = cdp_fast.lib() is not None
    print(f"{SECS:g} s of signal at 1 MS/s; C++ kernels: {'on' if have else 'NOT available: ' + str(cdp_fast.why())}")
    sig = {"mac": H.sig_csma(SECS), "corr": H.sig_corr(SECS), "bits": H.sig_bits(SECS)[0]}
    res = {}
    for fast in ([False, True] if have else [False]):
        label = "C++ kernels" if fast else "Python only"
        with contextlib.redirect_stdout(io.StringIO()):
            serial = {}
            for name, fn in jobs(fast, sig):
                t = time.perf_counter()
                fn()
                serial[name] = time.perf_counter() - t
            t_serial = sum(serial.values())
            ths = [threading.Thread(target=fn) for _, fn in jobs(fast, sig)]
            t0 = time.perf_counter()
            for th in ths:
                th.start()
            for th in ths:
                th.join()
            t_par = time.perf_counter() - t0
        res[label] = (serial, t_serial, t_par)
    print()
    names = list(next(iter(res.values()))[0])
    print(f"{'':18s}" + "".join(f"{k:>16s}" for k in res))
    for n in names:
        print(f"{n:18s}" + "".join(f"{100 * r[0][n] / SECS:14.1f} %" for r in res.values()))
    print(f"{'all four, serial':18s}" + "".join(f"{100 * r[1] / SECS:14.1f} %" for r in res.values()))
    print(f"{'all four, threads':18s}" + "".join(f"{100 * r[2] / SECS:14.1f} %" for r in res.values()) +
          "   (wall time / signal time)")
    if len(res) == 2:
        (a, b) = res.values()
        print(f"\nWith the C++ kernels the four blocks need {a[1] / b[1]:.1f}x less CPU; run side by side (threads, as in "
              f"GNU Radio) they finish {a[2] / b[2]:.1f}x sooner.")


if __name__ == "__main__":
    main()
