"""
Embedded Python Block: Adaptive Burst Packet Deframer RX (BPSK / QPSK)

Input: complex symbols at 1 sample/symbol, taken straight from the Costas loop
(order 4). The constellation decoder, diff decoder and unpack-bits blocks are NOT used.

- Differential detection: z[n] = r[n] * conj(r[n-1]). Works for any carrier phase
  (Costas 90/180 deg ambiguity is irrelevant) and resets cleanly on every burst.
    BPSK bits : Re(z) < 0
    QPSK bits : angle(z) quantised to 90 deg, then Gray-decoded
- Sync + header are always BPSK; header CRC-8 is checked BEFORE trusting len/MCS,
  so a false sync no longer makes the block wait for a random huge length.
- Payload is decoded with the MCS written in the header; CRC32 over hdr + payload.
- Data-aided SNR estimate on every CRC-valid frame addressed to us: the transmitted
  symbols are rebuilt from the decoded bits, a complex gain is fitted, and
  SNR = |gain|^2 / mean|residual|^2. Idle noise is never measured.
- Hysteresis on the averaged SNR decides which MCS we want the peer to use (my_req).
  A header-valid QPSK frame that fails CRC forces my_req back to BPSK at once.
- link_out (PMT dict) -> framer link_in:
    my_req, snr_db : our recommendation for the peer (framer puts it in our headers)
    peer_req       : what the peer's receiver asked us to use (framer obeys it)
"""
import zlib

import numpy as np
import pmt
from gnuradio import gr

SYNC_WORD = bytes([0x1A, 0xCF, 0xFC, 0x1D])
HDR_BYTES = 8
SYNC_BITS = 32
HDR_BITS = HDR_BYTES * 8
GRAY_INV = np.array([0, 1, 3, 2], dtype=np.int64)   # phase step -> dibit value


def crc8(data, poly=0x07):
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


