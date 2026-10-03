import numpy as np
from gnuradio import gr
import pmt

class TextToPDU(gr.basic_block):
    def __init__(self):
        gr.basic_block.__init__(
            self,
            name="Text to PDU",
            in_sig=[],
            out_sig=[]
        )
        self.message_port_register_in(pmt.intern("msg_in"))
        self.message_port_register_out(pmt.intern("pdu_out"))
        self.set_msg_handler(pmt.intern("msg_in"), self.handle_msg)

    def handle_msg(self, msg):
        # Extract string from PMT message
        if pmt.is_symbol(msg):
            text = pmt.symbol_to_string(msg)
        elif pmt.is_string(msg):
            text = pmt.string_to_load(msg)
        else:
            text = str(pmt.to_python(msg))

        raw_bytes = [ord(c) for c in text]
        meta = pmt.make_dict()
        vec = pmt.init_u8vector(len(raw_bytes), raw_bytes)
        
        # Publish PDU pair: (meta_dict, u8_data_vector)
        self.message_port_pub(pmt.intern("pdu_out"), pmt.cons(meta, vec))
