import numpy as np
from gnuradio import gr
import pmt

class blk(gr.basic_block):
    """
    PDU Console Printer
    Extracts binary payload from a PDU and prints it cleanly to stdout.
    """

    def __init__(self):
        gr.basic_block.__init__(
            self,
            name="PDU Payload Printer",
            in_sig=None,
            out_sig=None,
        )
        
        # Register PDU input port
        self.message_port_register_in(pmt.intern("pdu_in"))
        self.set_msg_handler(pmt.intern("pdu_in"), self.handle_pdu)

    def handle_pdu(self, pdu):
        # PDUs are PMT pairs: (metadata_dict . payload_u8vector)
        if not pmt.is_pair(pdu):
            print("[Printer Error] Received PMT is not a valid PDU Pair", flush=True)
            return

        meta = pmt.car(pdu)
        payload_pmt = pmt.cdr(pdu)

        # Ensure CDR is a uniform u8vector
        if not pmt.is_u8vector(payload_pmt):
            print("[Printer Error] PDU payload is not a u8vector", flush=True)
            return

        # Extract bytes from vector
        payload_bytes = bytes(pmt.u8vector_elements(payload_pmt))

        # Attempt to decode string; fallback to hex if binary
        try:
            text = payload_bytes.decode("utf-8")
            display_str = f"'{text}'"
        except UnicodeDecodeError:
            display_str = f"<Binary Data: {payload_bytes.hex()}>"

        # Print cleanly with formatting header
        print("\n" + "=" * 50, flush=True)
        print(f"[DECRYPTED PAYLOAD RECEIVED] ({len(payload_bytes)} bytes):", flush=True)
        print(f"  Payload: {display_str}", flush=True)
        
        # Print metadata if present
        if pmt.is_dict(meta):
            keys = pmt.dict_keys(meta)
            meta_str = []
            while not pmt.is_null(keys):
                k = pmt.car(keys)
                v = pmt.dict_ref(meta, k, pmt.PMT_NIL)
                meta_str.append(f"{pmt.symbol_to_string(k)}: {pmt.to_python(v)}")
                keys = pmt.cdr(keys)
            if meta_str:
                print(f"  Metadata: {', '.join(meta_str)}", flush=True)
                
        print("=" * 50 + "\n", flush=True)