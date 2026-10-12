"""
Embedded Python Block: Fast Burst Packet Deframer RX (Hamming FEC) - v6.2, with stage diagnostics
- Vectorized NumPy sliding-window sync detection; positive correlation only (diff decoder resolves 180 deg)
- fec = 0 / 15 / 31 and interleave must match the TX framer
- Hamming decode: header section first (gives the length), then payload+CRC section; CRC32 over header+payload
- Sender filter: delivers only frames whose ID byte (written by the sender's framer as its MY_ID) == peer_id
- Repeat filter: a frame is a repeat only if it has the same (sender, msg_id) as the last message, a burst
  number not seen yet, and arrives within dup_window_s. A restarted sender (msg_id back to 0) is shown again.

Diagnostics (debug = 1), at most max_dbg_lines lines per second:
  [RX dbg] train word ...  for every 'corr_est' tag (the correlator found the train word), once the bits behind it
                           have arrived: bit errors in the train word and in the sync word right after it, counted
                           on the decoded bits (after equalizer, Costas, slicer, diff decoder, i.e. what this block
                           gets). The deframer locks only if the sync word has <= max_bit_errors errors.
  [RX dbg] frame ...       for every sync word this block locks on: header length, CRC result, bits fixed by the
                           FEC, squelch closures inside the frame ('squelch_eob' tags: the power squelch with
                           gate = True dropped samples there), sender ID and what happened to the frame.
  [RX stats] ...           every stats_every_s while there is traffic: counters for each stage.
own_id is only used in the messages (this node's MY_ID, i.e. the ID its own frames carry).
"""
import time
import zlib
import numpy as np
import pmt
from gnuradio import gr

SYNC_WORD = bytes([0x1A, 0xCF, 0xFC, 0x1D])
TRAIN_WORD = bytes([0xF8, 0xDD, 0x42, 0x58])     # must match the framer / TRAIN_BITS
HDR_BYTES = 6  # len (2B) + sender/dest id (1B) + msg_id (1B) + rep (1B) + n_reps (1B)
HIST_BITS = 1 << 16


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


def _pm(b):
    return np.unpackbits(np.frombuffer(b, np.uint8)).astype(np.int16) * 2 - 1


