"""
Embedded Python Block: Fast Burst Packet Deframer RX (with Hamming FEC)
- Vectorized NumPy sliding-window sync detection (fast C execution, minimal GIL load)
- Positive correlation search only (180-deg phase ambiguity resolved upstream by diff decoder)
- 1-bit false sync backtracking (prevents dropped frames on noise triggers)
- fec = 0 / 15 / 31 and interleave must match the TX framer
- Hamming decode: header section first (gives the length), then payload+CRC section
- Full CRC32 verification over header + payload (after FEC correction)
- Destination address filtering: drops frames where dest_id != peer_id
- Message deduplication: displays payload once per msg_id, then logs burst statistics
"""
import zlib
import numpy as np
import pmt
from gnuradio import gr

SYNC_WORD = bytes([0x1A, 0xCF, 0xFC, 0x1D])
HDR_BYTES = 6  # len (2B) + dest_id (1B) + msg_id (1B) + rep (1B) + n_reps (1B)


def hamming_tables(n):
    """Standard positional Hamming layout: parity bits at positions 1, 2, 4, 8, (16)."""
    m = {15: 4, 31: 5}[n]
    pos = np.arange(1, n + 1)
    is_parity = (pos & (pos - 1)) == 0
    data_idx = np.flatnonzero(~is_parity)
    H = ((pos[None, :] >> np.arange(m)[:, None]) & 1).astype(np.int32)   # m x n, column j = bin(j)
    weights = (1 << np.arange(m)).astype(np.int32)
    return data_idx, H, weights


def hamming_decode_bits(coded, n, nblk, interleave):
    """coded: nblk*n bits. Returns (data bits nblk*k, number of corrected bits)."""
    data_idx, H, weights = hamming_tables(n)
    c = np.asarray(coded, dtype=np.int32)
    cw = c.reshape(n, nblk).T.copy() if interleave else c.reshape(nblk, n).copy()
    S = ((cw @ H.T) & 1) @ weights          # syndrome = error position (1..n), 0 = no error
    bad = np.flatnonzero(S)
    cw[bad, S[bad] - 1] ^= 1                # flip bit at position S
    return cw[:, data_idx].reshape(-1).astype(np.uint8), int(bad.size)


