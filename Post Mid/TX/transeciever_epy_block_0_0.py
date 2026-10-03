"""
Embedded Python Block: PacketFramerTX with Repetition
"""
import numpy as np
from gnuradio import gr
import pmt
import zlib

class PacketFramerTX(gr.basic_block):
    def __init__(self, preamble_len=32, postamble_len=16, repeat_count=1):
        gr.basic_block.__init__(
            self,
            name="Packet Framer TX",
            in_sig=None,
            out_sig=None
        )
        self.preamble_len = preamble_len
        self.postamble_len = postamble_len
        self.repeat_count = int(repeat_count)
        self.sync_word = bytes([0x1A, 0xCF, 0xFC, 0x1D])

        self.message_port_register_in(pmt.intern("msg_in"))
        self.set_msg_handler(pmt.intern("msg_in"), self.handle_msg)
        self.message_port_register_out(pmt.intern("pdu_out"))

    def set_repeat_count(self, repeat_count):
        self.repeat_count = int(repeat_count)

    def handle_msg(self, msg):
        try:
            py_val = pmt.to_python(msg)
            if isinstance(py_val, tuple) and len(py_val) == 2:
                payload = bytes(py_val[1])
                text = payload.decode('utf-8', errors='replace')
            elif isinstance(py_val, str):
                text = py_val
                payload = text.encode('utf-8')
            elif isinstance(py_val, (bytes, bytearray)):
                payload = bytes(py_val)
                text = payload.decode('utf-8', errors='replace')
            else:
                text = str(py_val)
                payload = text.encode('utf-8')
        except Exception as e:
            print(f"[TX Error] Failed to parse input message: {e}", flush=True)
            return

        if not payload:
            return

        # Frame construction
        preamble = bytes([0xAA] * self.preamble_len)
        length_hdr = len(payload).to_bytes(2, byteorder='big')
        crc = zlib.crc32(payload).to_bytes(4, byteorder='big')
        postamble = bytes([0xAA] * self.postamble_len)

        frame = preamble + self.sync_word + length_hdr + payload + crc + postamble

        # Create PDU
        vec = pmt.init_u8vector(len(frame), list(frame))
        pdu = pmt.cons(pmt.PMT_NIL, vec)

        # Publish N times
        print(f"[TX] Broadcasting '{text}' ({self.repeat_count} times)...", flush=True)
        for _ in range(self.repeat_count):
            self.message_port_pub(pmt.intern("pdu_out"), pdu)
