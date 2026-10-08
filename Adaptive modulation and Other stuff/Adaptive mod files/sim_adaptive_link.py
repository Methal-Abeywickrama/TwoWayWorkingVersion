"""Offline test for the adaptive framer/deframer epy blocks (no GNU Radio or SDR needed).
Put this next to transeciever_epy_block_0_0.py and transeciever_epy_block_0_0_0.py and run:
    python3 sim_adaptive_link.py
Symbol-level channel: gain, carrier phase drift + small CFO, AWGN at a chosen Es/N0."""
import sys, types


class P:  # generic pmt object
    def __init__(self, kind, val):
        self.kind, self.val = kind, val
    def __repr__(self):
        return f"P({self.kind},{self.val!r})"


pmt = types.ModuleType("pmt")
pmt.PMT_NIL = P("nil", None)
pmt.intern = lambda s: P("sym", s)
pmt.symbol_to_string = lambda p: p.val
pmt.is_symbol = lambda p: p.kind == "sym"
pmt.cons = lambda a, b: P("pair", (a, b))
pmt.is_pair = lambda p: p.kind == "pair"
pmt.car = lambda p: p.val[0]
pmt.cdr = lambda p: p.val[1]
pmt.init_u8vector = lambda n, l: P("u8", bytes(l))
pmt.is_u8vector = lambda p: p.kind == "u8"
pmt.u8vector_elements = lambda p: list(p.val)
pmt.init_c32vector = lambda n, l: P("c32", list(l))
pmt.from_long = lambda v: P("long", int(v))
pmt.from_double = lambda v: P("double", float(v))
pmt.to_long = lambda p: int(p.val)
pmt.to_double = lambda p: float(p.val)
pmt.make_dict = lambda: P("dict", {})
def _dict_add(d, k, v):
    nd = dict(d.val); nd[k.val] = v; return P("dict", nd)
pmt.dict_add = _dict_add
pmt.is_dict = lambda p: p.kind == "dict"
pmt.dict_has_key = lambda d, k: k.val in d.val
pmt.dict_ref = lambda d, k, default: d.val.get(k.val, default)
pmt.to_python = lambda p: p.val
sys.modules["pmt"] = pmt


class _Block:
    def __init__(self, name=None, in_sig=None, out_sig=None):
        self._handlers, self._out = {}, {}
    def message_port_register_in(self, p): pass
    def message_port_register_out(self, p): self._out.setdefault(p.val, [])
    def set_msg_handler(self, p, h): self._handlers[p.val] = h
    def message_port_pub(self, p, msg):
        for cb in self._out.get(p.val, []):
            cb(msg)
    def connect_out(self, port, cb): self._out.setdefault(port, []).append(cb)
    def post(self, port, msg): self._handlers[port](msg)


gr = types.ModuleType("gnuradio.gr")
gr.basic_block = _Block
gr.sync_block = _Block
gnuradio = types.ModuleType("gnuradio")
gnuradio.gr = gr
sys.modules["gnuradio"] = gnuradio
sys.modules["gnuradio.gr"] = gr

import io, contextlib, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pmt
import transeciever_epy_block_0_0 as txm
import transeciever_epy_block_0_0_0 as rxm

rng = np.random.default_rng(1)


class Channel:
    """Symbol-level channel: gain 2 (CMA modulus 4), random/drifting phase, small CFO, AWGN."""
    def __init__(self, esn0_db, cfo=2e-4, drift=0.002):
        self.esn0_db, self.cfo, self.drift, self.ph = esn0_db, cfo, drift, rng.uniform(0, 2 * np.pi)
    def run(self, s):
        n = len(s)
        walk = np.cumsum(rng.normal(0, self.drift, n))
        ph = self.ph + 2 * np.pi * self.cfo * np.arange(n) + walk
        self.ph = ph[-1]
        sig = 2.0 * s * np.exp(1j * ph)
        sigma = 2.0 / np.sqrt(10 ** (self.esn0_db / 10))
        noise = sigma / np.sqrt(2) * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
        return (sig + noise).astype(np.complex64)
    def idle(self, n):
        sigma = 2.0 / np.sqrt(10 ** (self.esn0_db / 10))
        return (sigma / np.sqrt(2) * (rng.standard_normal(n) + 1j * rng.standard_normal(n))).astype(np.complex64)


def feed(rx, x):
    i = 0
    while i < len(x):
        k = int(rng.integers(500, 9000))
        rx.work([x[i:i + k]], [])
        i += k


def make_pair(tx_kw, rx_kw):
    tx = txm.PacketFramerTX(**tx_kw)
    rx = rxm.PacketDeframerRX(**rx_kw)
    bursts = []
    tx.connect_out("pdu_out", lambda m: bursts.append(np.array(pmt.cdr(m).val, dtype=np.complex64)))
    got = []
    rx.connect_out("pdu_out", lambda m: got.append(bytes(pmt.cdr(m).val)))
    return tx, rx, bursts, got


