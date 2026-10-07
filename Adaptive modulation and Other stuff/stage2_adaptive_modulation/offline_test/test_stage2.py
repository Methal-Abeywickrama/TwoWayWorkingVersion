"""Stage 2 end-to-end test: SR-ARQ app over the adaptive BPSK/QPSK link."""
import collections
import os
import sys
import threading
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Link, make_pair, transfer, rng, pmt, gr  # noqa: E402

GEN = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "generated_hw")


def mcs_counter(node):
    """Count DATA frames (>= 32 B) received by `node`, per modulation."""
    c = collections.Counter()

    def on_pdu(m):
        meta = pmt.car(m)
        if len(pmt.cdr(m).val) >= 32:
            c["QPSK" if pmt.to_long(pmt.dict_ref(meta, pmt.intern("mcs"), pmt.from_long(0))) else "BPSK"] += 1
    gr.msg_connect(node.blocks["epy_block_0_0_0"], "pdu_out", on_pdu, None)
    return c


def scenario(label, snr_ab, snr_ba, files, change=None, env=None):
    a, b = make_pair(GEN, env, env)
    q = {"ab": snr_ab, "ba": snr_ba}
    at_b, at_a = mcs_counter(b), mcs_counter(a)
    links = [Link(a, b, "sym", lambda: q["ab"]), Link(b, a, "sym", lambda: q["ba"])]
    for ln in links:
        ln.start()
    if change:
        delay, key, val = change
        threading.Timer(delay, lambda: q.__setitem__(key, val)).start()
    ok = True
    print(f"--- {label}")
    for src, dst, name, data in files(a, b):
        ev, got, dt = transfer(src, dst, name, data, timeout=120)
        good = ev == "tx_done" and got is not None and got[1] == name and got[2] == data
        ok &= good
        print(f"  {src.name}->{dst.name} {name:<11} {len(data):6d} B: sender={ev}, "
              f"receiver={'identical' if good else got and (got[1], len(got[2]))}, {dt:4.1f} s")
    print(f"  DATA frames received by B (A->B @ {q['ab']} dB now): {dict(at_b)}")
    print(f"  DATA frames received by A (B->A @ {q['ba']} dB): {dict(at_a)}")
    for ln in links:
        ln.stop = True
    return ok, at_b


text = ("Hello from node A over the adaptive CDP link. " * 40).encode()
image = rng.integers(0, 256, 20000, dtype=np.uint8).tobytes()


def both_ways(a, b):
    return [(a, b, "hello.txt", text), (b, a, "reply.txt", text[:600]), (a, b, "photo.png", image)]


if __name__ == "__main__":
    print("Stage 2: SR-ARQ app over the adaptive link")
    r1, c1 = scenario("1) good link both ways (20 dB): should move to QPSK", 20, 20, both_ways)
    r2, c2 = scenario("2) A->B drops from 20 dB to 11 dB 1.5 s into the transfers", 20, 20, both_ways,
                      change=(1.5, "ab", 11))
    r3, c3 = scenario("3) weak link both ways (10 dB): must stay BPSK", 10, 10, both_ways)
    checks = [r1 and c1["QPSK"] > c1["BPSK"], r2, r3 and c3["QPSK"] == 0]
    print("RESULT:", "PASS" if all(checks) else f"FAIL {checks}")
