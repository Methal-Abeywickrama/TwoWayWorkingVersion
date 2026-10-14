"""
Embedded Python Block: Burst Packet Framer TX (Hamming FEC + equalizer training word)
- Outputs packed byte PDUs directly to pdu_pdu_to_tagged_stream
- Frame layout:
    [preamble] [train word: F8 DD 42 58] [sync: 1A CF FC 1D] [coded section(s)] [postamble]
  Before coding the sections are:  header = [len:2B][dest_id:1B][msg_id:1B][rep:1B][n_reps:1B]
                                   body   = [payload][CRC32 over header+payload: 4B]
- fec = 0  : header + body sent uncoded (identical to the non-FEC framer)
  fec = 15 : Hamming(15,11)    fec = 31 : Hamming(31,26)
  Preamble, train word and sync word are never coded (the correlator / deframer look for them as raw bits).
  Header and body are two separate coded sections (the receiver decodes the header first to learn the length);
  the coded bits are bit-for-bit what the previous FEC framer produced, so a deframer written for that
  format still decodes them. Anything after the body is ignored by the receiver.
- interleave = True: each section's codewords are sent column by column (burst-error protection)
- The train word is known to the RX: the correlator finds it, tags it, and the equalizer trains on it.
  It must match TRAIN_BITS in the flowgraph.
- Postamble: pads the frame to a whole number of Pluto sink buffers (buffer_size is in SAMPLES, so one
  buffer = buffer_size / samples_per_byte bytes) and is never shorter than one buffer. The TX pulse-shaping
  filter holds back ~86 samples and the sink holds back the last partial buffer; both fall inside the
  postamble, so no frame bits are lost or delayed.
"""
import zlib
import numpy as np
import pmt
from gnuradio import gr

SYNC_WORD = bytes([0x1A, 0xCF, 0xFC, 0x1D])
# 31-bit m-sequence + 1 pad bit. MUST match TRAIN_BITS in the flowgraph (RX side).
TRAIN_WORD = bytes([0xF8, 0xDD, 0x42, 0x58])


def hamming_tables(n):
    """Standard positional Hamming layout: parity bits at positions 1, 2, 4, 8, (16)."""
    m = {15: 4, 31: 5}[n]
    pos = np.arange(1, n + 1)
    is_parity = (pos & (pos - 1)) == 0
    data_idx = np.flatnonzero(~is_parity)          # 0-based indices of data positions
    par_idx = np.flatnonzero(is_parity)            # 0-based indices of 1, 2, 4, 8, (16)
    P = ((pos[data_idx, None] >> np.arange(m)) & 1).astype(np.int32)   # k x m
    return data_idx, par_idx, P


def hamming_encode_bits(bits, n, interleave):
    """bits: 0/1 array. Pads to a multiple of k, returns the coded bit array."""
    data_idx, par_idx, P = hamming_tables(n)
    k = len(data_idx)
    nblk = -(-len(bits) // k)
    d = np.zeros(nblk * k, dtype=np.int32)
    d[:len(bits)] = bits
    d = d.reshape(nblk, k)
    cw = np.zeros((nblk, n), dtype=np.uint8)
    cw[:, data_idx] = d
    cw[:, par_idx] = (d @ P) & 1
    return (cw.T if interleave else cw).reshape(-1)


class PacketFramerTX(gr.basic_block):
    def __init__(self, peer_id=1, preamble_len=64, repeat_count=1, max_payload_len=1024,
                 preamble_byte=0xFF, buffer_size=2048, postamble_byte=0xFF, train_enable=1,
                 fec=15, interleave=1, samples_per_byte=32):
        gr.basic_block.__init__(
            self,
            name="Packet Framer TX (Burst, FEC)",
            in_sig=None,
            out_sig=None,
        )
        self.peer_id = int(peer_id) & 0xFF
        self.preamble_len = int(preamble_len)
        self.repeat_count = int(repeat_count)
        self.max_payload_len = int(max_payload_len)
        self.preamble_byte = int(preamble_byte) & 0xFF
        self.buffer_size = int(buffer_size)              # Pluto sink buffer, in SAMPLES
        self.postamble_byte = int(postamble_byte) & 0xFF
        self.train_enable = int(train_enable)
        self.fec = int(fec)
        self.interleave = bool(int(interleave))
        self.samples_per_byte = max(1, int(samples_per_byte))   # 8 bits * samples/symbol (BPSK)
        if self.fec not in (0, 15, 31):
            raise ValueError("fec must be 0, 15 or 31")
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

    def _encode_section(self, data):
        bits = np.unpackbits(np.frombuffer(data, dtype=np.uint8))
        return hamming_encode_bits(bits, self.fec, self.interleave)

    def _coded_body(self, header, payload):
        """Everything after the sync word: header + payload + CRC, coded if fec != 0."""
        crc = zlib.crc32(header + payload).to_bytes(4, "big")
        if self.fec == 0:
            return header + payload + crc
        coded = np.concatenate((self._encode_section(header),
                                self._encode_section(payload + crc)))
        return np.packbits(coded).tobytes()               # pads the last byte with 0s

    def _postamble_len(self, raw_len):
        """Bytes of postamble: at least one sink buffer, and total a whole number of buffers."""
        if self.buffer_size <= 0:
            return 0
        unit = max(1, -(-self.buffer_size // self.samples_per_byte))    # bytes per sink buffer
        need = raw_len + unit                                           # leave one full buffer of postamble
        return (-(-need // unit)) * unit - raw_len

    def build_frame(self, header, payload):
        preamble = bytes([self.preamble_byte]) * self.preamble_len
        train = TRAIN_WORD if self.train_enable else b""
        raw = preamble + train + SYNC_WORD + self._coded_body(header, payload)
        return raw + bytes([self.postamble_byte]) * self._postamble_len(len(raw))

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

        fec_txt = "no FEC" if self.fec == 0 else f"Hamming({self.fec},{self.fec - (4 if self.fec == 15 else 5)})"
        print(
            f"[TX] msg #{msg_id} (to peer {self.peer_id}): '{payload.decode('utf-8', 'replace')}' "
            f"({n_reps} bursts queued, {fec_txt})",
            flush=True,
        )

        for rep in range(n_reps):
            header = len(payload).to_bytes(2, "big") + bytes([self.peer_id, msg_id, rep, n_reps])
            frame = self.build_frame(header, payload)

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