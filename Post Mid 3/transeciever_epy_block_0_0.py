"""
Embedded Python Block: Burst Packet Framer TX
- Outputs packed byte PDUs directly to pdu_pdu_to_tagged_stream
- Zero idle transmission (idle between bursts = complete silence)
- Zero postamble / trailing idle
- Frame layout: [preamble] [1A CF FC 1D] [len: 2B][dest_id: 1B][msg_id: 1B][rep: 1B][n_reps: 1B] [payload] [CRC32: 4B]
- CRC32 covers header + payload
"""
import zlib
import pmt
from gnuradio import gr

SYNC_WORD = bytes([0x1A, 0xCF, 0xFC, 0x1D])


class PacketFramerTX(gr.basic_block):
    def __init__(self, peer_id=1, preamble_len=64, repeat_count=5, max_payload_len=1024, preamble_byte=0xFF):
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
            payload = payload[:self.max_payload_len]

        msg_id = self._msg_id
        self._msg_id = (self._msg_id + 1) & 0xFF
        n_reps = max(1, min(255, self.repeat_count))

        preamble = bytes([self.preamble_byte]) * self.preamble_len
        print(f"[TX] msg #{msg_id} (to peer {self.peer_id}): '{payload.decode('utf-8', 'replace')}' ({n_reps} bursts queued)", flush=True)

        for rep in range(n_reps):
            header = len(payload).to_bytes(2, "big") + bytes([self.peer_id, msg_id, rep, n_reps])
            crc = zlib.crc32(header + payload).to_bytes(4, "big")
            frame = preamble + SYNC_WORD + header + payload + crc

            vec = pmt.init_u8vector(len(frame), list(frame))
            self.message_port_pub(pmt.intern("pdu_out"), pmt.cons(pmt.PMT_NIL, vec))