def run_frames(esn0, mcs, n_reps=40, msg=b"x" * 50, preamble=32, quiet=True):
    tx, rx, bursts, got = make_pair(
        dict(peer_id=0, preamble_len=preamble, repeat_count=n_reps, mcs_mode=mcs),
        dict(peer_id=0))
    ch = Channel(esn0)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        tx.post("msg_in", pmt.intern(msg.decode()))
        stream = [ch.idle(3000)]
        for b in bursts:
            stream += [ch.run(b), ch.idle(int(rng.integers(50, 2000)))]
        stream.append(ch.idle(5000))
        feed(rx, np.concatenate(stream))
        rx._flush_summary()
    return rx, got, out.getvalue()


def table():
    # 1. Functional check at high SNR, both modes, plus SNR estimator accuracy
    for mcs in (0, 1):
        rx, got, log = run_frames(25, mcs, n_reps=20, msg=b"Hello adaptive link! 0123456789")
        print(f"mcs={mcs}: delivered={len(got)} ok_frames={rx.n_ok}/20 crc_fail={rx.n_crc_fail} "
              f"bad_hdr={rx.n_bad_hdr} est_snr_ema={rx._snr_ema:.1f} dB (true 25)")
        print("   ", [l for l in log.splitlines() if l.startswith("[RX] msg")])

    # 2. Frame success vs Es/N0 (50-byte payload)
    print("\nEs/N0  BPSK-ok  QPSK-ok  est(BPSK) est(QPSK)")
    for esn0 in (4, 6, 8, 10, 12, 14, 16, 18):
        rb, _, _ = run_frames(esn0, 0, n_reps=40)
        rq, _, _ = run_frames(esn0, 1, n_reps=40)
        eb = f"{rb._snr_ema:.1f}" if rb._snr_ema is not None else "-"
        eq = f"{rq._snr_ema:.1f}" if rq._snr_ema is not None else "-"
        print(f"{esn0:5d}  {rb.n_ok:3d}/40   {rq.n_ok:3d}/40   {eb:>8} {eq:>8}")

    # 3. Pure noise: no false frames
    rx = rxm.PacketDeframerRX(peer_id=0)
    got = []
    rx.connect_out("pdu_out", lambda m: got.append(1))
    feed(rx, Channel(10).idle(3_000_000))
    print(f"\nnoise only (3M syms): delivered={len(got)} bad_hdr={rx.n_bad_hdr} crc_fail={rx.n_crc_fail} "
          f"buf={len(rx._sym)}")

def link_demo():
    
    
    class Node:
        def __init__(self, name, my_id, peer):
            self.name = name
            self.tx = txm.PacketFramerTX(peer_id=peer, preamble_len=64, repeat_count=10)
            self.rx = rxm.PacketDeframerRX(peer_id=my_id)
            self.rx.connect_out("link_out", self.tx.handle_link)     # the new msg connection
            self.out = []
            self.tx.connect_out("pdu_out", lambda m: self.out.append(np.array(pmt.cdr(m).val, np.complex64)))
            self.got = []
            self.rx.connect_out("text_out", lambda m: self.got.append(m.val))
    
    
    A, B = Node("A", 0, 1), Node("B", 1, 0)
    ch = {"AB": Channel(20), "BA": Channel(20)}
    
    
    def send(src, dst, chan, text):
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            src.tx.post("msg_in", pmt.intern(text))
            bursts, src.out[:] = list(src.out), []
            x = [chan.idle(1000)]
            for b in bursts:
                x += [chan.run(b), chan.idle(300)]
            x.append(chan.idle(4000))
            feed(dst.rx, np.concatenate(x))
            dst.rx._flush_summary()
        for l in log.getvalue().splitlines():
            if l.startswith(("[TX]", "[RX] msg", "[LINK]")):
                print(f"  {l}")
    
    
    steps = [
        ("A", 20, 20, "hello from A"),
        ("B", 20, 20, "reply from B"),
        ("A", 20, 20, "A again (should be QPSK now)"),
        ("A", 11, 20, "A->B channel degraded to 11 dB"),
        ("B", 11, 20, "B tells A to fall back"),
        ("A", 11, 20, "A again (should be BPSK)"),
    ]
    for who, ab, ba, text in steps:
        ch["AB"].esn0_db, ch["BA"].esn0_db = ab, ba
        print(f"--- {who} sends, A->B {ab} dB, B->A {ba} dB")
        if who == "A":
            send(A, B, ch["AB"], text)
        else:
            send(B, A, ch["BA"], text)
    
    print("\nB received:", B.got)
    print("A received:", A.got)
    
    # forced mode attribute (what a GRC chooser would set)
    A.tx.mcs_mode = 0
    ch["AB"].esn0_db = 20
    print("--- forced BPSK via mcs_mode=0")
    send(A, B, ch["AB"], "forced")

if __name__ == "__main__":
    table()
    print("\n=== two-node feedback demo ===")
    link_demo()
