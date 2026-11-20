#!/usr/bin/env python3
"""
epy_harness.py - runs the flowgraph's embedded Python blocks outside GNU Radio (with tools/grstub), on realistic
signals, so their outputs and their speed can be compared (tools/test_fast_blocks.py, tools/bench_blocks.py).

    load(name)            -> the block class, from v6.9_cdp_link.grc (or another .grc: CDP_GRC=...)
    sig_csma(seconds)     -> complex64 at 1 MS/s: noise floor + bursts of different power / length
    sig_corr(seconds)     -> complex64 at 2 samples/symbol: noise + bursts that start with the training word
    sig_bits(seconds)     -> uint8 bits at 500 kbit/s: idle noise bits + frames built by the real framer, with bit errors
    run_*(block, x, ...)  -> drive work() in GNU-Radio-sized chunks and collect what the block produced
"""
import os
import sys
import time
import types
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE / "grstub"))
sys.path.insert(0, str(ROOT))

import pmt  # noqa: E402  (the stand-in)
from gnuradio import gr  # noqa: E402

GRC = Path(os.environ.get("CDP_GRC", ROOT / "v6.9_cdp_link.grc"))
SR = 1e6


def block_source(name, grc=GRC):
    import yaml
    d = yaml.safe_load(open(grc))
    for b in d["blocks"]:
        if b["name"] == name:
            return b["parameters"]["_source_code"]
    raise KeyError(name)


def load(name, grc=GRC, fast=None):
    """-> the class defined by embedded block `name`. fast=False/True forces CDP_FAST off/on for this load."""
    src = block_source(name, grc)
    mod = types.ModuleType(f"epy_{name}")
    mod.__file__ = str(ROOT / f"{name}.py")
    old = os.environ.get("CDP_FAST")
    if fast is not None:
        os.environ["CDP_FAST"] = "1" if fast else "0"
    try:
        exec(compile(src, f"<{name}>", "exec"), mod.__dict__)
    finally:
        if fast is not None:
            if old is None:
                os.environ.pop("CDP_FAST", None)
            else:
                os.environ["CDP_FAST"] = old
    for v in mod.__dict__.values():
        if isinstance(v, type) and issubclass(v, gr.basic_block) and v.__module__ == mod.__name__:
            return v
    raise RuntimeError(f"no block class in {name}")


