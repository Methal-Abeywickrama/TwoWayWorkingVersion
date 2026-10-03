import os
import site
import sys
import pmt
import numpy as np
from gnuradio import gr

# --- DYNAMIC PORTABLE PATH FIX ---
possible_paths = site.getusersitepackages()
if isinstance(possible_paths, str):
    possible_paths = [possible_paths]

for path in possible_paths + site.getsitepackages():
    if os.path.exists(path) and path not in sys.path:
        sys.path.insert(0, path)

import zstandard as zstd


class pdu_zstd_decompressor(gr.basic_block):
    """
    Decompresses incoming Zstandard PDU payloads using a dictionary.
    Inputs and outputs asynchronously via PDU message ports.
    """

    def __init__(self, dict_path="chat_dictionary.zdict"):
        gr.basic_block.__init__(
            self,
            name="PDU Zstd Decompressor",
            in_sig=None,
            out_sig=None,
        )

        # Register PDU message ports
        self.message_port_register_in(pmt.intern("pdu_in"))
        self.message_port_register_out(pmt.intern("pdu_out"))
        self.set_msg_handler(pmt.intern("pdu_in"), self.handle_pdu)

        # Resolve dictionary relative to execution directory
        base_dir = os.getcwd()
        resolved_dict_path = os.path.join(base_dir, dict_path)

        if os.path.exists(resolved_dict_path):
            with open(resolved_dict_path, "rb") as f:
                dictionary = zstd.ZstdCompressionDict(f.read())
            self.decompressor = zstd.ZstdDecompressor(dict_data=dictionary)
        elif os.path.exists(dict_path):
            with open(dict_path, "rb") as f:
                dictionary = zstd.ZstdCompressionDict(f.read())
            self.decompressor = zstd.ZstdDecompressor(dict_data=dictionary)
        else:
            # Fallback if no dictionary is found
            self.decompressor = zstd.ZstdDecompressor()

    def handle_pdu(self, pdu):
        # Extract metadata dictionary and uniform 8-bit vector payload
        meta = pmt.car(pdu)
        data_vector = pmt.cdr(pdu)

        try:
            # Extract raw compressed bytes
            compressed_bytes = bytes(pmt.u8vector_elements(data_vector))

            # Decompress using zstd
            decompressed_bytes = self.decompressor.decompress(compressed_bytes)

            # Re-wrap decompressed bytes into PMT byte vector PDU
            out_data = pmt.init_u8vector(
                len(decompressed_bytes), list(decompressed_bytes)
            )
            out_pdu = pmt.cons(meta, out_data)

            # Publish decompressed message packet
            self.message_port_pub(pmt.intern("pdu_out"), out_pdu)

        except Exception as e:
            # Safely log corrupted or un-decompressable packets over noisy radio channels
            sys.stderr.write(f"[PDU Decompressor Error]: {e}\n")
