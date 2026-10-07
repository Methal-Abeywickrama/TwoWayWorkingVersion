"""Stage 1 end-to-end test: app (SR-ARQ) over the FDD link, BPSK framer/deframer."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Link, make_pair, transfer, rng  # noqa: E402

GEN = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "generated_hw")


def run(ber_ab, ber_ba, label):
    a, b = make_pair(GEN)
    links = [Link(a, b, "bits", lambda: ber_ab), Link(b, a, "bits", lambda: ber_ba)]
    for ln in links:
        ln.start()
    text = ("Hello from node A over the CDP link. " * 30).encode()
    image = rng.integers(0, 256, 6000, dtype=np.uint8).tobytes()
    chat = b'{"kind": "cdp-chat", "v": 1, "id": "abc123", "text": "reply from B"}'
    ok = True
    for src, dst, name, data in ((a, b, "notes.txt", text), (a, b, "photo.png", image),
                                 (b, a, "chat_1_abc123.json", chat)):
        ev, got, dt = transfer(src, dst, name, data, timeout=90)
        good = ev == "tx_done" and got is not None and got[1] == name and got[2] == data
        ok &= good
        print(f"  [{label}] {src.name}->{dst.name} {name:<20} {len(data):5d} B: sender={ev}, "
              f"receiver={'OK, identical' if good else got and (got[1], len(got[2]))} "
              f"(src_addr {got[3] if got else '-'}) in {dt:.1f} s")
    for ln in links:
        ln.stop = True
    return ok


import numpy as np  # noqa: E402

if __name__ == "__main__":
    print("Stage 1: SR-ARQ app over FDD link (BPSK)")
    r1 = run(1e-6, 1e-6, "clean   BER 1e-6")
    r2 = run(1.5e-4, 1.5e-4, "noisy BER 1.5e-4")
    print("RESULT:", "PASS" if (r1 and r2) else "FAIL")
