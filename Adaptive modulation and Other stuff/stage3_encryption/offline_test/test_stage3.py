"""Stage 3 end-to-end test: adaptive link + application-layer encryption."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Link, make_pair, transfer, rng, pmt, gr  # noqa: E402

GEN = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "generated_hw")
SECRET = b"TOP SECRET: the meeting moved to 3 pm. " * 20


def air_capture(node):
    """Everything the node hands to its framer (= what goes on air, after the transport header)."""
    seen = bytearray()
    gr.msg_connect(node.blocks["epy_block_2"], "pdus", lambda m: seen.extend(bytes(pmt.cdr(m).val)), None)
    return seen


def scenario(label, env_a, env_b, expect_ok):
    a, b = make_pair(GEN, env_a, env_b)
    air = air_capture(a)
    for ln in (Link(a, b, "sym", lambda: 20), Link(b, a, "sym", lambda: 20)):
        ln.start()
    image = rng.integers(0, 256, 8000, dtype=np.uint8).tobytes()
    res = []
    for src, dst, name, data in ((a, b, "secret.txt", SECRET), (b, a, "reply.txt", b"ack: got it " * 10),
                                 (a, b, "photo.png", image)):
        ev, got, dt = transfer(src, dst, name, data, timeout=90)
        good = ev == "tx_done" and got is not None and got[1] == name and got[2] == data
        res.append(good)
        shown = "identical" if good else (got and f"name={got[1][:28]!r} {len(got[2])} B, differs")
        print(f"  {src.name}->{dst.name} {name:<11} sender={ev}, receiver: {shown}")
    leaked = b"TOP SECRET" in bytes(air)
    print(f"  plaintext visible in A's on-air bytes: {'YES' if leaked else 'no'}")
    ok = res == list(expect_ok)
    print(f"  => {'as expected' if ok and not leaked else 'UNEXPECTED'}")
    return ok and not leaked


if __name__ == "__main__":
    print("Stage 3: adaptive link + application-layer encryption")
    A = {"TL_PRIVKEY": "241", "TL_PUBKEY": "17", "TL_PEER_PUBKEY": "3"}     # A: private 241, public 17
    B = {"TL_PRIVKEY": "171", "TL_PUBKEY": "3", "TL_PEER_PUBKEY": "17"}     # B: private 171, public 3
    print("--- 1) two key pairs (A: 17/241, B: 3/171), each encrypts with the other's public key")
    r1 = scenario("pairs", A, B, (True, True, True))
    print("--- 2) defaults (both nodes 17/241)")
    r2 = scenario("defaults", {}, {}, (True, True, True))
    print("--- 3) B uses the wrong private key (241 instead of 171): A->B garbled, B->A fine")
    r3 = scenario("wrong key", A, {**B, "TL_PRIVKEY": "241"}, (False, True, False))
    print("--- 4) the old setting PUBLIC_KEY_PEER = 0")
    r4 = scenario("key 0", {"TL_PEER_PUBKEY": "0"}, {"TL_PEER_PUBKEY": "0"}, (False, False, False))
    print("RESULT:", "PASS" if all((r1, r2, r3, r4)) else f"FAIL {(r1, r2, r3, r4)}")
