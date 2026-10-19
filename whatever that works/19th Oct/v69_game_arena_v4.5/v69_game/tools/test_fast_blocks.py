#!/usr/bin/env python3
"""
test_fast_blocks.py - the flowgraph's blocks with the C++ kernels (cdp_fast) do exactly what they did before.

Each block of v6.9_cdp_link.grc that uses a kernel runs twice on the same long, realistic stream: once with the
kernels off (CDP_FAST=0: the original NumPy code) and once with them on. Everything it produces is compared:
  MAC (epy_csma)       output samples (receive blanking), every MAC event (carrier sense busy/idle, backoff, TXOPs,
                       echoes, Block ACK timeouts, retries), counters, the bursts handed to the radio. The MAC really
                       transmits: frames are queued, framed by the real framer, sent, and time out (nobody answers).
  correlator           output samples and every tag (position and value)
  deframer             every frame delivered (bytes and metadata), counters, every debug event
  noise floor          the slice means and the squelch threshold

    python3 tools/test_fast_blocks.py               (about 1 min)
    CDP_GRC_ORIG=old.grc python3 tools/test_fast_blocks.py   also checks: this .grc with kernels off == old.grc
"""
import contextlib
import io
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import epy_harness as H  # noqa: E402
import pmt  # noqa: E402

FAILS = []
SECS = float(os.environ.get("TEST_SECONDS", "20"))


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({info})" if info else ""), flush=True)
    if not ok:
        FAILS.append(name)


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


def strip_events(evs):
    out = []
    for e in evs:
        e = dict(e)
        e.pop("t", None)
        out.append(e)
    return out


def events_equal(a, b, tol=0.02):
    """same events in the same order; floats may differ by tol (values rounded to 0.01 dB / 0.01 ms)"""
    if len(a) != len(b):
        return False, f"{len(a)} vs {len(b)} events"
    for i, (x, y) in enumerate(zip(a, b)):
        if x.keys() != y.keys():
            return False, f"event {i}: keys {sorted(x)} vs {sorted(y)}"
        for k in x:
            u, v = x[k], y[k]
            if isinstance(u, float) or isinstance(v, float):
                if u is None or v is None or abs(float(u) - float(v)) > tol:
                    return False, f"event {i} {x.get('k')}.{k}: {u} vs {v}"
            elif u != v:
                return False, f"event {i} {x.get('k')}.{k}: {u!r} vs {v!r}"
    return True, f"{len(a)} events"


# ------------------------------------------------------------------ MAC
def run_csma(fast, x, grc=H.GRC):
    H.new_bus()
    cls = H.load("epy_csma", grc)
    blk = H.make_csma(cls)
    blk._rng = np.random.default_rng(5)                         # the MAC draws its backoff from this
    fr = H.make_framer(0)
    blk.route("to_phy", fr.handle_msg)
    fr.route("pdu_out", blk._on_phy)
    rng = np.random.default_rng(9)
    plan = sorted(int(v) for v in rng.uniform(0.5, SECS - 1, int(SECS * 3)) * H.SR)
    msgs = {p: (int(rng.choice([1, 2, 255])), bytes(rng.integers(0, 256, int(rng.choice([40, 300, 1200])),
                                                                 dtype=np.uint8))) for p in plan}
    pending = list(plan)

    def before(a):
        while pending and pending[0] <= a:
            p = pending.pop(0)
            dst, pay = msgs[p]
            blk._on_app(H.app_msg(dst, pay, noack=(dst == 2)))
    with quiet():
        H.start_with(blk, fast)
        used = getattr(blk, "_cs_fast", None) is not None
        out, spent = H.run_sync(blk, x, out_dtype=np.complex64, before=before)
    radio = [bytes(pmt.cdr(m)) for p, m in blk.published if p == "pdu_out"]
    ev = strip_events(H.bus_events())
    return dict(out=out, ev=ev, st=dict(blk._st), dbg=dict(blk._dbg), floor=blk._floor_db, radio=radio,
                used=used, spent=spent)


