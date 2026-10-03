import os
import site
import sys
import json
import time
import numpy as np
import pmt
from gnuradio import gr

class pdu_chat_generator(gr.basic_block):
    """
    Generates JSON chat message PDUs. Accepts custom strings from QT Message Edit Box.
    """
    def __init__(self, sender_id="A", recipient_id="B"):
        gr.basic_block.__init__(
            self,
            name="PDU Chat Generator",
            in_sig=None,
            out_sig=None
        )
        self.message_port_register_out(pmt.intern("pdu_out"))
        self.message_port_register_in(pmt.intern("generate"))
        self.set_msg_handler(pmt.intern("generate"), self.handle_generate)

        self.sender_id = sender_id
        self.recipient_id = recipient_id
        self.msg_counter = 0

    def handle_generate(self, msg):
        text_content = "Default Message"

        # Check if incoming message is a PMT Pair (from QT Message Edit Box)
        if pmt.is_pair(msg):
            # Extract value from key-value pair ('mes' . 'MESSAGE')
            val = pmt.cdr(msg)
            if pmt.is_symbol(val):
                text_content = pmt.symbol_to_string(val)
            elif pmt.is_string(val):
                text_content = pmt.symbol_to_string(val)
        # Check if incoming message is a plain string/symbol
        elif pmt.is_symbol(msg):
            text_content = pmt.symbol_to_string(msg)
        elif pmt.is_string(msg):
            text_content = pmt.symbol_to_string(msg)

        # Build JSON using your custom typed string
        self.msg_counter += 1
        payload = {
            "sender": self.sender_id,
            "recipient": self.recipient_id,
            "msg_id": self.msg_counter,
            "timestamp": int(time.time()),
            "type": "text",
            "content": text_content
        }

        # Encode to compact JSON byte string
        json_bytes = json.dumps(payload, separators=(',', ':')).encode('utf-8')

        # Create PDU: metadata dict + byte array vector
        meta = pmt.make_dict()
        data = pmt.init_u8vector(len(json_bytes), list(json_bytes))
        pdu = pmt.cons(meta, data)

        # Publish compressed pipeline input
        self.message_port_pub(pmt.intern("pdu_out"), pdu)
