import os
import site
import sys
import pmt
import numpy as np
from gnuradio import gr

# Load user site packages dynamically
possible_paths = site.getusersitepackages()
if isinstance(possible_paths, str):
    possible_paths = [possible_paths]
for path in possible_paths + site.getsitepackages():
    if os.path.exists(path) and path not in sys.path:
        sys.path.insert(0, path)

import zstandard as zstd

class pdu_zstd_compressor(gr.basic_block):
    """
    Compresses incoming PDU payloads using Zstandard and dictionary.
    """
    def __init__(self, dict_path="chat_dictionary.zdict"):
        gr.basic_block.__init__(
            self,
            name="PDU Zstd Compressor",
            in_sig=None,
            out_sig=None
        )
        self.message_port_register_in(pmt.intern("pdu_in"))
        self.message_port_register_out(pmt.intern("pdu_out"))
        self.set_msg_handler(pmt.intern("pdu_in"), self.handle_pdu)

        base_dir = os.getcwd()
        resolved_dict_path = os.path.join(base_dir, dict_path)

        if os.path.exists(resolved_dict_path):
            with open(resolved_dict_path, "rb") as f:
                dictionary = zstd.ZstdCompressionDict(f.read())
            self.compressor = zstd.ZstdCompressor(dict_data=dictionary)
        else:
            self.compressor = zstd.ZstdCompressor()

    def handle_pdu(self, pdu):
        meta = pmt.car(pdu)
        data_vector = pmt.cdr(pdu)
        
        raw_bytes = bytes(pmt.u8vector_elements(data_vector))
        compressed_bytes = self.compressor.compress(raw_bytes)
        
        out_data = pmt.init_u8vector(len(compressed_bytes), list(compressed_bytes))
        out_pdu = pmt.cons(meta, out_data)
        
        self.message_port_pub(pmt.intern("pdu_out"), out_pdu)
