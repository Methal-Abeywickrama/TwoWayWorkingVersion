"""
Embedded Python Block: Burst Packet Framer TX (with equalizer training word)
- Outputs packed byte PDUs directly to pdu_pdu_to_tagged_stream
- Zero idle transmission between bursts
- Frame layout: [preamble] [train word: F8 DD 42 58] [1A CF FC 1D] [len: 2B][dest_id: 1B][msg_id: 1B][rep: 1B][n_reps: 1B] [payload] [CRC32: 4B] [postamble]
- The train word is known to the RX: the correlator (digital_corr_est_cc) finds it, tags it,
  and the linear equalizer trains on it. The deframer does not look at it (it only looks for the sync word).
- Postamble dynamically padded so total size is a multiple of buffer_size
- Attaches tx_sob (Start of Burst) and tx_eob (End of Burst) metadata tags
"""
import zlib
import pmt
from gnuradio import gr

SYNC_WORD = bytes([0x1A, 0xCF, 0xFC, 0x1D])
# 31-bit m-sequence + 1 pad bit. MUST match TRAIN_BITS in the flowgraph (RX side).
TRAIN_WORD = bytes([0xF8, 0xDD, 0x42, 0x58])


class PacketFramerTX(gr.basic_block):
    def __init__(
        self,
        peer_id=1,
        preamble_len=64,
        repeat_count=5,
        max_payload_len=1024,
        preamble_byte=0xFF,
        buffer_size=512,
        postamble_byte=0x00,
        train_enable=1,
    ):
        gr.basic_block.__init__(
            self,
            name="Packet Framer TX (Burst)",
            in_sig=None,
            out_sig=None,
        )
        self.peer_id = int(peer_id) & 0xFF
        self.preamble_len = int(preamble_len)
        self.repeat_count = int(repeat_count)
        self.max_payload_len = int(max_payload_len)
        self.preamble_byte = int(preamble_byte) & 0xFF
        self.buffer_size = int(buffer_size)
        self.postamble_byte = int(postamble_byte) & 0xFF
        self.train_enable = int(train_enable)
        self._msg_id = 0

        self.message_port_register_in(pmt.intern("msg_in"))
        self.set_msg_handler(pmt.intern("msg_in"), self.handle_msg)
        self.message_port_register_out(pmt.intern("pdu_out"))

    @staticmethod
    def _extract_payload(msg):
        if pmt.is_pair(msg):
            data = pmt.cdr(msg)
            if pmt.is_u8vector(data):
                return bytes(pmt.u8vector_elements(data))
            return str(pmt.to_python(data)).encode("utf-8")
        if pmt.is_symbol(msg):
            return pmt.symbol_to_string(msg).encode("utf-8")
        val = pmt.to_python(msg)
        if isinstance(val, (bytes, bytearray)):
            return bytes(val)
        return str(val).encode("utf-8")

    def handle_msg(self, msg):
        try:
            payload = self._extract_payload(msg)
        except Exception as e:
            print(f"[TX] Could not parse message: {e}", flush=True)
            return

        if not payload:
            return

        if len(payload) > self.max_payload_len:
            print(f"[TX] Payload {len(payload)} B truncated to {self.max_payload_len} B", flush=True)
            payload = payload[: self.max_payload_len]

        msg_id = self._msg_id
        self._msg_id = (self._msg_id + 1) & 0xFF
        n_reps = max(1, min(255, self.repeat_count))

        preamble = bytes([self.preamble_byte]) * self.preamble_len
        train = TRAIN_WORD if self.train_enable else b""
        print(
            f"[TX] msg #{msg_id} (to peer {self.peer_id}): '{payload.decode('utf-8', 'replace')}' ({n_reps} bursts queued)",
            flush=True,
        )

        for rep in range(n_reps):
            header = len(payload).to_bytes(2, "big") + bytes([self.peer_id, msg_id, rep, n_reps])
            crc = zlib.crc32(header + payload).to_bytes(4, "big")

            # Core frame structure before buffer alignment
            raw_frame = preamble + train + SYNC_WORD + header + payload + crc

            # Calculate postamble padding required to match buffer_size multiple
            if self.buffer_size > 0:
                rem = len(raw_frame) % self.buffer_size
                pad_len = (self.buffer_size - rem) % self.buffer_size
            else:
                pad_len = 0

            postamble = bytes([self.postamble_byte]) * pad_len
            frame = raw_frame + postamble

            # Build metadata dictionary with tx_sob and tx_eob tags
            meta = pmt.make_dict()
            meta = pmt.dict_add(meta, pmt.intern("tx_sob"), pmt.PMT_T)
            meta = pmt.dict_add(meta, pmt.intern("tx_eob"), pmt.PMT_T)

            # Explicit offset tags for downstream PDU-to-Tagged-Stream conversion
            sob_tag = pmt.make_tuple(pmt.from_long(0), pmt.intern("tx_sob"), pmt.PMT_T)
            eob_tag = pmt.make_tuple(pmt.from_long(len(frame) - 1), pmt.intern("tx_eob"), pmt.PMT_T)
            extra_tags = pmt.list2(sob_tag, eob_tag)
            meta = pmt.dict_add(meta, pmt.intern("extra_tags"), extra_tags)

            vec = pmt.init_u8vector(len(frame), list(frame))
            self.message_port_pub(pmt.intern("pdu_out"), pmt.cons(meta, vec))