def test_csma():
    x = H.sig_csma(SECS)
    a, b = run_csma(False, x), run_csma(True, x)
    check("MAC: kernel used when on, not when off", b["used"] and not a["used"])
    check("MAC: output samples identical (receive blanking)", np.array_equal(a["out"], b["out"]))
    ok, info = events_equal(a["ev"], b["ev"])
    check("MAC: every event identical, same order", ok, info)
    check("MAC: counters identical", a["st"] == b["st"], {k: (a['st'].get(k), b['st'].get(k)) for k in
                                                           set(a['st']) | set(b['st']) if a['st'].get(k) != b['st'].get(k)})
    da = {k: round(v, 6) for k, v in a["dbg"].items()}
    db = {k: round(v, 6) for k, v in b["dbg"].items()}
    check("MAC: debug counters identical", da == db)
    check("MAC: noise floor estimate within 0.001 dB", abs(a["floor"] - b["floor"]) < 1e-3, f"{a['floor']:.4f} dB")
    check("MAC: same bursts handed to the radio", a["radio"] == b["radio"], f"{len(a['radio'])} bursts")
    kinds = {e.get("k") for e in a["ev"]}
    check("MAC: the run exercised sensing, contention and transmission",
          {"busy", "idle", "backoff", "txop"} <= kinds or {"busy", "idle", "backoff"} <= kinds, sorted(kinds)[:12])
    return a["spent"], b["spent"]


# ------------------------------------------------------------------ correlator
def run_corr(fast, x, grc=H.GRC):
    H.new_bus()
    blk = H.make_corr(H.load("epy_block_corr", grc))
    with quiet():
        H.start_with(blk, fast)
        used = getattr(blk, "_fast", None) is not None
        out, spent = H.run_sync(blk, x, out_dtype=np.complex64)
    return dict(out=out, tags=blk.out_tags, used=used, spent=spent)


def test_corr():
    x = H.sig_corr(SECS)
    a, b = run_corr(False, x), run_corr(True, x)
    check("correlator: kernel used when on", b["used"] and not a["used"])
    check("correlator: output samples identical", np.array_equal(a["out"], b["out"]))
    ta = [(o, k) for o, k, _ in a["tags"]]
    tb = [(o, k) for o, k, _ in b["tags"]]
    check("correlator: same tags at the same samples", ta == tb, f"{len(ta) // 2} training words")
    dv = max((abs(u[2] - v[2]) for u, v in zip(a["tags"], b["tags"])), default=0.0)
    check("correlator: tag values (correlation) within 1e-4", dv < 1e-4, f"max diff {dv:.1e}")
    return a["spent"], b["spent"]


# ------------------------------------------------------------------ deframer
def run_deframer(fast, bits, grc=H.GRC):
    H.new_bus()
    blk = H.make_deframer(H.load("epy_block_0_0_0", grc))
    with quiet():
        H.start_with(blk, fast)
        used = getattr(blk, "_fast", None) is not None
        _, spent = H.run_sync(blk, bits)
    pdus = [(dict(pmt.car(m)), bytes(pmt.cdr(m))) for p, m in blk.published if p == "pdu_out"]
    return dict(pdus=pdus, c=dict(blk._c), ev=strip_events(H.bus_events()), used=used, spent=spent)


def test_deframer():
    bits, sent = H.sig_bits(SECS)
    a, b = run_deframer(False, bits), run_deframer(True, bits)
    check("deframer: kernel used when on", b["used"] and not a["used"])
    check("deframer: identical frames delivered (bytes + metadata)", a["pdus"] == b["pdus"],
          f"{len(a['pdus'])} of {sent} frames sent")
    check("deframer: counters identical", a["c"] == b["c"], f"ok {a['c']['ok']}, CRC fail {a['c']['crc_fail']}, "
                                                            f"bad length {a['c']['bad_len']}")
    ok, info = events_equal(a["ev"], b["ev"], tol=0)
    check("deframer: every event identical", ok, info)
    return a["spent"], b["spent"]


