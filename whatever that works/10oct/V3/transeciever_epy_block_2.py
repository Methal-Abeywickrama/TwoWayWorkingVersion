import struct, pmt
from gnuradio import gr

HDR_FMT    = ">BBBBBBHHQ"
PKT_CODE   = {"SYN":0x01, "SYN_ACK":0x02, "DATA":0x03,
              "ACK":0x04, "FIN":0x05, "FIN_ACK":0x06}
MEDIA_CODE = {"text":0x01, "image":0x02, "audio":0x03}


class blk(gr.sync_block):
    """Block 3: TX Header Serializer -- stateless 18-byte binary header serializer."""

    def __init__(self):
        gr.sync_block.__init__(self, name='TX Header Serializer',
                               in_sig=None, out_sig=None)
        self.message_port_register_in(pmt.intern("pdus"))
        self.message_port_register_out(pmt.intern("pdus"))
        self.set_msg_handler(pmt.intern("pdus"), self.handle)

    def handle(self, msg):
        if not pmt.is_pair(msg): return
        meta = pmt.car(msg); data = pmt.cdr(msg)
        if not pmt.is_dict(meta): return
        def gu(k, d=0):
            return pmt.to_uint64(pmt.dict_ref(meta, pmt.intern(k), pmt.from_uint64(d)))
        def gs(k, d=""):
            return pmt.symbol_to_string(pmt.dict_ref(meta, pmt.intern(k), pmt.intern(d)))
        hdr = struct.pack(HDR_FMT,
            PKT_CODE.get(gs("pkt_type"), 0),
            MEDIA_CODE.get(gs("media_type"), 0),
            gu("src_addr") & 0xFF, gu("src_port") & 0xFF,
            gu("dst_addr") & 0xFF, gu("dst_port") & 0xFF,
            gu("seq_no")    & 0xFFFF,
            gu("total_pkts") & 0xFFFF,
            gu("session_id"))
        payload  = bytes(pmt.u8vector_elements(data)) if pmt.is_u8vector(data) else b""
        frame    = hdr + payload
        out_data = pmt.init_u8vector(len(frame), list(frame))
        self.message_port_pub(pmt.intern("pdus"), pmt.cons(meta, out_data))

    def work(self, input_items, output_items):
        return 0
