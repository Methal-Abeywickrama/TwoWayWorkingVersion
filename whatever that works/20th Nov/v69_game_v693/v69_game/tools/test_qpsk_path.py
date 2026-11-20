#!/usr/bin/env python3
"""
test_qpsk_path.py - the BPSK / QPSK switch of v6.9.3 without GNU Radio: the flowgraph's own variable expressions
(MOD, BPS, MOD_CONST, TRAIN_BITS, TRAIN_SYMS, TRAIN_REF, PHY_PRE, PRE1, PRE_IN) are evaluated from the .grc, and
frames go through:

  real framer (bursts: long first preamble, short preambles inside, one postamble at the end)
  -> bits -> GNU Radio's constellation_modulator chain, re-built here (packed_to_unpacked MSB first, map_bb with the
     constellation's pre_diff_code, diff_encoder_bb(arity), chunks_to_symbols) -> RRC, 4 samples per symbol
  -> channel (unknown phase, noise) -> RRC matched filter, 2 samples per symbol
  -> real training-word correlator (epy_block_corr, with the flowgraph's TRAIN_REF)
  -> decisions after a phase lock with the usual 90/180 degree ambiguity (what the Costas loop leaves)
  -> constellation_decoder (nearest point) -> diff_decoder_bb(arity) -> map_bb (inverse Gray) -> unpack_k_bits(BPS)
  -> real deframer (epy_block_0_0_0)

Not covered (GNU Radio C++ blocks): AGC, FLL, symbol sync, the equalizer and the Costas loop itself.
    python3 tools/test_qpsk_path.py
"""
import os
import sys
from pathlib import Path

import numpy as np
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import epy_harness as H  # noqa: E402
import pmt  # noqa: E402

GRC = HERE.parent / "v6.9_cdp_link.grc"
fails = 0


def check(c, what):
    global fails
    print(("  ok    " if c else "  FAIL  ") + what, flush=True)
    fails += not c


class Const:
    """digital.constellation_rect(...).base(): amplitude-normalised points, pre_diff_code, arity"""

    def __init__(self, pts, code, *rest):
        p = np.array(pts, complex)
        self._p = p / np.mean(np.abs(p))
        self._code = list(code)

    def base(self):
        return self

    def points(self):
        return list(self._p)

    def pre_diff_code(self):
        return self._code

    def arity(self):
        return len(self._p)


def grc_vars(mod):
    """the flowgraph's variables, evaluated like GRC does (in dependency order), with CDP_MOD=mod"""
    os.environ["CDP_MOD"] = mod
    for k in ("CDP_MAC_PRE1", "CDP_MAC_PRE_IN"):
        os.environ.pop(k, None)
    g = yaml.safe_load(open(GRC))
    want = {"alpha", "RC_X", "RC_PULSE", "RX_AMP", "MOD", "BPS", "BPSK_CONST", "QPSK_CONST", "MOD_CONST",
            "TRAIN_BITS", "TRAIN_SYMS", "TRAIN_REF", "PHY_PRE", "PRE1", "PRE_IN", "sps", "BUFFER_SIZE", "corr_thr"}
    exprs = {}
    for b in g["blocks"]:
        if b["name"] not in want or b.get("states", {}).get("state") != "enabled":
            continue
        p = b["parameters"]
        if b["id"] == "variable_constellation_rect":
            exprs[b["name"]] = f"Const({p['const_points']}, {p['sym_map']})"
        else:
            exprs[b["name"]] = str(p["value"])
    ns = {"np": np, "os": os, "Const": Const}
    left = dict(exprs)
    while left:
        done = []
        for k, e in left.items():
            try:
                ns[k] = eval(e, ns)
                done.append(k)
            except NameError:
                pass
        if not done:
            raise RuntimeError(f"cannot evaluate {sorted(left)}")
        for k in done:
            del left[k]
    return ns


