"""
Modular Exponentiation Decryption Block (PDU-based)
    plain_byte = (cipher_byte ** private_key) % prime   (prime must be 257)

Placed between the SR-ARQ FSM's app_out and the ZMQ app sink.
- The first skip_bytes (8) bytes (app frame header) were never encrypted and are passed as-is.
- FSM event messages (tx_done / tx_failed / tx_busy: metadata with an "event" key) are
  forwarded untouched.
- private_key must be the inverse of the sender's key mod 256 (key 17 <-> 241, 3 <-> 171).
"""
import numpy as np
from gnuradio import gr
import pmt


class blk(gr.basic_block):
    def __init__(self, prime=257, private_key=241, skip_bytes=8):
        gr.basic_block.__init__(self, name='Decrypt Payload', in_sig=None, out_sig=None)
        self.prime = prime
        self.private_key = private_key
        self.skip_bytes = skip_bytes
        self._lut_for = None
        self._lut = None
        self.message_port_register_in(pmt.intern("pdus_in"))
        self.message_port_register_out(pmt.intern("pdus_out"))
        self.set_msg_handler(pmt.intern("pdus_in"), self.handle_pdu)

    def _table(self):
        cfg = (int(self.private_key), int(self.prime))
        if cfg != self._lut_for:
            d, prime = cfg
            if prime != 257:
                print(f"[DEC] prime must be 257 for byte data (got {prime})", flush=True)
            self._lut = np.array([pow(x, d, prime) & 0xFF for x in range(256)], dtype=np.uint8)
            self._lut_for = cfg
        return self._lut

    def handle_pdu(self, pdu):
        if not pmt.is_pair(pdu):
            return
        meta, payload = pmt.car(pdu), pmt.cdr(pdu)
        if (pmt.is_dict(meta) and pmt.dict_has_key(meta, pmt.intern("event"))) \
                or not pmt.is_u8vector(payload):
            self.message_port_pub(pmt.intern("pdus_out"), pdu)      # FSM event: pass through
            return
        data = np.array(pmt.u8vector_elements(payload), dtype=np.uint8)
        skip = max(0, int(self.skip_bytes))
        data[skip:] = self._table()[data[skip:]]
        out = pmt.init_u8vector(len(data), data.tolist())
        self.message_port_pub(pmt.intern("pdus_out"), pmt.cons(meta, out))
