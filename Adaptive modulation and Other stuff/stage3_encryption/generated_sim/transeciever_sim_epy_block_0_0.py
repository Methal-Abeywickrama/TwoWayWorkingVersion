"""
Embedded Python Block: Adaptive Burst Packet Framer TX (BPSK / QPSK)

Builds every burst as complex SYMBOLS (1 sample/symbol). Feed pdu_out into
PDU to Tagged Stream (Complex) -> Root Raised Cosine Filter (interpolating, interp = sps,
gain = sps) -> Tag Gate -> PlutoSDR Sink.

Modulation
- One QPSK grid (diagonal points, |s| = 1) for both modes, so one 4th-order Costas loop
  on the RX side handles everything.
- Differential encoding is done here, per burst, from a known reference symbol:
    BPSK : 1 bit/symbol,  bit b -> phase step of b * 180 deg
    QPSK : 2 bits/symbol, dibit -> Gray-coded phase step of 0/90/180/270 deg
- Preamble, sync word and header are ALWAYS BPSK; only payload + CRC switch.
- Postamble: postamble_len*8 symbols of constant phase (= the 160 zero bytes of the BPSK
  framer: a plain carrier). They push the last real samples out of the RRC filter and the
  Pluto sink's 4096-sample buffer, and keep the RX AGC level steady until the burst ends.

Frame (bytes before modulation)
  [preamble] [1A CF FC 1D] [hdr 8B] [payload] [CRC32 4B] [postamble carrier]
  hdr = len(2) | dest_id(1) | msg_id(1) | rep(1) | n_reps(1) | flags(1) | hdr_crc8(1)
  flags bit0 = payload MCS (0 BPSK, 1 QPSK); bit1 = "request valid";
        bit2 = MCS this node wants the PEER to use towards it
  dest_id = the PDU's 'dst_addr' metadata (from the SR-ARQ FSM), else the peer_id parameter.

Link adaptation (mcs_mode: -1 auto, 0 force BPSK, 1 force QPSK)
- link_in gets a dict from this node's deframer: my_req/snr_db (what we ask the peer for,
  carried in our headers) and peer_req (what the peer asked us to use).
- Auto mode uses peer_req while it is fresh (feedback_timeout s), else BPSK.
- Payloads shorter than qpsk_min_len (SYN/ACK/FIN control PDUs) are always BPSK.
- If QPSK frames get no reply at all from the peer for fallback_s seconds, the framer drops
  to BPSK until the peer is heard again (the peer cannot send feedback for frames it lost).
"""
import time
import zlib

import numpy as np
import pmt
from gnuradio import gr

SYNC_WORD = bytes([0x1A, 0xCF, 0xFC, 0x1D])
GRAY = np.array([0, 1, 3, 2], dtype=np.int64)   # dibit value -> phase step (x 90 deg)


def crc8(data, poly=0x07):
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def build_header(length, dest_id, msg_id, rep, n_reps, mcs, req_valid, req_mcs):
    flags = (mcs & 1) | ((1 if req_valid else 0) << 1) | ((req_mcs & 1) << 2)
    h7 = length.to_bytes(2, "big") + bytes([dest_id & 0xFF, msg_id & 0xFF, rep & 0xFF, n_reps & 0xFF, flags])
    return h7 + bytes([crc8(h7)])


def frame_to_symbols(bpsk_part, qpsk_part=b"", n_tail=0):
    """bpsk_part sent 1 bit/symbol, qpsk_part 2 bits/symbol, then n_tail constant-phase
    symbols; one leading reference symbol."""
    bb = np.unpackbits(np.frombuffer(bpsk_part, np.uint8)).astype(np.int64)
    steps = [2 * bb]
    if qpsk_part:
        qb = np.unpackbits(np.frombuffer(qpsk_part, np.uint8)).astype(np.int64)
        steps.append(GRAY[2 * qb[0::2] + qb[1::2]])
    steps.append(np.zeros(n_tail, np.int64))
    phase = np.concatenate(([0], np.cumsum(np.concatenate(steps)))) & 3
    return np.exp(1j * (np.pi / 4 + phase * np.pi / 2)).astype(np.complex64)