# ------------------------------------------------------------------ signals
def sig_csma(seconds, seed=1, sr=SR):
    rng = np.random.default_rng(seed)
    n = int(seconds * sr)
    floor = 10 ** (-62 / 20)
    x = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * floor / np.sqrt(2)
    t = int(0.3 * sr)
    while t < n:
        dur = int(rng.choice([0.003, 0.012, 0.03, 0.1]) * sr)
        amp = 10 ** (rng.choice([-50, -40, -30, -58]) / 20)
        sym = (rng.integers(0, 2, dur // 4 + 1) * 2 - 1) + 1j * (rng.integers(0, 2, dur // 4 + 1) * 2 - 1)
        burst = np.repeat(sym, 4)[:dur] * amp / np.sqrt(2)
        e = min(n, t + dur)
        x[t:e] += burst[:e - t]
        t = e + int(rng.uniform(0.005, 0.2) * sr)
    return x.astype(np.complex64)


def train_ref():
    alpha = 0.35
    bits = np.unpackbits(np.frombuffer(bytes([0xF8, 0xDD, 0x42, 0x58]), np.uint8))
    syms = 1 - 2 * np.bitwise_xor.accumulate(bits).astype(int)
    x = np.arange(-8, 9) / 2.0
    rc = np.sinc(x) * np.cos(np.pi * alpha * x) / (1 - (2 * alpha * x) ** 2)
    return list(map(complex, np.convolve(np.kron(syms, [1, 0]), rc)[8:8 + 2 * len(syms) - 1])), syms, rc


def sig_corr(seconds, seed=2, sps_rate=500e3):
    rng = np.random.default_rng(seed)
    ref, syms, rc = train_ref()
    n = int(seconds * sps_rate)
    out = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * 0.05
    t = 1000
    while t < n - 20000:
        nd = int(rng.integers(200, 4000))
        data = (rng.integers(0, 2, nd) * 2 - 1) + 1j * (rng.integers(0, 2, nd) * 2 - 1)
        s = np.concatenate((np.ones(64), syms, data / np.sqrt(2)))
        w = np.convolve(np.kron(s, [1, 0]), rc)
        snr = rng.choice([0.05, 0.2, 0.5])
        w = w + (rng.standard_normal(len(w)) + 1j * rng.standard_normal(len(w))) * snr
        e = min(n, t + len(w))
        out[t:e] = w[:e - t]
        t = e + int(rng.integers(500, 30000))
    return out.astype(np.complex64)


def sig_bits(seconds, seed=3, fec=31, ber=(0.0, 1e-3, 1e-2), max_payload=2048, my_id=0):
    """-> (bits uint8, frames sent). Frames come from the real framer block (its build_frame)."""
    rng = np.random.default_rng(seed)
    F = load("epy_block_0_0_1")
    fr = F(src_id=1, dest_id=255, preamble_len=128, repeat_count=1, max_payload_len=max_payload, preamble_byte=0xFF,
           buffer_size=4096, postamble_byte=0xFF, train_enable=1, fec=fec, interleave=True, samples_per_byte=32, verbose=0)
    n = int(seconds * 500e3)
    parts, used, sent, mid = [], 0, 0, 0
    while used < n:
        gap = rng.integers(0, 2, int(rng.integers(200, 40000)), dtype=np.uint8)
        parts.append(gap)
        used += len(gap)
        plen = int(rng.choice([20, 120, 900, max_payload]))
        payload = bytes(rng.integers(0, 256, plen, dtype=np.uint8))
        src = int(rng.choice([1, 2, my_id]))
        dst = int(rng.choice([my_id, 255, 7]))
        hdr = plen.to_bytes(2, "big") + bytes([dst, src, mid & 0xFF, 0, 1])
        mid += 1
        bits = np.unpackbits(np.frombuffer(fr.build_frame(hdr, payload), np.uint8))
        p = float(rng.choice(ber))
        if p:
            flip = rng.random(len(bits)) < p
            bits = bits ^ flip.astype(np.uint8)
        parts.append(bits)
        used += len(bits)
        sent += 1
    return np.concatenate(parts)[:n].astype(np.uint8), sent


def chunks(n, rng, lo=512, hi=8192):
    """GNU-Radio-like work() sizes"""
    i = 0
    while i < n:
        k = int(rng.integers(lo, hi + 1))
        yield i, min(n, i + k)
        i += k


# ------------------------------------------------------------------ drivers
def run_sync(blk, x, seed=0, out_dtype=None, tags=None, lo=512, hi=8192, before=None):
    """Calls work() like the scheduler; history is honoured; before(a) runs before each call (a = first item).
    -> (output or None, seconds spent in work())"""
    rng = np.random.default_rng(seed)
    h = blk.history() - 1
    pad = np.concatenate((np.zeros(h, x.dtype), x)) if h else x
    out = np.zeros(len(x), out_dtype) if out_dtype is not None else None
    blk.in_tags = tags or []
    spent = 0.0
    for a, b in chunks(len(x), rng, lo, hi):
        blk._nread, blk._nwritten = a, a
        if before is not None:
            before(a)
        inp = [pad[a:b + h]]
        o = [np.zeros(b - a, out_dtype)] if out_dtype is not None else []
        t = time.perf_counter()
        r = blk.work(inp, o)
        spent += time.perf_counter() - t
        assert r == b - a, (r, a, b)
        if out is not None:
            out[a:b] = o[0]
    return out, spent


def new_bus():
    """fresh debug bus (the blocks keep one per process in sys.modules)"""
    sys.modules.pop("cdp_dbg_bus", None)


def bus_events(kind_filter=None):
    b = sys.modules.get("cdp_dbg_bus")
    ev = list(b.events) if b else []
    return [e for e in ev if kind_filter is None or (e.get("blk"), e.get("k")) in kind_filter]


def make_csma(cls, sr=SR, **kw):
    args = dict(samp_rate=sr, samples_per_byte=32, my_id=0, dest_id=255, burst_m=4, window_n=16, txop_limit_s=0.25,
                p_persist=0.2, slot_s=0.002, sifs_s=0.001, turnaround_s=0.025, difs_s=0.0, cw_min=15, cw_max=63,
                k_max=5, retry_limit=7, ba_timeout_s=0.07, max_queue=256, udp_queue=32, phy_preamble_len=128,
                phy_fec=31, phy_buffer_size=4096, phy_max_payload=2048, cs_margin_db=6.0, cs_hyst_db=2.0,
                cs_bw_hz=400000.0, dc_exclude_hz=10000.0, nfft=256, sense_s=0.001, floor_window_s=2.0,
                floor_percentile=10.0, floor_rise_db_s=1.0, blank_rx=1, tx_delay_s=0.012, auto_delay=1,
                gate_guard_s=0.005, tx_mute=0, iio_uri="", tx_att_db=0.0, mute_att_db=89.75, unmute_timeout_s=0.1,
                echo_margin_db=25.0, verbose=0, stats_every_s=0.0)
    args.update(kw)
    return cls(**args)


def make_framer(my_id=0):
    F = load("epy_block_0_0_1")
    return F(src_id=my_id, dest_id=255, preamble_len=128, repeat_count=1, max_payload_len=2048, preamble_byte=0xFF,
             buffer_size=4096, postamble_byte=0xFF, train_enable=1, fec=31, interleave=True, samples_per_byte=32,
             verbose=0)


def start_with(blk, fast):
    """blk.start() with the C++ kernels switched on / off (they are chosen in start())"""
    old = os.environ.get("CDP_FAST")
    os.environ["CDP_FAST"] = "1" if fast else "0"
    try:
        return blk.start()
    finally:
        if old is None:
            os.environ.pop("CDP_FAST", None)
        else:
            os.environ["CDP_FAST"] = old


def app_msg(dst, payload, noack=False):
    meta = pmt.dict_add(pmt.make_dict(), pmt.intern("dest_id"), pmt.from_long(dst))
    if noack:
        meta = pmt.dict_add(meta, pmt.intern("noack"), pmt.from_long(1))
    return pmt.cons(meta, pmt.init_u8vector(len(payload), list(payload)))


def make_noise(cls, sr=SR):
    return cls(samp_rate=sr, margin_db=6.0, window_s=1.0, percentile=10.0, rise_db_s=1.0, thr_min=-100.0,
               thr_max=-20.0, init_db=-40.0, verbose=0)


def make_corr(cls, thr=0.7):
    ref, _, _ = train_ref()
    return cls(ref=ref, thr=thr, mark_delay=0, verbose=0)


def make_deframer(cls, my_id=0):
    return cls(src_id=my_id, max_bit_errors=1, max_payload_len=2048, bit_rate=375000, fec=31, interleave=True,
               accept_all=1, debug=1, max_dbg_lines=10, stats_every_s=1e9, dup_window_s=2.0, print_rx=0)
