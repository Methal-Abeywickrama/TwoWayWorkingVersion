"""
Modular Exponentiation Encryption Block (PDU-based)
    cipher_byte = (plain_byte ** key) % prime          (prime must be 257)

Placed between the ZMQ app source and the SR-ARQ FSM's app_in (application-layer,
end-to-end encryption).
- The first skip_bytes (8) bytes stay in clear: they are the app frame header
  [dst_addr][dst_port][media][reserved][length:4] that the FSM needs to route and size the
  transfer. Everything after it (file name + file data) is encrypted.
- key must be odd (gcd(key, 256) == 1). Then x -> x^key mod 257 is a permutation of 0..255
  and the peer can invert it with private_key = key^-1 mod 256 (17 <-> 241, 3 <-> 171).
  key = 0 (the old PUBLIC_KEY_PEER default) maps every byte to 1 and destroys the data.
- A 256-entry lookup table replaces the per-byte np.vectorize call.
NOTE: this is a byte-substitution cipher for demonstration. Anyone who knows key and prime
can compute the inverse, and byte frequencies are preserved, so it is not secure.
"""
import numpy as np
from gnuradio import gr
import pmt


class blk(gr.basic_block):
    def __init__(self, prime=257, key=17, skip_bytes=8):
        gr.basic_block.__init__(self, name='Encrypt Payload', in_sig=None, out_sig=None)
        self.prime = prime
        self.key = key
        self.skip_bytes = skip_bytes
        self._lut_for = None
        self._lut = None
        self.message_port_register_in(pmt.intern("pdus_in"))
        self.message_port_register_out(pmt.intern("pdus_out"))
        self.set_msg_handler(pmt.intern("pdus_in"), self.handle_pdu)

    def _table(self):
        cfg = (int(self.key), int(self.prime))
        if cfg != self._lut_for:
            key, prime = cfg
            if prime != 257:
                print(f"[ENC] prime must be 257 for byte data (got {prime})", flush=True)
            if key % 2 == 0:
                print(f"[ENC] key {key} is even: it has no inverse mod 256, the receiver "
                      "cannot decrypt. Use an odd key (e.g. 17 or 3).", flush=True)
            self._lut = np.array([pow(x, key, prime) & 0xFF for x in range(256)], dtype=np.uint8)
            self._lut_for = cfg
        return self._lut

    def handle_pdu(self, pdu):
        if not pmt.is_pair(pdu) or not pmt.is_u8vector(pmt.cdr(pdu)):
            return
        meta = pmt.car(pdu)
        data = np.array(pmt.u8vector_elements(pmt.cdr(pdu)), dtype=np.uint8)
        skip = max(0, int(self.skip_bytes))
        data[skip:] = self._table()[data[skip:]]
        out = pmt.init_u8vector(len(data), data.tolist())
        self.message_port_pub(pmt.intern("pdus_out"), pmt.cons(meta, out))