class PacketDeframerRX(gr.sync_block):
    def __init__(self, peer_id=1, max_bit_errors=2, max_payload_len=1024, bit_rate=375000,
                 fec=15, interleave=True, own_id=-1, debug=1, max_dbg_lines=20, stats_every_s=10.0,
                 dup_window_s=2.0):
        gr.sync_block.__init__(
            self,
            name="Packet Deframer RX (Fast Burst, Hamming, diag)",
            in_sig=[np.uint8],
            out_sig=None,
        )
        self.peer_id = int(peer_id) & 0xFF
        self.own_id = int(own_id)
        self.max_bit_errors = int(max_bit_errors)
        self.max_payload_len = int(max_payload_len)
        self.bit_rate = float(bit_rate)
        self.fec = int(fec)
        self.interleave = bool(interleave)
        self.debug = int(debug)
        self.max_dbg_lines = max(1, int(max_dbg_lines))
        self.stats_every_s = float(stats_every_s)
        self.dup_window_s = float(dup_window_s)
        if self.fec not in (0, 15, 31):
            raise ValueError("fec must be 0, 15 or 31")

        self._sync = _pm(SYNC_WORD)
        self._train = _pm(TRAIN_WORD)
        self._buf = np.zeros(0, dtype=np.uint8)
        self._buf_abs = 0                     # absolute input index of self._buf[0]

        # decoded-bit history for the train-word check
        self._hist = np.zeros(0, dtype=np.uint8)
        self._hist_abs = 0
        self._train_pending = []              # [(abs offset, rho)]
        self._train_seen = []                 # [(abs index where the sync word should start, rho, terr, serr)]
        self._eobs = []                       # abs offsets of squelch_eob tags (recent)

        self.n_sync = 0
        self.n_ok = 0
        self.n_crc_fail = 0
        self.n_bad_len = 0
        self.n_fec_corrected = 0
        self._c = dict.fromkeys(("train", "train_clean", "train_bad_sync", "train_garbage", "lock", "bad_len",
                                 "crc_fail", "crc_fail_cut", "ok", "wrong_id", "delivered", "repeat", "sq_eob"), 0)
        self._c_last = dict(self._c)
        self._t_stats = time.monotonic()
        self._dbg_t = 0.0
        self._dbg_n = 0
        self._dbg_dropped = 0
        self._warned_id = False

        self._bits_total = 0
        self._bits_at_last_ok = 0
        self._cur_id = None
        self._cur_reps = set()
        self._cur_nreps = 0
        self._cur_corr = 0
        self._last = None                     # {"key": (sender, msg_id), "reps": set, "t": time}

        self.message_port_register_out(pmt.intern("pdu_out"))
        self.message_port_register_out(pmt.intern("text_out"))

    # ---------- start / stop ----------
    def start(self):
        own = f"MY_ID {self.own_id}" if self.own_id >= 0 else "this node"
        print(f"[RX] {own}: delivers frames sent by ID {self.peer_id} only "
              f"(FEC {self.fec or 'off'}, sync word may have <= {self.max_bit_errors} bit errors)", flush=True)
        if self.own_id == self.peer_id:
            print(f"[RX] WARNING: own_id == peer_id ({self.peer_id}). This node would accept its own frames; "
                  f"check MY_ID / peer_id.", flush=True)
        return True

    def stop(self):
        self._flush_summary()
        if self.debug:
            self._print_stats(final=True)
        return True

    # ---------- debug output ----------
    def _dbg(self, line):
        if not self.debug:
            return
        now = time.monotonic()
        if now - self._dbg_t >= 1.0:
            self._dbg_t, self._dbg_n = now, 0
        if self._dbg_n >= self.max_dbg_lines:
            self._dbg_dropped += 1
            return
        self._dbg_n += 1
        if self._dbg_dropped:
            print(f"[RX dbg] ({self._dbg_dropped} lines not shown: more than {self.max_dbg_lines}/s)", flush=True)
            self._dbg_dropped = 0
        print(f"[RX dbg] {line}", flush=True)

    def _print_stats(self, final=False):
        c, l = self._c, self._c_last
        d = {k: c[k] - l[k] for k in c} if not final else c
        if not final and not any(d.values()):
            return
        span = "total" if final else "since last line"
        print(f"[RX stats] {span}: train words {d['train']} (clean {d['train_clean']}, sync word broken "
              f"{d['train_bad_sync']}, no train word in the bits {d['train_garbage']}) | sync locks {d['lock']} | "
              f"bad length {d['bad_len']} | CRC fail {d['crc_fail']} (squelch closed inside {d['crc_fail_cut']}) | "
              f"CRC ok {d['ok']}: wrong sender ID {d['wrong_id']}, delivered {d['delivered']}, repeats {d['repeat']} | "
              f"squelch closures {d['sq_eob']}", flush=True)
        self._c_last = dict(c)

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
        """Returns 'delivered' or 'repeat'."""
        sender, msg_id, rep, n_reps = header[2], header[3], header[4], header[5]
        now = time.monotonic()
        last = self._last
        repeat = (last is not None and last["key"] == (sender, msg_id) and rep not in last["reps"]
                  and now - last["t"] <= self.dup_window_s)
        self._bits_at_last_ok = self._bits_total
        if repeat:
            last["reps"].add(rep)
            last["t"] = now
            if rep not in self._cur_reps:
                self._cur_corr += corrected
            self._cur_reps.add(rep)
            return "repeat"

        self._flush_summary()
        self._last = {"key": (sender, msg_id), "reps": {rep}, "t": now}
        self._cur_id, self._cur_nreps, self._cur_reps, self._cur_corr = msg_id, n_reps, {rep}, corrected
        text = payload.decode("utf-8", errors="replace")
        print("\n" + "=" * 40, flush=True)
        print(f"[RX RECEIVED] #{msg_id}: {text}", flush=True)
        print("=" * 40 + "\n", flush=True)

        meta = pmt.make_dict()
        meta = pmt.dict_add(meta, pmt.intern("dest_id"), pmt.from_long(sender))
        meta = pmt.dict_add(meta, pmt.intern("msg_id"), pmt.from_long(msg_id))
        meta = pmt.dict_add(meta, pmt.intern("n_reps"), pmt.from_long(n_reps))
        meta = pmt.dict_add(meta, pmt.intern("fec_corrected"), pmt.from_long(corrected))
        vec = pmt.init_u8vector(len(payload), list(payload))
        self.message_port_pub(pmt.intern("pdu_out"), pmt.cons(meta, vec))
        self.message_port_pub(pmt.intern("text_out"), pmt.intern(text))
        return "delivered"

    # ---------- diagnostics helpers ----------
    def _read_tags(self, n):
        try:
            tags = self.get_tags_in_window(0, 0, n)
        except Exception:
            return
        for t in tags:
            try:
                key = pmt.symbol_to_string(t.key)
            except Exception:
                continue
            if key == "corr_est":
                try:
                    rho = float(pmt.to_double(t.value))
                except Exception:
                    rho = float("nan")
                self._train_pending.append((int(t.offset), rho))
                self._c["train"] += 1
            elif key == "squelch_eob":
                self._eobs.append(int(t.offset))
                self._c["sq_eob"] += 1
        if len(self._eobs) > 512:
            self._eobs = self._eobs[-256:]

    def _check_trains(self):
        """Look at the decoded bits around each correlator tag once enough bits have arrived."""
        end_abs = self._hist_abs + len(self._hist)
        keep = []
        for off, rho in self._train_pending:
            if off + 160 > end_abs:
                keep.append((off, rho))
                continue
            a = max(0, off - 64 - self._hist_abs)
            b = off + 160 - self._hist_abs
            seg = self._hist[a:b].astype(np.int16) * 2 - 1
            if len(seg) < 96:
                continue
            c = np.lib.stride_tricks.sliding_window_view(seg[:len(seg) - 32], 32) @ self._train
            j = int(np.argmax(c))
            terr = (32 - int(c[j])) // 2
            serr = (32 - int(seg[j + 32:j + 64] @ self._sync)) // 2
            sync_abs = self._hist_abs + a + j + 32
            self._train_seen.append((sync_abs, rho, terr, serr))
            if terr <= 3 and serr <= self.max_bit_errors:
                self._c["train_clean"] += 1
                verdict = "clean, deframer can lock"
            elif terr <= 3:
                self._c["train_bad_sync"] += 1
                verdict = (f"sync word has more than {self.max_bit_errors} errors -> no lock: bits go wrong right "
                           f"after the train word (equalizer / Costas / squelch)")
            else:
                self._c["train_garbage"] += 1
                verdict = ("the train word is not in the decoded bits: the demodulator output is wrong although the "
                           "correlator saw the word (equalizer, Costas, slicer, diff decoder, DC offset)")
            self._dbg(f"train word (corr {rho:.2f}): decoded train word {terr}/32 bits wrong, "
                      f"sync word {serr}/32 wrong -> {verdict}")
        self._train_pending = keep
        if len(self._train_seen) > 64:
            self._train_seen = self._train_seen[-32:]

    def _cuts(self, a, b):
        return sum(1 for e in self._eobs if a < e < b)

    def _keep(self, buf, start):
        start = max(0, min(start, len(buf)))
        self._buf = buf[start:].copy()
        self._buf_abs += start

    # ---------- main loop ----------
    def work(self, input_items, output_items):
        bits = input_items[0] & 1
        n_in = len(bits)
        self._bits_total += n_in
        if len(self._buf) == 0:
            self._buf_abs = self.nitems_read(0)
        self._buf = np.concatenate((self._buf, bits)) if len(self._buf) else bits.copy()

        if self.debug:
            self._read_tags(n_in)
            if len(self._hist) == 0:
                self._hist_abs = self.nitems_read(0)
            self._hist = np.concatenate((self._hist, bits))
            if len(self._hist) > HIST_BITS:
                cut = len(self._hist) - HIST_BITS
                self._hist = self._hist[cut:]
                self._hist_abs += cut
            self._check_trains()
            if time.monotonic() - self._t_stats >= self.stats_every_s:
                self._t_stats = time.monotonic()
                self._print_stats()

        # Flush transmission stats once channel has been quiet for > 0.5s of air time
        if self._cur_id is not None and self._bits_total - self._bits_at_last_ok > 0.5 * self.bit_rate:
            self._flush_summary()

        thr = 32 - 2 * self.max_bit_errors
        hdr_coded, hdr_blk = self._coded_len(HDR_BYTES)
        hdr_bits = 32 + hdr_coded

        while True:
            buf = self._buf
            if len(buf) < hdr_bits:
                return n_in

            pm = buf.astype(np.int16) * 2 - 1
            corr = np.lib.stride_tricks.sliding_window_view(pm, 32) @ self._sync
            cand = np.flatnonzero(corr >= thr)

            if cand.size == 0:
                self._keep(buf, len(buf) - 31)          # keep trailing bits that might begin a sync word
                return n_in

            i = int(cand[0])
            if len(buf) < i + hdr_bits:
                self._keep(buf, i)                      # await complete header bits
                return n_in

            header, hdr_corr = self._decode_section(buf[i + 32: i + hdr_bits], HDR_BYTES, hdr_blk)
            length = int.from_bytes(header[:2], "big")
            fa = self._buf_abs + i

            if length == 0 or length > self.max_payload_len:
                self.n_bad_len += 1
                self._c["bad_len"] += 1
                if self._near_train(fa):
                    self._dbg(f"frame: sync word found after a train word, but the header length {length} is "
                              f"invalid (FEC fixed {hdr_corr} bits) -> header corrupted")
                self._keep(buf, i + 1)                  # backtrack 1 bit past false sync
                continue

            body_coded, body_blk = self._coded_len(length + 4)
            end = i + hdr_bits + body_coded
            if len(buf) < end:
                self._keep(buf, i)                      # await complete frame body bits
                return n_in

            body, body_corr = self._decode_section(buf[i + hdr_bits: end], length + 4, body_blk)
            payload, rx_crc = body[:length], body[length:]
            self.n_sync += 1
            self._c["lock"] += 1
            fe = self._buf_abs + end
            cuts = self._cuts(fa - 32, fe)
            cut_txt = f", squelch closed {cuts}x inside the frame" if cuts else ""
            corrected = hdr_corr + body_corr

            if zlib.crc32(header + payload) == int.from_bytes(rx_crc, "big"):
                self.n_fec_corrected += corrected
                self.n_ok += 1
                self._c["ok"] += 1
                sender, msg_id = header[2], header[3]
                if sender == self.peer_id:
                    what = self._deliver(header, payload, corrected)
                    self._c[what] += 1
                    self._dbg(f"frame: {length} B, CRC ok (FEC fixed {corrected} bits), sender ID {sender}, "
                              f"msg #{msg_id} -> {'delivered' if what == 'delivered' else 'repeat burst, not shown again'}")
                else:
                    # Frame from another ID: not for us
                    self._c["wrong_id"] += 1
                    preview = payload[:40].decode("utf-8", errors="replace")
                    self._dbg(f"frame: {length} B, CRC ok, sender ID {sender}, msg #{msg_id} '{preview}' -> DROPPED: "
                              f"this node accepts only sender ID {self.peer_id}")
                    if not self._warned_id and (self.own_id < 0 or sender == self.own_id):
                        self._warned_id = True
                        print(f"[RX] WARNING: a good frame carried sender ID {sender}, the ID this node sends with. "
                              f"If this PC did not just send that text, the other PC has the same MY_ID: set "
                              f"MY_ID {self.peer_id} on the other PC. (If it is this PC's own text, its own burst "
                              f"leaked past the CSMA receive-off window.)", flush=True)
                self._keep(buf, end)                    # advance past valid frame
            else:
                self.n_crc_fail += 1
                self._c["crc_fail"] += 1
                if cuts:
                    self._c["crc_fail_cut"] += 1
                self._dbg(f"frame: header says {length} B from ID {header[2]} msg #{header[3]}, CRC FAIL "
                          f"(FEC fixed {corrected} bits, still wrong){cut_txt}")
                self._keep(buf, i + 1)                  # backtrack 1 bit past false sync

        return n_in

    def _near_train(self, sync_abs):
        return any(abs(s - sync_abs) <= 8 for s, _, _, _ in self._train_seen)