def rrc(sps, alpha=0.35, span=11):
    t = (np.arange(span * sps + 1) - span * sps / 2) / sps
    h = np.zeros_like(t)
    for i, x in enumerate(t):
        if abs(x) < 1e-9:
            h[i] = 1 - alpha + 4 * alpha / np.pi
        elif abs(abs(x) - 1 / (4 * alpha)) < 1e-9:
            h[i] = alpha / np.sqrt(2) * ((1 + 2 / np.pi) * np.sin(np.pi / (4 * alpha)) + (1 - 2 / np.pi) * np.cos(np.pi / (4 * alpha)))
        else:
            h[i] = (np.sin(np.pi * x * (1 - alpha)) + 4 * alpha * x * np.cos(np.pi * x * (1 + alpha))) / (np.pi * x * (1 - (4 * alpha * x) ** 2))
    return h / np.sqrt(np.sum(h ** 2))


def modulate(bits, C, bps, sps=4, state=0):
    """generic_mod: bits (MSB first per symbol) -> pre_diff map -> diff encoder -> points -> RRC"""
    v = bits.reshape(-1, bps) @ (1 << np.arange(bps - 1, -1, -1))
    m = np.array(C.pre_diff_code())[v]
    idx = (state + np.cumsum(m)) % C.arity()
    sym = np.array(C.points())[idx]
    up = np.zeros(len(sym) * sps, complex)
    up[::sps] = sym
    return np.convolve(up, rrc(sps)), int(idx[-1])


def demod_bits(y2, C, bps, mod_inv, rng):
    """2 samples / symbol (on time at even indices) -> phase lock with ambiguity -> decisions -> bits"""
    s = y2[0::2]
    M = C.arity()
    p0 = np.array(C.points())[0]
    ph = (np.angle(np.sum(s ** M)) - np.angle(p0 ** M)) / M  # a locked Costas loop: points back on the constellation,
    #                                                           up to an unknown multiple of 360/M degrees
    s = s * np.exp(-1j * (ph + rng.integers(0, M) * 2 * np.pi / M))
    pts = np.array(C.points())
    idx = np.argmin(np.abs(s[:, None] - pts[None, :]), axis=1)
    d = (idx - np.concatenate(([idx[0]], idx[:-1]))) % M  # diff_decoder_bb
    v = np.array(mod_inv)[d]                              # map_bb
    return ((v[:, None] >> np.arange(bps - 1, -1, -1)) & 1).astype(np.uint8).reshape(-1)


