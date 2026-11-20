#!/usr/bin/env python3
"""
test_arq_v692.py - two real MAC blocks (epy_csma) and two real framers (epy_block_0_0_1) from the flowgraph, on a
simulated half-duplex channel, to check the v6.9.2 ARQ changes and compare them with v6.9:

    first frame of a transmission with a longer preamble   (CDP_MAC_PRE1, block param first_pre_len)
    a duplicate copy after 4 retries                         (CDP_MAC_DUP_AFTER, dup_after)
    window Block ACK                                         (CDP_MAC_WBA, window_ba)

    python3 tools/test_arq_v692.py              # functional checks + comparison (about 2-4 minutes)
    python3 tools/test_arq_v692.py --quick

The channel copies the loss pattern of the 19 Oct session (debug/20261019_174436_node1), not a radio:
  - TX latency 7.3 ms (the measured own echo), receiver decode latency 1 ms
  - the sender's RX blanking ends 13.2 ms after its burst really ended (12 ms TX delay estimate + 5 ms guard +
    the IIO re-mute, as measured: RX back on 8.5 ms after the MAC's estimated end)
  - the receiver must acquire the signal in the preamble of the first frame of every transmission: acquisition
    time ~ exponential, mean 3.73 ms, so a 128 B (4.1 ms) preamble loses 33 % (19 Oct: 31-36 %); a receiver that
    is blanked during part of the preamble only has the rest
  - frames after the first: 10 % loss in position 2, 6 % later (19 Oct: 7-11 %, 1-7 %)
  - a frame is lost at a node that is transmitting while it is on the air (half duplex)
So the numbers say how the MAC/ARQ logic reacts to that loss pattern; they are not a prediction for the radio.
"""
import argparse
import collections
import contextlib
import io
import math
import os
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import epy_harness as H  # noqa: E402  (puts grstub first on sys.path)
import pmt  # noqa: E402

SR = 1e6
SPB = 32                     # samples per byte (8 bits x 4 samples/symbol)
TX_LAT = 0.0073
DEC_LAT = 0.001
GATE_GUARD = 0.0132          # see above: RX back on 13.2 ms after the real end of the own burst
ACQ_MEAN = 0.00373
P_POS = {1: 0.10}            # loss of frames after the first, by position
P_LATER = 0.06
CHUNK = 1024
MAC_CLS = None
FRAMER_CLS = None


class Node:
    def __init__(self, nid, peer, chan, **mac_kw):
        self.id = nid
        kw = dict(samp_rate=SR, samples_per_byte=SPB, my_id=nid, dest_id=peer, burst_m=4, window_n=16,
                  txop_limit_s=0.25, p_persist=0.2, slot_s=0.002, turnaround_s=0.025, difs_s=0.0, cw_min=15,
                  cw_max=63, k_max=5, retry_limit=7, ba_timeout_s=0.07, phy_preamble_len=128, phy_fec=31,
                  phy_buffer_size=4096, phy_max_payload=2048, tx_mute=0, iio_uri="none", gate_guard_s=GATE_GUARD,
                  verbose=0, stats_every_s=0)
        kw.update(mac_kw)
        self.mac = MAC_CLS(**kw)
        self.fr = FRAMER_CLS(src_id=nid, dest_id=peer, preamble_len=128, max_payload_len=2048, buffer_size=4096,
                             fec=31, interleave=1, samples_per_byte=SPB, verbose=0)
        self.payload_of = {}
        self.delivered = []
        self.status = collections.Counter()
        self.chan = chan
        self.mac.route("to_phy", self._to_phy)
        self.fr.route("pdu_out", self.mac._handlers["phy_in"])
        self.mac.route("pdu_out", lambda m: chan.on_air(self, m))
        self.mac.route("app_out", lambda m: self.delivered.append(bytes(pmt.u8vector_elements(pmt.cdr(m)))))
        self.mac.route("tx_status", self._status)
        self.on_air = []            # (start, end) of own frames, sample time in s
        self.pos = 0
        self.blank = []             # (start, end) RX blanked by the MAC
        with contextlib.redirect_stdout(io.StringIO()):
            self.mac.start()

    def _to_phy(self, msg):
        meta = pmt.car(msg)
        mid = pmt.to_long(pmt.dict_ref(meta, pmt.intern("mac_id"), pmt.PMT_NIL))
        self.payload_of[mid] = bytes(pmt.u8vector_elements(pmt.cdr(msg)))
        self.fr._handlers["msg_in"](msg)

    def _status(self, d):
        self.status[pmt.symbol_to_string(pmt.dict_ref(d, pmt.intern("status"), pmt.PMT_NIL))] += 1

    def send(self, dst, data, ref, seg):
        meta = pmt.make_dict()
        for k, v in (("dest_id", dst), ("app_ref", ref), ("app_seg", seg)):
            meta = pmt.dict_add(meta, pmt.intern(k), pmt.from_long(v))
        self.mac._handlers["app_in"](pmt.cons(meta, pmt.init_u8vector(len(data), list(data))))

    def blanked_at(self, t):
        return any(a <= t < b for a, b in self.blank[-50:])

    def rx_on_since(self, t0, t1):
        """-> the earliest time in [t0, t1] from which this node's RX is not blanked up to t1 (None: blanked at t1)"""
        if self.blanked_at(t1):
            return None
        last = t0
        for a, b in self.blank[-50:]:
            if b > t0 and a < t1:
                last = max(last, b)
        return last

    def transmitting(self, a, b):
        return any(x < b and a < y for x, y in self.on_air[-50:])