class PacketDeframerRX(gr.sync_block):
    def __init__(self, peer_id=1, max_bit_errors=2, max_payload_len=1024, bit_rate=375000,
                 fec=15, interleave=True):
        gr.sync_block.__init__(
            self,
            name="Packet Deframer RX (Fast Burst, Hamming)",
            in_sig=[np.uint8],
            out_sig=None,
        )
        self.peer_id = int(peer_id) & 0xFF
        self.max_bit_errors = int(max_bit_errors)
        self.max_payload_len = int(max_payload_len)
        self.bit_rate = float(bit_rate)
        self.fec = int(fec)
        self.interleave = bool(interleave)
        if self.fec not in (0, 15, 31):
            raise ValueError("fec must be 0, 15 or 31")

        # Bipolar template (+1 / -1) for correlation
        self._sync = np.unpackbits(np.frombuffer(SYNC_WORD, np.uint8)).astype(np.int16) * 2 - 1
        self._buf = np.zeros(0, dtype=np.uint8)

        self.n_sync = 0
        self.n_ok = 0
        self.n_crc_fail = 0
        self.n_bad_len = 0
        self.n_fec_corrected = 0      # total bits corrected by Hamming in CRC-valid frames

        self._bits_total = 0
        self._bits_at_last_ok = 0
        self._cur_id = None
        self._cur_reps = set()
        self._cur_nreps = 0
        self._cur_corr = 0

        self.message_port_register_out(pmt.intern("pdu_out"))
        self.message_port_register_out(pmt.intern("text_out"))

    # ---------- section sizes (in channel bits) ----------
    def _coded_len(self, n_bytes):
        if self.fec == 0:
            return n_bytes * 8, 0
        nblk = -(-(n_bytes * 8) // {15: 11, 31: 26}[self.fec])
        return nblk * self.fec, nblk

    def _decode_section(self, coded, n_bytes, nblk):
        if self.fec == 0:
            return np.packbits(coded).tobytes(), 0
        bits, corr = hamming_decode_bits(coded, self.fec, nblk, self.interleave)
        return np.packbits(bits[:n_bytes * 8]).tobytes(), corr

    # ---------- statistics / delivery ----------
    def _flush_summary(self):
        if self._cur_id is not None:
            got, n = len(self._cur_reps), self._cur_nreps
            loss = 100.0 * (n - got) / max(n, 1)
            fec_txt = f", FEC corrected {self._cur_corr} bits" if self.fec else ""
            print(f"[RX] msg #{self._cur_id}: {got}/{n} bursts OK (Loss: {loss:.0f}%){fec_txt}", flush=True)
        self._cur_id = None
        self._cur_reps = set()
        self._cur_corr = 0

    def _deliver(self, header, payload, corrected):
        dest_id, msg_id, rep, n_reps = header[2], header[3], header[4], header[5]
        if msg_id != self._cur_id:
            self._flush_summary()
            self._cur_id, self._cur_nreps = msg_id, n_reps
            text = payload.decode("utf-8", errors="replace")
            print("\n" + "=" * 40, flush=True)
            print(f"[RX RECEIVED] #{msg_id}: {text}", flush=True)
            print("=" * 40 + "\n", flush=True)

            meta = pmt.make_dict()
            meta = pmt.dict_add(meta, pmt.intern("dest_id"), pmt.from_long(dest_id))
            meta = pmt.dict_add(meta, pmt.intern("msg_id"), pmt.from_long(msg_id))
            meta = pmt.dict_add(meta, pmt.intern("n_reps"), pmt.from_long(n_reps))
            meta = pmt.dict_add(meta, pmt.intern("fec_corrected"), pmt.from_long(corrected))
            vec = pmt.init_u8vector(len(payload), list(payload))
            self.message_port_pub(pmt.intern("pdu_out"), pmt.cons(meta, vec))
            self.message_port_pub(pmt.intern("text_out"), pmt.intern(text))

        if rep not in self._cur_reps:
            self._cur_corr += corrected
        self._cur_reps.add(rep)
        self._bits_at_last_ok = self._bits_total

    # ---------- main loop ----------
    def work(self, input_items, output_items):
        bits = input_items[0] & 1
        self._bits_total += len(bits)
        self._buf = np.concatenate((self._buf, bits)) if len(self._buf) else bits.copy()

        # Flush transmission stats once channel has been quiet for > 0.5s of air time
        if self._cur_id is not None and self._bits_total - self._bits_at_last_ok > 0.5 * self.bit_rate:
            self._flush_summary()

        thr = 32 - 2 * self.max_bit_errors
        hdr_coded, hdr_blk = self._coded_len(HDR_BYTES)
        hdr_bits = 32 + hdr_coded

        while True:
            buf = self._buf
            if len(buf) < hdr_bits:
                return len(bits)

            # Fast matrix dot product over 32-bit window (only positive peaks)
            pm = buf.astype(np.int16) * 2 - 1
            corr = np.lib.stride_tricks.sliding_window_view(pm, 32) @ self._sync
            cand = np.flatnonzero(corr >= thr)

            if cand.size == 0:
                self._buf = buf[-31:].copy()  # Preserve trailing bits that might begin a sync word
                return len(bits)

            i = int(cand[0])

            if len(buf) < i + hdr_bits:
                self._buf = buf[i:].copy()  # Await complete header bits
                return len(bits)

            header, hdr_corr = self._decode_section(buf[i + 32 : i + hdr_bits], HDR_BYTES, hdr_blk)
            length = int.from_bytes(header[:2], "big")

            if length == 0 or length > self.max_payload_len:
                self.n_bad_len += 1
                self._buf = buf[i + 1:].copy()  # Backtrack 1 bit past false sync
                continue

            body_coded, body_blk = self._coded_len(length + 4)
            end = i + hdr_bits + body_coded
            if len(buf) < end:
                self._buf = buf[i:].copy()  # Await complete frame body bits
                return len(bits)

            body, body_corr = self._decode_section(buf[i + hdr_bits : end], length + 4, body_blk)
            payload, rx_crc = body[:length], body[length:]
            self.n_sync += 1

            if zlib.crc32(header + payload) == int.from_bytes(rx_crc, "big"):
                corrected = hdr_corr + body_corr
                self.n_fec_corrected += corrected
                dest_id = header[2]
                if dest_id == self.peer_id:
                    self.n_ok += 1
                    self._deliver(header, payload, corrected)
                # Frame addressed to another peer: discard silently
                self._buf = buf[end:].copy()  # Advance past valid frame
            else:
                self.n_crc_fail += 1
                self._buf = buf[i + 1:].copy()  # Backtrack 1 bit past false sync

        return len(bits)
