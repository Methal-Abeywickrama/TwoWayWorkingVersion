import struct, pmt
from gnuradio import gr

HDR_FMT  = ">BBBBBBHHQ"
HDR_SIZE = struct.calcsize(HDR_FMT)  # 18

PKT_STR   = {0x01:"SYN", 0x02:"SYN_ACK", 0x03:"DATA",
             0x04:"ACK", 0x05:"FIN", 0x06:"FIN_ACK"}
MEDIA_STR = {0x01:"text", 0x02:"image", 0x03:"audio"}


class blk(gr.sync_block):
    """Block 1: RX Header Parser -- stateless 18-byte binary header deserializer."""

    def __init__(self):
        gr.sync_block.__init__(self, name='RX Header Parser',
                               in_sig=None, out_sig=None)
        self.message_port_register_in(pmt.intern("pdus"))
        self.message_port_register_out(pmt.intern("pdus"))
        self.set_msg_handler(pmt.intern("pdus"), self.handle)

    def handle(self, msg):
        if not pmt.is_pair(msg): return
        in_meta = pmt.car(msg)
        in_data = pmt.cdr(msg)
        if not pmt.is_u8vector(in_data): return
        raw = bytes(pmt.u8vector_elements(in_data))
        if len(raw) < HDR_SIZE: return
        fields   = struct.unpack_from(HDR_FMT, raw)
        pkt_type = fields[0]
        if not (0x01 <= pkt_type <= 0x06): return
        meta = in_meta if pmt.is_dict(in_meta) else pmt.make_dict()
        for k, v in [
            ("pkt_type",     pmt.intern(PKT_STR.get(pkt_type, "UNKNOWN"))),
            ("pkt_type_raw", pmt.from_uint64(pkt_type)),
            ("media_type",   pmt.intern(MEDIA_STR.get(fields[1], "unknown"))),
            ("src_addr",     pmt.from_uint64(fields[2])),
            ("src_port",     pmt.from_uint64(fields[3])),
            ("dst_addr",     pmt.from_uint64(fields[4])),
            ("dst_port",     pmt.from_uint64(fields[5])),
            ("seq_no",       pmt.from_uint64(fields[6])),
            ("total_pkts",   pmt.from_uint64(fields[7])),
            ("session_id",   pmt.from_uint64(fields[8])),
        ]:
            meta = pmt.dict_add(meta, pmt.intern(k), v)
        payload  = raw[HDR_SIZE:]
        out_data = pmt.init_u8vector(len(payload), list(payload))
        self.message_port_pub(pmt.intern("pdus"), pmt.cons(meta, out_data))

    def work(self, input_items, output_items):
        return 0
