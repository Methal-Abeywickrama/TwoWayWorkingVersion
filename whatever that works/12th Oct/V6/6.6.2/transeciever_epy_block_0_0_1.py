"""
Embedded Python Block: Burst Packet Framer TX (Hamming FEC + train word) - v6.6, src / dest addressing
- Packets from the ARQ / UDP transport blocks (binary payload starting with C5) are sent without a console line;
  the transport blocks print their own summaries.
- Outputs packed byte PDUs (to the CSMA block / PDU to Tagged Stream)
- Frame layout:
    [preamble] [train word: F8 DD 42 58] [sync: 1A CF FC 1D] [coded header] [coded body] [postamble]
  header = [len:2B][dest_id:1B][src_id:1B][msg_id:1B][rep:1B][n_reps:1B]   (7 bytes; v6.4 had 6, no dest)
  body   = [payload][CRC32 over header+payload: 4B]
- src_id  = this node's ID (0..254), written into every frame.
  dest_id = default destination: 0..254 one node, 255 = every node. Can be changed while running (GUI box).
  Destination of each message, first match wins:
    1. PDU metadata key "dest_id" (or "dst"), integer, e.g. set by an upper layer
    2. text messages (QT edit box) starting with "@<id> " -> that node, "@all " or "@* " -> every node;
       the prefix is removed before sending. Binary PDUs are never parsed for "@".
    3. dest_id
- fec = 0 / 15 / 31 (Hamming), interleave = column-wise codeword order. Must match the deframer.
- Postamble pads the frame to whole Pluto sink buffers (never shorter than one buffer).
"""
import zlib
import numpy as np
import pmt
from gnuradio import gr

SYNC_WORD = bytes([0x1A, 0xCF, 0xFC, 0x1D])
# 31-bit m-sequence + 1 pad bit. MUST match TRAIN_BITS in the flowgraph (RX side).
TRAIN_WORD = bytes([0xF8, 0xDD, 0x42, 0x58])
BROADCAST = 0xFF


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


def id_name(i):
    return "all" if i == BROADCAST else str(i)


class PacketFramerTX(gr.basic_block):
    def __init__(self, src_id=1, dest_id=255, preamble_len=64, repeat_count=1, max_payload_len=1024,
                 preamble_byte=0xFF, buffer_size=2048, postamble_byte=0xFF, train_enable=1,
                 fec=15, interleave=1, samples_per_byte=32):
        gr.basic_block.__init__(
            self,
            name="Packet Framer TX (Burst, FEC, src/dest)",
            in_sig=None,
            out_sig=None,
        )
        self.src_id = int(src_id)
        if not 0 <= self.src_id <= 254:
            raise ValueError("src_id must be 0..254 (255 is the broadcast address)")
        self._dest_id = self._check_id(dest_id, BROADCAST)
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

    # dest_id is a property so the GUI box (GRC callback: blk.dest_id = value) can change it while running
    @property
    def dest_id(self):
        return self._dest_id

    @dest_id.setter
    def dest_id(self, value):
        new = self._check_id(value, None)
        if new is None:
            return
        if new != self._dest_id:
            self._dest_id = new
            print(f"[TX] default destination is now ID {id_name(new)}", flush=True)

    @staticmethod
    def _check_id(value, fallback):
        try:
            v = int(float(value))
        except (TypeError, ValueError):
            v = -1
        if 0 <= v <= 255:
            return v
        print(f"[TX] dest ID {value!r} ignored: use 0..254, or 255 for every node", flush=True)
        return fallback

    def start(self):
        print(f"[TX] this node: src ID {self.src_id}; messages go to dest ID {id_name(self._dest_id)} unless "
              f"they start with '@<id> ' or '@all '", flush=True)
        return True

    # ---------- input parsing ----------
    @staticmethod
    def _extract(msg):
        """Returns (payload bytes, dst from metadata or None, is_text)."""
        meta_dst = None
        if pmt.is_pair(msg):
            meta, data = pmt.car(msg), pmt.cdr(msg)
            try:
                if pmt.is_dict(meta):
                    for key in ("dest_id", "dst"):
                        v = pmt.dict_ref(meta, pmt.intern(key), pmt.PMT_NIL)
                        if pmt.is_integer(v):
                            meta_dst = pmt.to_long(v) & 0xFF
                            break
            except Exception:
                pass
            if pmt.is_u8vector(data):
                return bytes(pmt.u8vector_elements(data)), meta_dst, False
            return str(pmt.to_python(data)).encode("utf-8"), meta_dst, True
        if pmt.is_symbol(msg):
            return pmt.symbol_to_string(msg).encode("utf-8"), None, True
        val = pmt.to_python(msg)
        if isinstance(val, (bytes, bytearray)):
            return bytes(val), None, False
        return str(val).encode("utf-8"), None, True

    @staticmethod
    def _parse_at(payload):
        """'@3 hello' -> (3, b'hello'); '@all hi' / '@* hi' -> (255, b'hi'); otherwise (None, payload)."""
        if not payload.startswith(b"@"):
            return None, payload
        head, _, rest = payload[1:].partition(b" ")
        h = head.decode("ascii", "replace").lower()
        if h in ("all", "*"):
            return BROADCAST, rest
        if h.isdigit() and 0 <= int(h) <= 255:
            return int(h), rest
        return None, payload

    # ---------- frame building ----------
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
            payload, dst, is_text = self._extract(msg)
        except Exception as e:
            print(f"[TX] Could not parse message: {e}", flush=True)
            return
        if dst is None and is_text:
            dst, payload = self._parse_at(payload)
        if dst is None:
            dst = self._dest_id
        if dst == self.src_id:
            print(f"[TX] message addressed to this node (ID {dst}) not sent", flush=True)
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
        shown = payload.decode("utf-8", "replace") if is_text else f"{len(payload)} B binary"
        quiet = not is_text and payload[:1] == b"\xC5"
        quiet or print(f"[TX] msg #{msg_id} src ID {self.src_id} -> dest ID {id_name(dst)}: '{shown}' "
              f"({n_reps} bursts queued, {fec_txt})", flush=True)

        for rep in range(n_reps):
            header = len(payload).to_bytes(2, "big") + bytes([dst, self.src_id, msg_id, rep, n_reps])
            frame = self.build_frame(header, payload)

            meta = pmt.make_dict()
            meta = pmt.dict_add(meta, pmt.intern("tx_sob"), pmt.PMT_T)
            meta = pmt.dict_add(meta, pmt.intern("tx_eob"), pmt.PMT_T)
            sob_tag = pmt.make_tuple(pmt.from_long(0), pmt.intern("tx_sob"), pmt.PMT_T)
            eob_tag = pmt.make_tuple(pmt.from_long(len(frame) - 1), pmt.intern("tx_eob"), pmt.PMT_T)
            meta = pmt.dict_add(meta, pmt.intern("extra_tags"), pmt.list2(sob_tag, eob_tag))

            vec = pmt.init_u8vector(len(frame), list(frame))
            self.message_port_pub(pmt.intern("pdu_out"), pmt.cons(meta, vec))