class Channel:
    def __init__(self, seed):
        self.rng = np.random.default_rng(seed)
        self.pending = []            # (deliver time, node, msg)
        self.air = []                # frames on the air, judged when they have ended
        self.nodes = []
        self.lost = collections.Counter()
        self.sent = collections.Counter()

    def on_air(self, node, msg):
        """a coded frame published by node's MAC (frames of one transmission arrive one after the other)"""
        meta = pmt.car(msg)
        nbytes = int(pmt.length(pmt.cdr(msg)))
        raw = bytes(pmt.u8vector_elements(pmt.cdr(msg)))
        pre = len(raw) - len(raw.lstrip(b"\xff"))            # preamble length, as the framer built it
        mid = pmt.to_long(pmt.dict_ref(meta, pmt.intern("mac_id"), pmt.PMT_NIL))
        now = node.mac._t
        if node.on_air and node.on_air[-1][1] > now:          # continues the current transmission
            start, first = node.on_air[-1][1], False
        else:
            start, first = now + TX_LAT, True
            node.pos = 0
        end = start + nbytes * SPB / SR
        node.on_air.append((start, end))
        pos = node.pos
        node.pos += 1
        peer = next(n for n in self.nodes if n is not node)
        payload = node.payload_of.pop(mid)
        is_ba = (payload[0] & 0x03) in (2, 3)
        sync_t = start + pre * SPB / SR
        self.sent["BA" if is_ba else f"pos{pos + 1}"] += 1
        self.air.append((end, node, peer, start, end, sync_t, first, pos, is_ba, payload))

    def _judge(self, node, peer, start, end, sync_t, first, pos, is_ba, payload):
        """called once the frame has ended on the air (the peer's RX blanking up to then is known)"""
        why = None
        if peer.transmitting(start, end):
            why = "half duplex"
        elif first:
            on = peer.rx_on_since(start, sync_t)
            have = 0.0 if on is None else sync_t - on
            if self.rng.exponential(ACQ_MEAN) > have:
                why = "acquisition"
        elif self.rng.random() < P_POS.get(pos, P_LATER):
            why = "noise"
        if why:
            self.lost[("BA" if is_ba else f"pos{pos + 1}", why)] += 1
            return
        md = pmt.make_dict()
        for k, v in (("src_id", node.id), ("dest_id", peer.id), ("msg_id", 0), ("fec_corrected", 0)):
            md = pmt.dict_add(md, pmt.intern(k), pmt.from_long(v))
        self.pending.append((end + DEC_LAT, peer, pmt.cons(md, pmt.init_u8vector(len(payload), list(payload)))))

    def deliver(self, t):
        done = [f for f in self.air if f[0] <= t]
        self.air = [f for f in self.air if f[0] > t]
        for f in done:
            self._judge(*f[1:])
        due = [p for p in self.pending if p[0] <= t]
        self.pending = [p for p in self.pending if p[0] > t]
        for _, node, msg in due:
            node.mac._handlers["rx_in"](msg)


