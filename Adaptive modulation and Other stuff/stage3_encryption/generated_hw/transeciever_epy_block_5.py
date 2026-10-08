"""
PDU Console Printer
Prints each decrypted application frame from the FSM (skips FSM event messages).
Long payloads are shortened to max_chars so a received image does not flood the console.
"""
from gnuradio import gr
import pmt


class blk(gr.basic_block):
    def __init__(self, max_chars=120):
        gr.basic_block.__init__(self, name="PDU Payload Printer", in_sig=None, out_sig=None)
        self.max_chars = max_chars
        self.message_port_register_in(pmt.intern("pdu_in"))
        self.set_msg_handler(pmt.intern("pdu_in"), self.handle_pdu)

    def handle_pdu(self, pdu):
        if not pmt.is_pair(pdu):
            return
        meta, payload_pmt = pmt.car(pdu), pmt.cdr(pdu)
        if pmt.is_dict(meta) and pmt.dict_has_key(meta, pmt.intern("event")):
            ev = pmt.symbol_to_string(pmt.dict_ref(meta, pmt.intern("event"), pmt.intern("?")))
            print(f"[APP] FSM event: {ev}", flush=True)
            return
        if not pmt.is_u8vector(payload_pmt):
            return
        data = bytes(pmt.u8vector_elements(payload_pmt))
        body = data[8:]                       # after the 8-byte app frame header
        name = ""
        if len(body) >= 2:
            nlen = int.from_bytes(body[:2], "big")
            name = body[2:2 + nlen].decode("utf-8", errors="replace")
            body = body[2 + nlen:]
        try:
            shown = repr(body.decode("utf-8"))
        except UnicodeDecodeError:
            shown = f"<binary {body[:24].hex()}...>"
        n = int(self.max_chars)
        if len(shown) > n:
            shown = shown[:n] + "..."
        src = ""
        if pmt.is_dict(meta) and pmt.dict_has_key(meta, pmt.intern("src_addr")):
            src = f" from addr {pmt.to_python(pmt.dict_ref(meta, pmt.intern('src_addr'), pmt.PMT_NIL))}"
        print("\n" + "=" * 50, flush=True)
        print(f"[DECRYPTED PAYLOAD RECEIVED]{src}: '{name}' ({len(data)} bytes)", flush=True)
        print(f"  Content: {shown}", flush=True)
        print("=" * 50 + "\n", flush=True)