class PacketFramerTX(gr.basic_block):
    def __init__(self, peer_id=1, preamble_len=1024, repeat_count=1, max_payload_len=8192,
                 preamble_byte=0xFF, postamble_len=160, mcs_mode=-1, feedback_timeout=30.0,
                 qpsk_min_len=32, fallback_s=1.0):
        gr.basic_block.__init__(self, name="Adaptive Packet Framer TX", in_sig=None, out_sig=None)
        self.peer_id = int(peer_id) & 0xFF
        self.preamble_len = int(preamble_len)
        self.repeat_count = int(repeat_count)
        self.max_payload_len = int(max_payload_len)
        self.preamble_byte = int(preamble_byte) & 0xFF
        self.postamble_len = max(0, int(postamble_len))
        self.mcs_mode = int(mcs_mode)
        self.feedback_timeout = float(feedback_timeout)
        self.qpsk_min_len = int(qpsk_min_len)
        self.fallback_s = float(fallback_s)
        self._msg_id = 0

        self._peer_req = None          # MCS the peer asked us to use
        self._peer_req_t = 0.0
        self._my_req = None            # MCS our receiver wants the peer to use
        self._my_snr = None
        self._qpsk_unanswered = None   # time of the first QPSK frame sent since we last heard the peer

        self.message_port_register_in(pmt.intern("msg_in"))
        self.set_msg_handler(pmt.intern("msg_in"), self.handle_msg)
        self.message_port_register_in(pmt.intern("link_in"))
        self.set_msg_handler(pmt.intern("link_in"), self.handle_link)
        self.message_port_register_out(pmt.intern("pdu_out"))

    # ------------------------------------------------------------------ feedback
    def handle_link(self, msg):
        if not pmt.is_dict(msg):
            return
        self._qpsk_unanswered = None   # we heard the peer
        k_peer, k_my, k_snr = pmt.intern("peer_req"), pmt.intern("my_req"), pmt.intern("snr_db")
        if pmt.dict_has_key(msg, k_peer):
            self._peer_req = 1 if pmt.to_long(pmt.dict_ref(msg, k_peer, pmt.PMT_NIL)) == 1 else 0
            self._peer_req_t = time.monotonic()
        if pmt.dict_has_key(msg, k_my):
            self._my_req = 1 if pmt.to_long(pmt.dict_ref(msg, k_my, pmt.PMT_NIL)) == 1 else 0
        if pmt.dict_has_key(msg, k_snr):
            self._my_snr = pmt.to_double(pmt.dict_ref(msg, k_snr, pmt.PMT_NIL))

    def _select_mcs(self, n_payload):
        mode = int(self.mcs_mode)
        if mode in (0, 1):
            return mode, "forced"
        if n_payload < self.qpsk_min_len:
            return 0, "short frame"
        now = time.monotonic()
        if self._peer_req is None:
            return 0, "no feedback yet"
        if now - self._peer_req_t >= self.feedback_timeout:
            return 0, "feedback stale"
        if self._peer_req == 0:
            return 0, "peer request"
        if self._qpsk_unanswered is None:
            self._qpsk_unanswered = now
        elif now - self._qpsk_unanswered > self.fallback_s:
            return 0, "QPSK unanswered, fallback"
        return 1, "peer request"

    # ------------------------------------------------------------------ data
    def _dest_of(self, msg):
        if pmt.is_pair(msg) and pmt.is_dict(pmt.car(msg)):
            key = pmt.intern("dst_addr")
            if pmt.dict_has_key(pmt.car(msg), key):
                v = pmt.dict_ref(pmt.car(msg), key, pmt.PMT_NIL)
                try:
                    return int(pmt.to_uint64(v)) & 0xFF
                except Exception:
                    try:
                        return int(pmt.to_long(v)) & 0xFF
                    except Exception:
                        pass
        return self.peer_id

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
        dest = self._dest_of(msg)
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

        mcs, why = self._select_mcs(len(payload))
        msg_id = self._msg_id
        self._msg_id = (self._msg_id + 1) & 0xFF
        n_reps = max(1, min(255, self.repeat_count))
        preamble = bytes([self.preamble_byte]) * self.preamble_len
        req_valid = self._my_req is not None
        req = self._my_req if req_valid else 0

        ask = ("QPSK" if req else "BPSK") if req_valid else "-"
        snr = f" rxSNR {self._my_snr:.1f}dB" if self._my_snr is not None else ""
        print(f"[TX] #{msg_id} -> {dest} {'QPSK' if mcs else 'BPSK'} ({why}) {len(payload)} B"
              f"{f' x{n_reps}' if n_reps > 1 else ''} | ask peer: {ask}{snr}", flush=True)

        n_tail = self.postamble_len * 8
        for rep in range(n_reps):
            hdr = build_header(len(payload), dest, msg_id, rep, n_reps, mcs, req_valid, req)
            crc = zlib.crc32(hdr + payload).to_bytes(4, "big")
            head = preamble + SYNC_WORD + hdr
            body = payload + crc
            if mcs == 0:
                syms = frame_to_symbols(head + body, n_tail=n_tail)
            else:
                syms = frame_to_symbols(head, body, n_tail=n_tail)
            vec = pmt.init_c32vector(len(syms), syms.tolist())
            self.message_port_pub(pmt.intern("pdu_out"), pmt.cons(pmt.PMT_NIL, vec))