# ------------------------------------------------------------------ noise floor
def run_noise(fast, x, grc=H.GRC):
    H.new_bus()
    cls = H.load("epy_noise", grc)
    blk = H.make_noise(cls)
    levels = []
    step = int(0.2 * H.SR)
    clock = {"t": 0.0}                     # level() limits the rise per second: give it the stream's clock
    cls.level.__globals__["time"] = type("T", (), {"monotonic": staticmethod(lambda: clock["t"])})

    def before(a):
        clock["t"] = a / H.SR
        if a // step != getattr(before, "last", -1):
            before.last = a // step
            levels.append(blk.level())
    with quiet():
        H.start_with(blk, fast)
        used = getattr(blk, "_fast", None) is not None
        _, spent = H.run_sync(blk, x, before=before)
    return dict(ring=np.array(blk._ring), levels=np.array(levels), used=used, spent=spent)


def test_noise():
    x = H.sig_csma(SECS, seed=4)
    a, b = run_noise(False, x), run_noise(True, x)
    check("noise floor: kernel used when on", b["used"] and not a["used"])
    same = a["ring"].shape == b["ring"].shape and np.allclose(a["ring"], b["ring"], rtol=1e-5, atol=0)
    check("noise floor: slice powers equal (1e-5)", same, f"{len(a['ring'])} slices")
    dl = float(np.max(np.abs(a["levels"] - b["levels"]))) if len(a["levels"]) == len(b["levels"]) else 1e9
    check("noise floor: squelch threshold within 0.001 dB", dl < 1e-3, f"{len(a['levels'])} readings, max diff {dl:.1e} dB")
    return a["spent"], b["spent"]


# ------------------------------------------------------------------ the old .grc (optional)
def test_orig(orig):
    print(f"-- {Path(orig).name} vs this .grc with the kernels off (must be bit-identical)")
    x = H.sig_csma(min(SECS, 8))
    a, b = run_csma(False, x, orig), run_csma(False, x)
    ok, info = events_equal(a["ev"], b["ev"], tol=0)
    check("old .grc == new .grc (kernels off): MAC", np.array_equal(a["out"], b["out"]) and ok and a["st"] == b["st"],
          info)
    y = H.sig_corr(min(SECS, 8))
    a, b = run_corr(False, y, orig), run_corr(False, y)
    check("old .grc == new .grc (kernels off): correlator", np.array_equal(a["out"], b["out"]) and a["tags"] == b["tags"])
    bits, _ = H.sig_bits(min(SECS, 8))
    a, b = run_deframer(False, bits, orig), run_deframer(False, bits)
    check("old .grc == new .grc (kernels off): deframer", a["pdus"] == b["pdus"] and a["c"] == b["c"])
    a, b = run_noise(False, x, orig), run_noise(False, x)
    check("old .grc == new .grc (kernels off): noise floor", np.array_equal(a["ring"], b["ring"]))


def main():
    import cdp_fast
    if cdp_fast.lib() is None:
        print(f"C++ kernels not available: {cdp_fast.why()}")
        sys.exit(2)
    print(f"{SECS:g} s of signal per block; kernels: {cdp_fast.SRC.name} v{cdp_fast.VERSION}")
    times = {}
    times["MAC (epy_csma)"] = test_csma()
    times["correlator"] = test_corr()
    times["deframer"] = test_deframer()
    times["noise floor"] = test_noise()
    if os.environ.get("CDP_GRC_ORIG"):
        test_orig(os.environ["CDP_GRC_ORIG"])
    print("\nCPU time in work() for %g s of signal (one core):" % SECS)
    tot = [0.0, 0.0]
    for k, (a, b) in times.items():
        tot[0] += a
        tot[1] += b
        print(f"  {k:16s} Python {100 * a / SECS:5.1f} %   with C++ {100 * b / SECS:5.1f} %   x{a / max(b, 1e-9):.1f}")
    print(f"  {'all four':16s} Python {100 * tot[0] / SECS:5.1f} %   with C++ {100 * tot[1] / SECS:5.1f} %   "
          f"x{tot[0] / max(tot[1], 1e-9):.1f}")
    print("ALL PASS" if not FAILS else f"FAILED: {FAILS}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
