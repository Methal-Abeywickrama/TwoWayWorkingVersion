"""
Embedded Python Block: Lossy PDU Channel (testing only)

Stands in for the radio in game_pdu_bridge.grc: drops PDUs with probability drop_prob,
flips one random bit with probability corrupt_prob, and can add a fixed delay.
Lets you see how the game behaves with a bad link before using the ANTSDRs.
The payload has no CRC, so keep corrupt_prob at 0 unless you want to see what
unchecked bit errors do (your real link layer is expected to drop bad frames).
"""
import random
import threading

import pmt
from gnuradio import gr


class blk(gr.basic_block):
    """Lossy PDU Channel - drop / corrupt / delay PDUs to mimic a radio link."""

    def __init__(self, drop_prob=0.1, corrupt_prob=0.0, delay_ms=0.0, seed=0):
        gr.basic_block.__init__(self, name="Lossy PDU Channel", in_sig=None, out_sig=None)
        self.drop_prob = drop_prob
        self.corrupt_prob = corrupt_prob
        self.delay_ms = delay_ms
        self.rng = random.Random(seed if seed else None)
        self.message_port_register_in(pmt.intern("in"))
        self.message_port_register_out(pmt.intern("out"))
        self.set_msg_handler(pmt.intern("in"), self.handle)

    def handle(self, msg):
        if self.rng.random() < self.drop_prob:
            return
        if self.rng.random() < self.corrupt_prob and pmt.is_pair(msg):
            data = list(pmt.u8vector_elements(pmt.cdr(msg)))
            if data:
                i = self.rng.randrange(len(data))
                data[i] ^= 1 << self.rng.randrange(8)
                msg = pmt.cons(pmt.car(msg), pmt.init_u8vector(len(data), data))
        if self.delay_ms > 0:
            threading.Timer(self.delay_ms / 1000.0, self.message_port_pub,
                            (pmt.intern("out"), msg)).start()
        else:
            self.message_port_pub(pmt.intern("out"), msg)