def run_mode(mod, rng):
    V = grc_vars(mod)
    bps, C, sps = V["BPS"], V["MOD_CONST"], int(V["sps"])
    mod_inv = [0, 1, 3, 2] if bps == 2 else [0, 1]
    print(f"\n{mod.upper()}: BPS {bps}, preamble {V['PHY_PRE']} B, first {V['PRE1']} B, inside a burst {V['PRE_IN']} B, "
          f"{len(V['TRAIN_SYMS'])} training symbols")
    check(abs(np.mean(np.abs(V["TRAIN_SYMS"])) - 1) < 1e-9 and len(V["TRAIN_SYMS"]) == 32,
          "training sequence: 32 unit-amplitude symbols")
    # 0xFF preamble: 180 degrees per symbol in both modes
    x, _ = modulate(np.ones(8 * 16, np.uint8), C, bps)
    s = x[len(rrc(sps)) // 2::sps][:8 * 16 // bps][4:-4]
    steps = np.angle(s[1:] / s[:-1], deg=True)
    check(np.allclose(np.abs(steps), 180, atol=1), f"0xFF preamble alternates (phase step {np.median(np.abs(steps)):.0f} deg)")
    # bursts from the real framer: first frame long preamble, the others short, one postamble at the end
    F = H.load("epy_block_0_0_1")
    fr = F(src_id=1, dest_id=255, preamble_len=V["PHY_PRE"], repeat_count=1, max_payload_len=2048, preamble_byte=0xFF,
           buffer_size=4096, postamble_byte=0xFF, train_enable=1, fec=31, interleave=True,
           samples_per_byte=8 * sps // bps, verbose=0)
    payloads, stream = [], [np.zeros(4000, np.uint8)]
    for burst in range(6):
        n = 4
        for i in range(n):
            pl = bytes(rng.integers(0, 256, int(rng.choice([40, 600, 1030])), dtype=np.uint8))
            payloads.append(pl)
            hdr = len(pl).to_bytes(2, "big") + bytes([0, 1, len(payloads) & 0xFF, 0, 1])
            pre = V["PRE1"] if i == 0 else V["PRE_IN"]
            post = None if i == n - 1 else 0
            stream.append(np.unpackbits(np.frombuffer(fr.build_frame(hdr, pl, pre, post), np.uint8)))
        stream.append(np.zeros(2000 * bps, np.uint8))          # silence between bursts (zeros = "no carrier")
    bits = np.concatenate(stream)
    bits = bits[:len(bits) - len(bits) % bps]
    x, _ = modulate(bits, C, bps)
    x = x * np.exp(1j * rng.uniform(0, 2 * np.pi))
    x = x + (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x))) * 0.05
    h = rrc(sps)
    y = np.convolve(x, h)[len(h) - 1:]                         # matched filter, symbol k at index k*sps
    y2 = (y[::sps // 2] / np.sqrt(np.mean(np.abs(y[::sps]) ** 2))).astype(np.complex64)
    # correlator (real block) on the 2 samples/symbol stream
    corr = H.load("epy_block_corr")(ref=V["TRAIN_REF"], thr=float(V["corr_thr"]), mark_delay=0, verbose=0)
    out, _ = H.run_sync(corr, y2, out_dtype=np.complex64)
    tags = [t for t in corr.out_tags if t[1] == "corr_est"]
    check(len(tags) >= len(payloads), f"correlator finds the training sequence: {len(tags)} tags for {len(payloads)} "
                                      f"frames (also the short-preamble ones inside bursts)")
    # decisions -> bits -> real deframer
    rx_bits = demod_bits(y2, C, bps, mod_inv, rng)
    D = H.load("epy_block_0_0_0")
    dfr = D(src_id=0, max_bit_errors=1, max_payload_len=2048, bit_rate=250000 * bps, fec=31, interleave=True,
            accept_all=1, debug=0, max_dbg_lines=0, stats_every_s=1e9, dup_window_s=0.0, print_rx=0)
    got = []
    dfr.route("pdu_out", lambda m: got.append(bytes(pmt.u8vector_elements(pmt.cdr(m)))))
    H.run_sync(dfr, rx_bits)
    check(got == payloads, f"deframer: {len(got)}/{len(payloads)} frames back, bytes identical, in order")
    # framer sizes vs the MAC's air-time formula
    M = H.load("epy_csma")(tx_mute=0, iio_uri="none", verbose=0, stats_every_s=0, samples_per_byte=8 * sps // bps,
                           phy_preamble_len=V["PHY_PRE"], first_pre_len=V["PRE1"], pre_in=V["PRE_IN"])
    ok = True
    for pl in (b"x" * 13, b"y" * 1030):
        hdr = len(pl).to_bytes(2, "big") + bytes([0, 1, 0, 0, 1])
        for pre, post in ((None, None), (V["PRE1"], None), (V["PRE_IN"], 0), (V["PRE_IN"], 77)):
            ok &= len(fr.build_frame(hdr, pl, pre, post)) == M.phy_bytes(len(pl), pre, post)
    check(ok, "MAC air-time formula = framer length for every preamble / postamble combination")
    raws = [M.phy_raw(1030, V["PRE1"])] + [M.phy_raw(1030, V["PRE_IN"])] * 3
    post = M._burst_post(sum(raws))
    unit = 4096 // (8 * sps // bps)
    check((sum(raws) + post) % unit == 0 and post >= unit, f"a 4-frame burst is padded to whole Pluto buffers "
                                                           f"({sum(raws) + post} B = {(sum(raws) + post) // unit} x {unit} B)")


def main():
    rng = np.random.default_rng(7)
    run_mode("bpsk", rng)
    run_mode("qpsk", rng)
    os.environ.pop("CDP_MOD", None)
    print("\nPASS test_qpsk_path" if fails == 0 else f"\nFAIL test_qpsk_path ({fails})")
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