def run(cfg, n_frames=300, seconds=120.0, seed=3, payload=1017, both_ways=False):
    """-> result dict. cfg: kwargs for both MACs, or {"a": {...}, "b": {...}} per node."""
    chan = Channel(seed)
    ca, cb = (cfg.get("a", {}), cfg.get("b", {})) if "a" in cfg else (cfg, cfg)
    a, b = Node(1, 32, chan, **ca), Node(32, 1, chan, **cb)
    chan.nodes = [a, b]
    rng = np.random.default_rng(seed + 100)
    msgs = [bytes([i & 0xFF, i >> 8]) + rng.integers(0, 256, payload - 2, dtype=np.uint8).tobytes()
            for i in range(n_frames)]
    for i, m in enumerate(msgs):
        a.send(32, m, 7, i)
        if both_ways:
            b.send(1, m, 9, i)
    noise = (10 ** (-62 / 20)) / np.sqrt(2)
    t, t_done = 0.0, None
    nrng = np.random.default_rng(seed + 1)
    while t < seconds:
        for node in (a, b):
            x = ((nrng.standard_normal(CHUNK) + 1j * nrng.standard_normal(CHUNK)) * noise).astype(np.complex64)
            ts = node.mac._n_abs / SR + np.arange(CHUNK) / SR
            for other in (a, b):                       # any transmission on the air (own = echo)
                for s, e in other.on_air[-8:]:
                    m = (ts >= s) & (ts < e)
                    if m.any():
                        x[m] += 10 ** (-30 / 20)
            out = np.empty_like(x)
            node.mac.work([x], [out])
            z = np.flatnonzero(out == 0)
            if z.size:                                  # RX blanking intervals, from the zeroed output
                t0 = ts[z[0]]
                t1 = ts[z[-1]] + 1 / SR
                if node.blank and node.blank[-1][1] >= t0 - 2 / SR:
                    node.blank[-1] = (node.blank[-1][0], t1)
                else:
                    node.blank.append((t0, t1))
        t = a.mac._t
        chan.deliver(t)
        if t_done is None and len(b.delivered) >= n_frames and (not both_ways or len(a.delivered) >= n_frames):
            t_done = t
        if t_done is not None and t > t_done + 1.0:
            break
        if not a.mac._has_work() and not b.mac._has_work() and not chan.pending and not a.mac._self_tx \
                and not b.mac._self_tx and a.mac._await is None and b.mac._await is None and t > 1.0:
            break
    got = b.delivered
    st = a.mac._st
    res = {"delivered": len(got), "unique": len(set(got)), "in_order": got == [m for m in msgs if m in set(got)],
           "intact": set(got) <= set(msgs), "missing": n_frames - len(set(got)), "t_done": t_done, "t_end": t,
           "tx": st["tx_D"], "retx": st["tx_retx"], "dups_sent": st["tx_dup"], "acked": st["acked"],
           "retry_drop": st["retry_drop"], "ba_rx": st["ba_rx"], "ba_late": st["late_ba"],
           "ba_timeout": st["ba_timeout"], "rx_dup": b.mac._st["rx_dup"], "status": dict(a.status),
           "lost": dict(chan.lost), "sent": dict(chan.sent)}
    if both_ways:
        res["delivered_back"] = len(a.delivered)
        res["intact_back"] = set(a.delivered) <= set(msgs) and a.delivered == [m for m in msgs if m in set(a.delivered)]
    return res