class PacketDeframerRX(gr.sync_block):
    def __init__(self, peer_id=0, max_bit_errors=1, max_payload_len=8192, sym_rate=375000,
                 snr_up_db=16.0, snr_down_db=13.0, snr_avg=0.2):
        gr.sync_block.__init__(self, name="Adaptive Packet Deframer RX",
                               in_sig=[np.complex64], out_sig=None)
        self.peer_id = int(peer_id) & 0xFF          # this node's own ID (frames for us)
        self.max_bit_errors = int(max_bit_errors)
        self.max_payload_len = int(max_payload_len)
        self.sym_rate = float(sym_rate)
        self.snr_up_db = float(snr_up_db)
        self.snr_down_db = float(snr_down_db)
        self.snr_avg = float(snr_avg)

        self._sync = (np.unpackbits(np.frombuffer(SYNC_WORD, np.uint8)).astype(np.int16) * 2 - 1)
        self._sym = np.zeros(0, dtype=np.complex64)

        self.n_sync = self.n_ok = self.n_crc_fail = self.n_bad_hdr = 0
        self._syms_total = 0
        self._syms_at_last_ok = 0
        self._cur_id = None
        self._cur_reps = set()
        self._cur_nreps = 0
        self._cur_mcs = 0
        self._cur_snr = []

        self._snr_ema = None
        self._my_req = 0          # MCS we want the peer to use towards us

        self.message_port_register_out(pmt.intern("pdu_out"))
        self.message_port_register_out(pmt.intern("text_out"))
        self.message_port_register_out(pmt.intern("link_out"))

    # ------------------------------------------------------------------ helpers
    def _publish_link(self, **kv):
        d = pmt.make_dict()
        for k, v in kv.items():
            val = pmt.from_double(float(v)) if isinstance(v, float) else pmt.from_long(int(v))
            d = pmt.dict_add(d, pmt.intern(k), val)
        self.message_port_pub(pmt.intern("link_out"), d)

    def _flush_summary(self):
        if self._cur_id is not None:
            got, n = len(self._cur_reps), self._cur_nreps
            loss = 100.0 * (n - got) / max(n, 1)
            snr = f", SNR {np.mean(self._cur_snr):.1f} dB" if self._cur_snr else ""
            print(f"[RX] msg #{self._cur_id}: {got}/{n} bursts OK (Loss: {loss:.0f}%) "
                  f"[{'QPSK' if self._cur_mcs else 'BPSK'}{snr}]", flush=True)
        self._cur_id = None
        self._cur_reps = set()
        self._cur_snr = []

    def _update_mcs(self, snr_db):
        a = self.snr_avg
        self._snr_ema = snr_db if self._snr_ema is None else (1 - a) * self._snr_ema + a * snr_db
        old = self._my_req
        if self._my_req == 0 and self._snr_ema > self.snr_up_db:
            self._my_req = 1
        elif self._my_req == 1 and self._snr_ema < self.snr_down_db:
            self._my_req = 0
        if self._my_req != old:
            print(f"[LINK] avg SNR {self._snr_ema:.1f} dB -> asking peer for "
                  f"{'QPSK' if self._my_req else 'BPSK'}", flush=True)

    def _qpsk_failure(self):
        if self._my_req == 1:
            print("[LINK] QPSK frame failed CRC -> asking peer for BPSK", flush=True)
        self._my_req = 0
        if self._snr_ema is not None:
            self._snr_ema = min(self._snr_ema, self.snr_down_db)
        self._publish_link(my_req=self._my_req)

    @staticmethod
    def _snr_estimate(r, steps, win=33):
        """Data-aided SNR. r: received symbols (len N+1), steps: phase steps (len N, x 90 deg).
        The known modulation is stripped off, then a sliding average gives the local complex
        gain, so leftover carrier rotation does not count as noise."""
        u = np.exp(1j * (np.pi / 2) * np.concatenate(([0], np.cumsum(steps))))
        w = r * np.conj(u)                         # = gain(t) + noise
        win = min(win, len(w) | 1)
        g = np.convolve(w, np.ones(win) / win, mode="valid")
        h = win // 2
        e = w[h:len(w) - h] - g
        noise = np.mean(np.abs(e) ** 2) * win / (win - 1)   # undo self-inclusion bias
        sig = np.mean(np.abs(g) ** 2) - noise / win
        return 10 * np.log10(max(sig / max(noise, 1e-12), 1e-3))

    def _deliver(self, hdr, payload, snr_db):
        dest_id, msg_id, rep, n_reps, flags = hdr[2], hdr[3], hdr[4], hdr[5], hdr[6]
        if msg_id != self._cur_id:
            self._flush_summary()
            self._cur_id, self._cur_nreps, self._cur_mcs = msg_id, n_reps, flags & 1
            text = payload.decode("utf-8", errors="replace")
            print("\n" + "=" * 40, flush=True)
            print(f"[RX RECEIVED] #{msg_id} ({'QPSK' if flags & 1 else 'BPSK'}): {text}", flush=True)
            print("=" * 40 + "\n", flush=True)

            meta = pmt.make_dict()
            meta = pmt.dict_add(meta, pmt.intern("dest_id"), pmt.from_long(dest_id))
            meta = pmt.dict_add(meta, pmt.intern("msg_id"), pmt.from_long(msg_id))
            meta = pmt.dict_add(meta, pmt.intern("n_reps"), pmt.from_long(n_reps))
            meta = pmt.dict_add(meta, pmt.intern("mcs"), pmt.from_long(flags & 1))
            meta = pmt.dict_add(meta, pmt.intern("snr_db"), pmt.from_double(float(snr_db)))
            vec = pmt.init_u8vector(len(payload), list(payload))
            self.message_port_pub(pmt.intern("pdu_out"), pmt.cons(meta, vec))
            self.message_port_pub(pmt.intern("text_out"), pmt.intern(text))

        self._cur_reps.add(rep)
        self._cur_snr.append(snr_db)
        self._syms_at_last_ok = self._syms_total

        self._update_mcs(snr_db)
        link = dict(my_req=self._my_req, snr_db=float(self._snr_ema))
        if flags & 0x02:                      # peer included a request for us
            link["peer_req"] = (flags >> 2) & 1
        self._publish_link(**link)

    # ------------------------------------------------------------------ work
    def work(self, input_items, output_items):
        x = input_items[0]
        n_in = len(x)
        self._syms_total += n_in
        self._sym = np.concatenate((self._sym, x)) if len(self._sym) else x.copy()

        if self._cur_id is not None and self._syms_total - self._syms_at_last_ok > 0.5 * self.sym_rate:
            self._flush_summary()

        sym = self._sym
        if len(sym) < SYNC_BITS + HDR_BITS + 1:
            return n_in

        z = sym[1:] * np.conj(sym[:-1])               # z[k] uses sym[k] and sym[k+1]
        nz = len(z)
        bits = (z.real < 0).astype(np.uint8)          # BPSK-mode hard bits
        pm = bits.astype(np.int16) * 2 - 1
        corr = np.lib.stride_tricks.sliding_window_view(pm, SYNC_BITS) @ self._sync
        cand = np.flatnonzero(corr >= SYNC_BITS - 2 * self.max_bit_errors)

        pos = 0           # z index up to which everything has been consumed
        waiting = None    # z index of a frame that is still incomplete
        for i in cand:
            i = int(i)
            if i < pos:
                continue                              # inside a frame already decoded
            h0 = i + SYNC_BITS
            if h0 + HDR_BITS > nz:
                waiting = i                           # header not complete yet
                break
            hdr = np.packbits(bits[h0:h0 + HDR_BITS]).tobytes()
            length = int.from_bytes(hdr[:2], "big")
            if crc8(hdr[:7]) != hdr[7] or length == 0 or length > self.max_payload_len:
                self.n_bad_hdr += 1                   # false sync: try next candidate
                continue

            mcs = hdr[6] & 1
            p0 = h0 + HDR_BITS
            nbytes = length + 4
            end = p0 + (nbytes * 8 if mcs == 0 else nbytes * 4)
            if end > nz:
                waiting = i                           # wait for the rest of the frame
                break

            if mcs == 0:
                pay_steps = 2 * bits[p0:end].astype(np.int64)
                body = np.packbits(bits[p0:end]).tobytes()
            else:
                pay_steps = np.rint(np.angle(z[p0:end]) / (np.pi / 2)).astype(np.int64) & 3
                v = GRAY_INV[pay_steps]
                qbits = np.empty(2 * len(v), dtype=np.uint8)
                qbits[0::2] = v >> 1
                qbits[1::2] = v & 1
                body = np.packbits(qbits).tobytes()

            payload, rx_crc = body[:length], body[length:length + 4]
            self.n_sync += 1
            if zlib.crc32(hdr + payload) == int.from_bytes(rx_crc, "big"):
                if hdr[2] == self.peer_id:
                    self.n_ok += 1
                    steps = np.concatenate((2 * bits[i:p0].astype(np.int64), pay_steps))
                    snr_db = self._snr_estimate(sym[i:end + 1].astype(np.complex128), steps)
                    self._deliver(hdr, payload, snr_db)
                pos = end                             # skip past the valid frame
            else:
                self.n_crc_fail += 1                  # next candidate (1-bit backtrack)
                if mcs == 1 and hdr[2] == self.peer_id:
                    self._qpsk_failure()

        # Keep sym[keep:] so that z is rebuilt from the same index next call
        keep = waiting if waiting is not None else max(pos, nz - (SYNC_BITS - 1))
        self._sym = sym[keep:].copy()
        return n_in
