"""
Embedded Python Block: Adaptive Burst Packet Framer TX (BPSK / QPSK)

Builds the whole burst as complex SYMBOLS (1 sample per symbol). Feed pdu_out into
PDU to Tagged Stream (type: Complex) -> Root Raised Cosine Filter (interpolating,
interp = sps, gain = sps) -> Tag Gate -> PlutoSDR Sink.

Modulation
- One QPSK grid is used for everything (points on the diagonals, |s| = 1), so a single
  4th-order Costas loop on the RX side works for both modes.
- Differential encoding is done here, per burst, with a known reference symbol, so the
  receiver never depends on encoder state left over from the previous burst.
    BPSK mode : 1 bit / symbol,  bit b  -> phase change of b * 180 deg
    QPSK mode : 2 bits / symbol, dibit -> Gray-coded phase change of 0/90/180/270 deg
- Preamble, sync word and header are ALWAYS BPSK. Only the payload+CRC switches.

Frame layout (bytes, before modulation)
  [preamble] [1A CF FC 1D] [hdr: 8B] [payload] [CRC32: 4B]
  hdr = len(2) | dest_id(1) | msg_id(1) | rep(1) | n_reps(1) | flags(1) | hdr_crc8(1)
  flags bit0 = payload MCS (0 = BPSK, 1 = QPSK)
        bit1 = "mcs request valid"
        bit2 = MCS this node wants the PEER to use when sending to it
  CRC32 covers hdr + payload (same as the working framer); CRC-8 protects the header.

Link adaptation
- link_in receives a PMT dict from this node's own deframer (link_out):
    my_req   : MCS our receiver wants the peer to use (piggybacked in our headers)
    peer_req : MCS the peer's receiver asked us to use (selects our payload MCS)
- mcs_mode: -1 = auto (use peer_req, BPSK if none / stale), 0 = force BPSK, 1 = force QPSK.
  It is a plain attribute, so a GRC variable / QT chooser can change it at run time.
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


def frame_to_symbols(bpsk_part, qpsk_part=b""):
    """bpsk_part: bytes sent 1 bit/symbol; qpsk_part: bytes sent 2 bits/symbol.
    Returns complex64 symbols, including one leading reference symbol."""
    bb = np.unpackbits(np.frombuffer(bpsk_part, np.uint8)).astype(np.int64)
    steps = [2 * bb]
    if qpsk_part:
        qb = np.unpackbits(np.frombuffer(qpsk_part, np.uint8)).astype(np.int64)
        steps.append(GRAY[2 * qb[0::2] + qb[1::2]])
    phase = np.concatenate(([0], np.cumsum(np.concatenate(steps)))) & 3
    return np.exp(1j * (np.pi / 4 + phase * np.pi / 2)).astype(np.complex64)


class PacketFramerTX(gr.basic_block):
    def __init__(self, peer_id=1, preamble_len=1024, repeat_count=50, max_payload_len=8192,
                 preamble_byte=0xFF, mcs_mode=-1, feedback_timeout=30.0):
        gr.basic_block.__init__(self, name="Adaptive Packet Framer TX", in_sig=None, out_sig=None)
        self.peer_id = int(peer_id) & 0xFF
        self.preamble_len = int(preamble_len)
        self.repeat_count = int(repeat_count)
        self.max_payload_len = int(max_payload_len)
        self.preamble_byte = int(preamble_byte) & 0xFF
        self.mcs_mode = int(mcs_mode)
        self.feedback_timeout = float(feedback_timeout)
        self._msg_id = 0

        self._peer_req = None        # what the peer asked us to use
        self._peer_req_t = 0.0
        self._my_req = None          # what our RX wants the peer to use
        self._my_snr = None

        self.message_port_register_in(pmt.intern("msg_in"))
        self.set_msg_handler(pmt.intern("msg_in"), self.handle_msg)
        self.message_port_register_in(pmt.intern("link_in"))
        self.set_msg_handler(pmt.intern("link_in"), self.handle_link)
        self.message_port_register_out(pmt.intern("pdu_out"))

    # ------------------------------------------------------------------ feedback
    def handle_link(self, msg):
        if not pmt.is_dict(msg):
            return
        k_peer, k_my, k_snr = pmt.intern("peer_req"), pmt.intern("my_req"), pmt.intern("snr_db")
        if pmt.dict_has_key(msg, k_peer):
            self._peer_req = 1 if pmt.to_long(pmt.dict_ref(msg, k_peer, pmt.PMT_NIL)) == 1 else 0
            self._peer_req_t = time.monotonic()
        if pmt.dict_has_key(msg, k_my):
            self._my_req = 1 if pmt.to_long(pmt.dict_ref(msg, k_my, pmt.PMT_NIL)) == 1 else 0
        if pmt.dict_has_key(msg, k_snr):
            self._my_snr = pmt.to_double(pmt.dict_ref(msg, k_snr, pmt.PMT_NIL))

    def _select_mcs(self):
        mode = int(self.mcs_mode)
        if mode in (0, 1):
            return mode, "forced"
        fresh = self._peer_req is not None and (time.monotonic() - self._peer_req_t) < self.feedback_timeout
        if fresh:
            return self._peer_req, "peer request"
        return 0, "no feedback yet" if self._peer_req is None else "feedback stale"

    # ------------------------------------------------------------------ data
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

        mcs, why = self._select_mcs()
        msg_id = self._msg_id
        self._msg_id = (self._msg_id + 1) & 0xFF
        n_reps = max(1, min(255, self.repeat_count))
        preamble = bytes([self.preamble_byte]) * self.preamble_len
        req_valid = self._my_req is not None
        req = self._my_req if req_valid else 0

        snr_txt = f", our RX SNR {self._my_snr:.1f} dB" if self._my_snr is not None else ""
        print(f"[TX] msg #{msg_id} (to peer {self.peer_id}) {'QPSK' if mcs else 'BPSK'} ({why}): "
              f"'{payload.decode('utf-8', 'replace')}' ({n_reps} bursts; asking peer for "
              f"{('QPSK' if req else 'BPSK') if req_valid else 'n/a'}{snr_txt})", flush=True)

        for rep in range(n_reps):
            hdr = build_header(len(payload), self.peer_id, msg_id, rep, n_reps, mcs, req_valid, req)
            crc = zlib.crc32(hdr + payload).to_bytes(4, "big")
            head = preamble + SYNC_WORD + hdr
            body = payload + crc
            syms = frame_to_symbols(head + body) if mcs == 0 else frame_to_symbols(head, body)
            vec = pmt.init_c32vector(len(syms), syms.tolist())
            self.message_port_pub(pmt.intern("pdu_out"), pmt.cons(pmt.PMT_NIL, vec))
