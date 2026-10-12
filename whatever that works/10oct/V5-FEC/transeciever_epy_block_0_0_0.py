"""
Embedded Python Block: Fast Burst Packet Deframer RX
- Vectorized NumPy sliding-window sync detection (fast C execution, minimal GIL load)
- Positive correlation search only (180-deg phase ambiguity resolved upstream by diff decoder)
- 1-bit false sync backtracking (prevents dropped frames on noise triggers)
- Full CRC32 verification over header + payload
- Destination address filtering: drops frames where dest_id != peer_id
- Message deduplication: displays payload once per msg_id, then logs burst statistics
"""
import zlib
import numpy as np
import pmt
from gnuradio import gr

SYNC_WORD = bytes([0x1A, 0xCF, 0xFC, 0x1D])
HDR_BYTES = 6  # len (2B) + dest_id (1B) + msg_id (1B) + rep (1B) + n_reps (1B)


class PacketDeframerRX(gr.sync_block):
    def __init__(self, peer_id=1, max_bit_errors=2, max_payload_len=1024, bit_rate=375000):
        gr.sync_block.__init__(
            self,
            name="Packet Deframer RX (Fast Burst)",
            in_sig=[np.uint8],
            out_sig=None,
        )
        self.peer_id = int(peer_id) & 0xFF
        self.max_bit_errors = int(max_bit_errors)
        self.max_payload_len = int(max_payload_len)
        self.bit_rate = float(bit_rate)

        # Bipolar template (+1 / -1) for correlation
        self._sync = np.unpackbits(np.frombuffer(SYNC_WORD, np.uint8)).astype(np.int16) * 2 - 1
        self._buf = np.zeros(0, dtype=np.uint8)

        self.n_sync = 0
        self.n_ok = 0
        self.n_crc_fail = 0
        self.n_bad_len = 0

        self._bits_total = 0
        self._bits_at_last_ok = 0
        self._cur_id = None
        self._cur_reps = set()
        self._cur_nreps = 0

        self.message_port_register_out(pmt.intern("pdu_out"))
        self.message_port_register_out(pmt.intern("text_out"))

    def _flush_summary(self):
        if self._cur_id is not None:
            got, n = len(self._cur_reps), self._cur_nreps
            loss = 100.0 * (n - got) / max(n, 1)
            print(f"[RX] msg #{self._cur_id}: {got}/{n} bursts OK (Loss: {loss:.0f}%)", flush=True)
        self._cur_id = None
        self._cur_reps = set()

    def _deliver(self, header, payload):
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
            vec = pmt.init_u8vector(len(payload), list(payload))
            self.message_port_pub(pmt.intern("pdu_out"), pmt.cons(meta, vec))
            self.message_port_pub(pmt.intern("text_out"), pmt.intern(text))

        self._cur_reps.add(rep)
        self._bits_at_last_ok = self._bits_total

    def work(self, input_items, output_items):
        bits = input_items[0] & 1
        self._bits_total += len(bits)
        self._buf = np.concatenate((self._buf, bits)) if len(self._buf) else bits.copy()

        # Flush transmission stats once channel has been quiet for > 0.5s of air time
        if self._cur_id is not None and self._bits_total - self._bits_at_last_ok > 0.5 * self.bit_rate:
            self._flush_summary()

        thr = 32 - 2 * self.max_bit_errors
        hdr_bits = 32 + HDR_BYTES * 8

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

            header = np.packbits(buf[i + 32 : i + hdr_bits]).tobytes()
            length = int.from_bytes(header[:2], "big")

            if length == 0 or length > self.max_payload_len:
                self.n_bad_len += 1
                self._buf = buf[i + 1:].copy()  # Backtrack 1 bit past false sync
                continue

            end = i + 32 + (HDR_BYTES + length + 4) * 8
            if len(buf) < end:
                self._buf = buf[i:].copy()  # Await complete frame body bits
                return len(bits)

            body = np.packbits(buf[i + 32 : end]).tobytes()
            hdr, payload, rx_crc = body[:HDR_BYTES], body[HDR_BYTES : HDR_BYTES + length], body[-4:]
            self.n_sync += 1

            if zlib.crc32(hdr + payload) == int.from_bytes(rx_crc, "big"):
                dest_id = hdr[2]
                if dest_id == self.peer_id:
                    self.n_ok += 1
                    self._deliver(hdr, payload)
                # Frame addressed to another peer: discard silently
                self._buf = buf[end:].copy()  # Advance past valid frame
            else:
                self.n_crc_fail += 1
                self._buf = buf[i + 1:].copy()  # Backtrack 1 bit past false sync

        return len(bits)