def main():
    global MAC_CLS, FRAMER_CLS
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()
    MAC_CLS = H.load("epy_csma", fast=False)
    FRAMER_CLS = H.load("epy_block_0_0_1")
    n = 120 if a.quick else 240               # below the MAC queue limit (256)
    fails = 0

    def check(c, what):
        nonlocal fails
        print(("  ok    " if c else "  FAIL  ") + what, flush=True)
        fails += not c

    old = dict(first_pre_len=0, dup_after=0, window_ba=0, pre_in=128)
    new = dict(first_pre_len=256, dup_after=4, window_ba=1, pre_in=128)
    v693 = dict(new, pre_in=64)
    print("functional checks")
    # 1. framer: preamble per frame
    fr = FRAMER_CLS(src_id=1, dest_id=2, preamble_len=128, max_payload_len=2048, buffer_size=4096, fec=31,
                    interleave=1, samples_per_byte=SPB, verbose=0)
    out = []
    fr.route("pdu_out", out.append)
    for pre in (None, 256):
        md = pmt.make_dict()
        if pre:
            md = pmt.dict_add(md, pmt.intern("pre_len"), pmt.from_long(pre))
        fr._handlers["msg_in"](pmt.cons(md, pmt.init_u8vector(5, list(b"hello"))))
    raws = [bytes(pmt.u8vector_elements(pmt.cdr(m))) for m in out]
    pres = [len(r) - len(r.lstrip(b"\xff")) for r in raws]
    check(pres == [128, 256], f"framer preamble 128 B by default, 256 B with pre_len metadata ({pres})")
    mac = MAC_CLS(tx_mute=0, iio_uri="none", verbose=0, stats_every_s=0, first_pre_len=256, window_ba=1)
    check(mac.phy_bytes(5, 256) == len(raws[1]) and mac.phy_bytes(5) == len(raws[0]),
          f"MAC air-time formula matches the framer with and without the longer preamble "
          f"({mac.phy_bytes(5)} / {mac.phy_bytes(5, 256)} B vs {len(raws[0])} / {len(raws[1])} B)")
    # 2. window Block ACK encode / parse
    M = __import__('types').SimpleNamespace(**MAC_CLS.__init__.__globals__)     # the block's module namespace
    raw = M.wba_frame(513, 65530, 16, 0b1010_0000_0000_0110)
    h = M.parse_mac(raw)
    check(h["type"] == M.T_WBA and h["bsn"] == 513 and h["rws"] == 65530 and h["nbits"] == 16
          and h["wbits"] == 0b1010_0000_0000_0110 and len(raw) == 10,
          "window Block ACK: 10 B, BSN / SSN / 16-bit bitmap survive encode -> parse")
    check([M.wba_has(h, s) for s in (65529, 65530, 65531, 65532, 3, 5, 9)] == [True, False, True, True, False, False,
                                                                                 True]
          and M.wba_has(h, (65530 + 13) & 0xFFFF) and M.wba_has(h, (65530 + 15) & 0xFFFF),
          "window Block ACK across the 16-bit wrap: SN before SSN acked, bits 1, 2, 13, 15 acked")
    old_ba = M.parse_mac(M.ba_frame(7, 100, 4, 0b1011))
    check(old_ba["type"] == M.T_BA and old_ba["bits"] == 0b1011, "the v6.9 Block ACK still parses")
    # 3. TxSession.on_window_ack: frames of earlier bursts acked, missing frame of this burst failed once
    s = M.TxSession(2, 16, 7)
    for i in range(6):
        s.backlog.append((b"x", {}, 0.0))
    s.admit()
    rec = [s.frames[i] for i in range(6)]
    for r in rec[:3]:
        r["state"] = "retx"; r["retries"] = 1          # burst 1 lost its Block ACK
    for r in rec[3:]:
        r["state"] = "out"
    h = {"rws": 1, "nbits": 16, "wbits": 0b10110}      # receiver: 0 in order; has 2, 3, 5 (bits 1, 2, 4)
    acked, nacked, dropped = s.on_window_ack(rec[3:], h)
    check(sorted(r["sn"] for r in acked) == [0, 2, 3, 5] and [r["sn"] for r in nacked] == [4]
          and rec[1]["state"] == "retx" and rec[4]["retries"] == 1,
          "window ACK: SN 0, 2 (earlier burst) and 3, 5 acked, SN 4 of this burst retried, SN 1 still waits")
    # 4. duplicate in a burst counts once
    s2 = M.TxSession(2, 16, 7)
    s2.backlog.append((b"y", {}, 0.0)); s2.admit()
    r0 = s2.frames[0]; r0["state"] = "out"; r0["retries"] = 4
    nacked, dropped = s2.on_no_ba([r0, r0])
    check(r0["retries"] == 5 and len(nacked) == 1, "a frame sent twice in a burst is charged one retry, not two")
    acked, _, _ = s2.on_block_ack([r0, r0], 0b10, 0)
    check([x["sn"] for x in acked] == [0], "positional Block ACK: the second copy's bit acknowledges the frame")

    print("\ntwo MACs + framers on the simulated channel (loss pattern of 19 Oct), "
          f"{n} frames of 1017 B node 1 -> node 32")
    rows = []
    variants = [
        ("v6.9 (SIFS 1 ms)", dict(old, sifs_s=0.001)),
        ("v6.9, SIFS 12 ms", dict(old, sifs_s=0.012)),
        ("v6.9.2 (SIFS 1 ms)", dict(new, sifs_s=0.001)),
        ("v6.9.2, SIFS 12 ms", dict(new, sifs_s=0.012)),
        ("only longer preamble", dict(old, first_pre_len=256, sifs_s=0.001)),
        ("only duplicates", dict(old, dup_after=4, sifs_s=0.001)),
        ("only window Block ACK", dict(old, window_ba=1, sifs_s=0.001)),
        ("v6.9.3: + 64 B inside bursts, SIFS 12", dict(v693, sifs_s=0.012)),
        ("v6.9.3: + 32 B inside bursts, SIFS 12", dict(v693, pre_in=32, sifs_s=0.012)),
    ]
    for name, cfg in variants:
        t0 = time.time()
        r = run(cfg, n_frames=n, seconds=200 if not a.quick else 90)
        rows.append((name, r))
        thr = (r["delivered"] * 1017 * 8 / r["t_done"] / 1e3) if r["t_done"] else 0
        ba = r["ba_rx"] / max(1, r["ba_rx"] + r["ba_timeout"])
        print(f"  {name:38s} delivered {r['unique']:3d}/{n}  dropped {r['retry_drop']:3d}  "
              f"{'done %5.1f s' % r['t_done'] if r['t_done'] else 'not done   '}  {thr:5.1f} kbit/s  "
              f"Block ACKs back {ba:4.0%}  frames sent {r['tx']:4d} (dup copies {r['dups_sent']:3d})  "
              f"needless resends at receiver {r['rx_dup']:4d}   [{time.time() - t0:.0f} s]", flush=True)
        check(r["intact"] and r["in_order"] and r["unique"] == r["delivered"],
              f"{name}: everything delivered is intact, in order, once")
    by = dict(rows)
    check(by["v6.9.2 (SIFS 1 ms)"]["unique"] >= by["v6.9 (SIFS 1 ms)"]["unique"]
          and by["v6.9.2 (SIFS 1 ms)"]["retry_drop"] <= by["v6.9 (SIFS 1 ms)"]["retry_drop"],
          "v6.9.2 drops fewer frames than v6.9 on the same channel")
    check(by["only duplicates"]["dups_sent"] > 0, "duplicates are sent once a frame reached 4 retries")
    check(by["v6.9.3: + 64 B inside bursts, SIFS 12"]["t_done"] is not None and
          by["v6.9.3: + 64 B inside bursts, SIFS 12"]["t_done"] < by["v6.9.2, SIFS 12 ms"]["t_done"],
          "short preambles inside bursts finish the same transfer sooner")

    print("\nboth directions at once, and a v6.9.2 node with a node that sends the v6.9 Block ACK")
    r = run(dict(v693, sifs_s=0.012), n_frames=n // 2, seconds=200, both_ways=True)
    check(r["intact"] and r["in_order"] and r.get("intact_back"),
          f"both ways: {r['unique']} + {r['delivered_back']} of {n // 2} each way delivered, intact and in order")
    r = run({"a": dict(v693, sifs_s=0.012), "b": dict(v693, window_ba=0, sifs_s=0.012)}, n_frames=n // 2, seconds=200)
    check(r["intact"] and r["in_order"] and r["unique"] == n // 2,
          f"receiver sends v6.9 Block ACKs, sender v6.9.2: {r['unique']}/{n // 2} delivered in order")
    print("\nPASS test_arq_v692" if fails == 0 else f"\nFAIL test_arq_v692 ({fails})")
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
