"""
Embedded Python Block: CSMA/CA DCF MAC + Selective-Repeat ARQ with TXOP bursts and Block ACK        - v6.9
v6.9 adds: an unreliable service (metadata noack = 1, used for UDP datagrams: no Block ACK, no reordering,
burst of up to M frames, queue of udp_queue per destination, oldest dropped when full), a tx_status output (one
message per reliable frame that carries app_ref: acked / dropped / qfull), and a one-line 'txop' summary event per
channel access for the session logs.
(single frequency, half duplex, TX mute + RX blanking as in v6.7)

Wiring (same as v6.7)
  RX stream : DC blocker -> in0 -> out0 (zeroed while this node transmits) -> Power Squelch -> ... -> Deframer
  app_in    <- data to send: text ("@N text" = to node N, "@all text" = everyone) or PDUs (metadata dest_id)
  to_phy    -> Packet Framer TX msg_in            phy_in <- Packet Framer TX pdu_out (coded frames come back)
  pdu_out   -> PDU to Tagged Stream -> modulator -> Pluto sink
  rx_in     <- Packet Deframer RX pdu_out (accept_all = 1)
  app_out   -> received data for this node, IN ORDER per sender, duplicates removed, MAC header stripped
  ctrl      <- "flush" drops everything not yet sent
  tx_status -> {app_ref, app_seg, status (acked / dropped / qfull), dst, tx, ms}: delivery report per reliable frame

Two layers inside this block
  MAC (802.11 DCF + TXOP, from v6.7): p-persistence -> DIFS -> backoff (CW = (cw_min+1)*2^k - 1, k <= k_max) ->
      TXOP: up to M DATA frames to ONE destination, back to back in one transmission (802.11 A-MPDU / TXOP burst,
      limited to txop_limit_s of airtime) -> waits for ONE Block ACK.
      Block ACK received -> the TXOP succeeded: k = 0, the MAC goes straight back to contention;
                            the Block ACK is handed to the ARQ layer, which reads it afterwards.
      no Block ACK      -> k += 1 (CW doubles, up to k_max); every frame of the burst counts as not received.
  ARQ (selective repeat, per destination, 802.11 Block Ack session):
      sender window of N sequence numbers starting at WS (oldest frame not yet acknowledged). New frames get a
      sequence number only when they fit in [WS, WS+N). A burst takes the eligible frames in sequence order
      (retransmissions first, because they are older), at most M.
      Block ACK bit i = 1 -> burst frame i acknowledged; 0 -> retransmitted in a later burst. Each frame has its own
      retry counter; after retry_limit retransmissions it is dropped (802.11 per-MPDU retry limit). WS then moves
      past every acknowledged / dropped frame and new frames enter the window.
      The receiver keeps a reorder buffer of N (802.11 recipient scoreboard), delivers in order, removes
      duplicates, and gives up on a missing frame when the sender's WS has moved past it (the WS carried in every
      DATA frame does what an 802.11 BlockAckReq does).

MAC header (first bytes of the framer payload; the framer's CRC32 and Hamming FEC cover it)
  DATA (13 B):  FC | SN (2) | DURATION (2) | WS (2) | BSN (2) | IDX | CNT | REM (2)
       FC        1011 R T T   (TT 00 = DATA, 10 = Block ACK; R = contains a retransmission)
       SN        sequence number of this frame (16 bit, per destination)
       DURATION  NAV, 0.1 ms units: rest of the burst + SIFS + turnaround + Block ACK airtime (0 for broadcast)
       WS        sender's window start: everything before it is acknowledged or given up
       BSN       burst (TXOP) sequence number - the number the Block ACK answers
       IDX, CNT  position of this frame in the burst, number of frames in the burst
       REM       airtime of the burst still to come after this frame, 0.1 ms units (tells the receiver when to ACK)
  BLOCK ACK (8 B + ceil(M/8)):  FC | BSN (2) | DURATION (2) = 0 | RWS (2) | CNT | BITMAP
       BSN       the burst being answered
       RWS       receiver's window start = next sequence number it needs in order (cumulative ACK)
       BITMAP    CNT bits (M max), LSB first: bit i = burst frame i received with a good CRC
  Source / destination IDs are in the framer header. Broadcast DATA (dest 255): one frame per TXOP, no Block ACK.

Receiver timing: the Block ACK is sent SIFS after the burst ended: (decode time of the latest frame + its REM) +
sifs_s - i.e. SIFS after the last frame if that arrived, otherwise at the time it should have ended. It ignores
carrier sense, NAV and backoff and goes before any own DATA (802.11 immediate Block Ack).
Overheard frames for other nodes set the NAV (virtual carrier sense) from DURATION.

Timing constants (as v6.7): turnaround_s = frame end on air -> reply on air on this SDR (~20-30 ms);
DIFS = sifs_s + turnaround_s + 2 slot_s (difs_s > 0 overrides); Block ACK timeout ba_timeout_s after the burst
ended on air. Frame airtimes are computed from the framer settings (phy_preamble_len, phy_fec, phy_buffer_size,
samples_per_byte) - keep them equal to the framer's.

v6.9.2 (three changes against frames that get stuck; every one can be switched off, see the env variables)
  1. Longer preamble on the first frame of every transmission (DATA burst or Block ACK): the receiver acquires the
     signal from silence there and lost 30-36 % of those frames with 128 B (19 Oct logs; later frames 1-11 %).
     first_pre_len / env CDP_MAC_PRE1 (bytes, default 256; 0 or <= phy_preamble_len = off). The framer takes the
     length per frame from the PDU metadata key "pre_len".
  2. Duplicate after dup_after retries (env CDP_MAC_DUP_AFTER, default 4, 0 = off): a frame retried that often is
     sent twice in its burst - once in its place, once more at the end of the burst. The receiver keeps the first
     good copy (its usual duplicate filter); both copies are acknowledged by the same Block ACK.
  3. Window Block ACK (window_ba / env CDP_MAC_WBA, default 1): the Block ACK reports the receiver's whole
     reorder window, not only the frames of the burst it answers:
       WINDOW BLOCK ACK (8 B + ceil(N/8)):  FC (TT = 11) | BSN (2) | DURATION (2) = 0 | SSN (2) | NB | BITMAP (NB B)
         SSN     = the receiver's window start (next SN it needs in order; everything before it arrived)
         BITMAP  bit i (LSB first) = SN (SSN + i) is in the reorder buffer
     One Block ACK that gets through acknowledges every frame the receiver holds, also those of earlier bursts whose
     Block ACK was lost (19 Oct: 72 % of the received frames were such needless retransmissions). Both formats are
     always understood; the setting only chooses what this node sends. Both nodes need v6.9.2 (a v6.9 node does
     not know TT = 11).
v6.9.3
  4. Inside a burst the receiver is already locked: frames 2..M get a short preamble (pre_in / env CDP_MAC_PRE_IN,
     bytes, default 64; 0 = none, >= phy_preamble_len = off) and no postamble - only the last frame of the burst
     carries the postamble that pads the whole burst to whole Pluto buffers (at least one buffer, as before).
     The framer takes both per frame from the PDU metadata keys "pre_len" and "post_len".

Read-out for QT widgets (Function Probe): cs_db(), floor_db(), threshold_db(), is_busy(), is_tx(), queue_len(),
is_muted(), nav_ms(), cw(), window_info()
"""
import collections
import math
import os
import threading
import time

import numpy as np
import pmt
from gnuradio import gr

# ---- shared debug bus (one per flowgraph process; the Stress Test block writes it to disk) ----
import sys as _sys
import types as _types
import collections as _collections
import time as _time


def _bus():
    b = _sys.modules.get("cdp_dbg_bus")
    if b is None:
        nb = _types.ModuleType("cdp_dbg_bus")
        nb.events = _collections.deque(maxlen=400000)   # dicts; oldest dropped if nobody writes them
        nb.sources = {}                                 # name -> object with dbg_snapshot()
        b = _sys.modules.setdefault("cdp_dbg_bus", nb)
    return b


def _emit(blk, kind, /, **kw):
    kw["t"] = round(_time.time(), 6)
    kw["blk"] = blk
    kw["k"] = kind
    _bus().events.append(kw)
# ---- end debug bus ----

# ---- C++ kernels (cdp_fast.py / cdp_fast.cpp next to the flowgraph); None -> the NumPy code of this block ----
def _load_fast():
    import os as _os
    if _os.environ.get("CDP_FAST", "1").strip().lower() in ("0", "no", "off", "false"):
        return None
    try:
        import cdp_fast
        return cdp_fast
    except ImportError:
        pass
    for d in (_os.environ.get("CDP_HOME"), _os.getcwd(),
              _os.path.dirname(_os.path.abspath(_sys.argv[0])) if _sys.argv and _sys.argv[0] else None):
        if d and _os.path.isfile(_os.path.join(d, "cdp_fast.py")):
            if d not in _sys.path:
                _sys.path.insert(0, d)
            try:
                import cdp_fast
                return cdp_fast
            except Exception:
                return None
    return None
# ---- end C++ kernels ----

FC_BASE = 0xB0
T_DATA, T_BA = 0, 2
F_RETRY = 0x04
F_NOACK = 0x08          # unreliable frame (UDP / broadcast): no Block ACK, no reordering
T_WBA = 3               # v6.9.2: Block ACK with a bitmap of the receiver's whole window
HDR_DATA = 13
HDR_BA = 8
BCAST = 0xFF
MASK = 0xFFFF


def sdiff(a, b):
    """a - b in 16-bit sequence space, -32768..32767"""
    d = (a - b) & MASK
    return d - 0x10000 if d >= 0x8000 else d


def u16(x):
    return bytes([(x >> 8) & 0xFF, x & 0xFF])


def t01(s):
    return max(0, min(65535, int(round(s * 1e4))))


def data_header(sn, dur_s, ws, bsn, idx, cnt, rem_s, retry, noack=False):
    return (bytes([FC_BASE | (F_RETRY if retry else 0) | (F_NOACK if noack else 0) | T_DATA]) + u16(sn) + u16(t01(dur_s)) + u16(ws) + u16(bsn)
            + bytes([idx & 0xFF, cnt & 0xFF]) + u16(t01(rem_s)))


def ba_frame(bsn, rws, cnt, bits):
    nb = max(1, (cnt + 7) // 8)
    bm = bytearray(nb)
    for i in range(cnt):
        if bits >> i & 1:
            bm[i // 8] |= 1 << (i % 8)
    return bytes([FC_BASE | T_BA]) + u16(bsn) + u16(0) + u16(rws) + bytes([cnt & 0xFF]) + bytes(bm)


def wba_frame(bsn, ssn, nbits, wbits):
    """v6.9.2 window Block ACK: SSN = receiver window start, bit i = SN (SSN + i) received (LSB first)."""
    nb = max(1, (nbits + 7) // 8)
    bm = bytearray(nb)
    for i in range(8 * nb):
        if wbits >> i & 1:
            bm[i // 8] |= 1 << (i % 8)
    return bytes([FC_BASE | T_WBA]) + u16(bsn) + u16(0) + u16(ssn) + bytes([nb]) + bytes(bm)


def wba_has(h, sn):
    """Does window Block ACK h say that SN sn has arrived?"""
    d = sdiff(sn, h["rws"])
    return d < 0 or (d < h["nbits"] and bool(h["wbits"] >> d & 1))


def parse_mac(b):
    if len(b) < 5 or (b[0] & 0xF0) != FC_BASE:
        return None
    t = b[0] & 0x03
    g = lambda i: (b[i] << 8) | b[i + 1]
    if t == T_DATA and len(b) >= HDR_DATA:
        return {"type": T_DATA, "retry": bool(b[0] & F_RETRY), "noack": bool(b[0] & F_NOACK), "sn": g(1), "dur": g(3) / 1e4, "ws": g(5),
                "bsn": g(7), "idx": b[9], "cnt": b[10], "rem": g(11) / 1e4}
    if t == T_BA and len(b) >= HDR_BA + 1:
        cnt = b[7]
        bits = 0
        for i in range(min(cnt, 8 * (len(b) - HDR_BA))):
            if b[HDR_BA + i // 8] >> (i % 8) & 1:
                bits |= 1 << i
        return {"type": T_BA, "bsn": g(1), "dur": g(3) / 1e4, "rws": g(5), "cnt": cnt, "bits": bits}
    if t == T_WBA and len(b) >= HDR_BA + 1:
        nb = min(b[7], len(b) - HDR_BA)
        wbits = 0
        for i in range(8 * nb):
            if b[HDR_BA + i // 8] >> (i % 8) & 1:
                wbits |= 1 << i
        return {"type": T_WBA, "bsn": g(1), "dur": g(3) / 1e4, "rws": g(5), "nbits": 8 * nb, "wbits": wbits,
                "cnt": 0, "bits": 0}
    return None


def _unique(recs):
    """records in first-seen order without repeats (a burst may carry a frame twice)"""
    seen, out = set(), []
    for r in recs:
        if id(r) not in seen:
            seen.add(id(r))
            out.append(r)
    return out


def id_name(i):
    return "all" if i == BCAST else str(i)


def bitstr(bits, cnt):
    return "".join("1" if bits >> i & 1 else "0" for i in range(cnt))


# ======================================================================================== ARQ: sender side
class TxSession:
    """Originator side of a Block Ack session to one destination (selective repeat, window N)."""

    def __init__(self, dst, n_win, retry_limit):
        self.dst = dst
        self.N = n_win
        self.retry_limit = retry_limit
        self.ws = 0
        self.next_sn = 0
        self.frames = {}                 # sn -> record
        self.backlog = collections.deque()

    def admit(self):
        new = []
        while self.backlog and sdiff(self.next_sn, self.ws) < self.N:
            payload, keep, t_enq = self.backlog.popleft()
            r = {"sn": self.next_sn, "payload": payload, "keep": keep, "t_enq": t_enq, "retries": 0,
                 "state": "new", "tx": 0}
            self.frames[self.next_sn] = r
            new.append(r)
            self.next_sn = (self.next_sn + 1) & MASK
        return new

    def in_window(self):
        return [self.frames[(self.ws + i) & MASK] for i in range(sdiff(self.next_sn, self.ws))
                if (self.ws + i) & MASK in self.frames]

    def eligible(self):
        return [r for r in self.in_window() if r["state"] in ("new", "retx")]

    def pending(self):
        return any(r["state"] in ("new", "retx") for r in list(self.frames.values()))

    def outstanding(self):
        return len(self.frames) + len(self.backlog)

    def _done(self, r, state):
        r["state"] = state

    def slide(self):
        moved = 0
        while self.ws != self.next_sn:
            r = self.frames.get(self.ws)
            if r is not None and r["state"] not in ("acked", "dropped"):
                break
            self.frames.pop(self.ws, None)
            self.ws = (self.ws + 1) & MASK
            moved += 1
        return moved

    def on_block_ack(self, burst, bits, rws):
        """burst: list of records in burst order (a record twice if it was sent twice). Returns (acked, nacked,
        dropped) lists of records."""
        acked, nacked, dropped = [], [], []
        got = {}
        for i, r in enumerate(burst):
            got[id(r)] = got.get(id(r), False) or bool(bits >> i & 1)
        for r in _unique(burst):
            if r["state"] in ("acked", "dropped"):
                continue
            ok = got[id(r)] or sdiff(r["sn"], rws) < 0
            if ok:
                r["state"] = "acked"
                acked.append(r)
            else:
                self._fail(r, nacked, dropped)
        # cumulative part: everything before the receiver's window start has arrived (or was given up)
        ids = {id(r) for r in burst}
        for r in self.in_window():
            if r["state"] not in ("acked", "dropped") and sdiff(r["sn"], rws) < 0 and id(r) not in ids:
                r["state"] = "acked"
                acked.append(r)
        return acked, nacked, dropped

    def on_window_ack(self, burst, h):
        """v6.9.2 window Block ACK h (SSN + bitmap of the receiver's window): every frame in our window that the
        receiver has is acknowledged - also frames of earlier bursts; a frame of this burst it does not have is
        counted as failed. burst = [] for a late or stray Block ACK (only acknowledgements then)."""
        acked, nacked, dropped = [], [], []
        mine = {id(r) for r in burst}
        for r in self.in_window():
            if r["state"] in ("acked", "dropped"):
                continue
            if wba_has(h, r["sn"]):
                r["state"] = "acked"
                acked.append(r)
            elif id(r) in mine and r["state"] == "out":
                self._fail(r, nacked, dropped)
        return acked, nacked, dropped

    def on_no_ba(self, burst):
        nacked, dropped = [], []
        for r in _unique(burst):
            if r["state"] == "out":
                self._fail(r, nacked, dropped)
        return nacked, dropped

    def _fail(self, r, nacked, dropped):
        r["retries"] += 1
        if r["retries"] > self.retry_limit:
            r["state"] = "dropped"
            dropped.append(r)
        else:
            r["state"] = "retx"
            nacked.append(r)


# ======================================================================================== ARQ: receiver side
class RxSession:
    """Recipient side: reorder buffer of N, in-order delivery, duplicate removal."""

    def __init__(self, src, n_win, ws):
        self.src = src
        self.N = n_win
        self.rws = ws
        self.buf = {}

    def on_frame(self, sn, ws_sender, item):
        """Returns (list of items to deliver in order, duplicate?, gave_up count)."""
        out, gave_up = [], 0
        if sdiff(self.rws, ws_sender) > self.N:             # sender restarted (its window is far behind ours)
            self.buf.clear()
            self.rws = ws_sender
        if sdiff(ws_sender, self.rws) > 0:                   # sender moved its window: missing frames are given up
            gave_up += self._flush_to(ws_sender, out)
        d = sdiff(sn, self.rws)
        if d < 0:
            return out, True, gave_up
        if d >= self.N:                                      # beyond the window: move it (802.11 rule)
            gave_up += self._flush_to((sn - self.N + 1) & MASK, out)
        if sn in self.buf:
            return out, True, gave_up
        self.buf[sn] = item
        while self.rws in self.buf:
            out.append(self.buf.pop(self.rws))
            self.rws = (self.rws + 1) & MASK
        return out, False, gave_up

    def _flush_to(self, new_ws, out):
        gave_up = 0
        while sdiff(new_ws, self.rws) > 0:
            it = self.buf.pop(self.rws, None)
            if it is None:
                gave_up += 1
            else:
                out.append(it)
            self.rws = (self.rws + 1) & MASK
        while self.rws in self.buf:
            out.append(self.buf.pop(self.rws))
            self.rws = (self.rws + 1) & MASK
        return gave_up


class TxMuter:
    """Writes the Pluto TX attenuation (ad9361-phy, output voltage0, hardwaregain) from a worker thread."""

    def __init__(self, uri, verbose=1):
        self.uri = str(uri)
        self.verbose = int(verbose)
        self.ok = False
        self.failed = False
        self.reason = "connecting"
        self.backend = ""
        self.errors = 0
        self.writes = 0
        self.write_ms = collections.deque(maxlen=200)
        self._cv = threading.Condition()
        self._want = None
        self._seq = 0
        self._done_seq = 0
        self._stop = False
        self._last_write = 0.0
        self._write = None
        self._close = None
        self._thr = None

    def start(self):
        self._thr = threading.Thread(target=self._run, name="tx-mute", daemon=True)
        self._thr.start()

    def request(self, att_db):
        with self._cv:
            self._want = float(att_db)
            self._seq += 1
            self._cv.notify()
            return self._seq

    def done(self, seq):
        return self._done_seq >= seq

    def stop(self, restore_att=None):
        with self._cv:
            self._stop = True
            self._cv.notify()
        if self._thr is not None:
            self._thr.join(timeout=2.0)
        if self.ok and restore_att is not None:
            try:
                self._write(float(restore_att))
            except Exception:
                pass
        if self._close:
            try:
                self._close()
            except Exception:
                pass

    def _open(self):
        errs = []
        try:
            import ctypes
            import ctypes.util
            name = ctypes.util.find_library("iio") or "libiio.so.0"
            lib = ctypes.CDLL(name)
            vp, cp = ctypes.c_void_p, ctypes.c_char_p
            lib.iio_create_context_from_uri.restype = vp
            lib.iio_create_context_from_uri.argtypes = [cp]
            lib.iio_context_find_device.restype = vp
            lib.iio_context_find_device.argtypes = [vp, cp]
            lib.iio_device_find_channel.restype = vp
            lib.iio_device_find_channel.argtypes = [vp, cp, ctypes.c_bool]
            lib.iio_channel_attr_write.restype = ctypes.c_ssize_t
            lib.iio_channel_attr_write.argtypes = [vp, cp, cp]
            lib.iio_context_set_timeout.restype = ctypes.c_int
            lib.iio_context_set_timeout.argtypes = [vp, ctypes.c_uint]
            lib.iio_context_destroy.restype = None
            lib.iio_context_destroy.argtypes = [vp]
            ctx = lib.iio_create_context_from_uri(self.uri.encode())
            if not ctx:
                raise RuntimeError(f"cannot open {self.uri}")
            lib.iio_context_set_timeout(ctx, 1000)
            dev = lib.iio_context_find_device(ctx, b"ad9361-phy")
            chn = lib.iio_device_find_channel(dev, b"voltage0", True) if dev else None
            if not chn:
                lib.iio_context_destroy(ctx)
                raise RuntimeError("ad9361-phy output voltage0 not found")

            def write(att):
                r = lib.iio_channel_attr_write(chn, b"hardwaregain", f"{-att:.2f}".encode())
                if r < 0:
                    raise OSError(-r, os.strerror(-r))

            self._write, self._close = write, (lambda: lib.iio_context_destroy(ctx))
            self.backend = f"libiio C library ({name})"
            return
        except Exception as e:
            errs.append(f"libiio C library: {e}")
        try:
            import iio
            ctx = iio.Context(self.uri)
            dev = ctx.find_device("ad9361-phy")
            chn = dev.find_channel("voltage0", True) if dev else None
            if chn is None:
                raise RuntimeError("ad9361-phy output voltage0 not found")
            attr = chn.attrs["hardwaregain"]

            def write(att):
                attr.value = f"{-att:.2f}"

            self._write, self._close = write, None
            self._ctx_keepalive = ctx
            self.backend = "python iio module"
            return
        except Exception as e:
            errs.append(f"python iio module: {e}")
        raise RuntimeError("; ".join(errs))

    def _run(self):
        try:
            self._open()
        except Exception as e:
            self.failed, self.reason = True, str(e)
            print(f"[MAC] TX mute OFF: could not control the radio's TX attenuation ({e}). Install libiio "
                  f"(Fedora: sudo dnf install libiio python3-libiio) or check iio_uri. Running without mute.", flush=True)
            return
        self.ok, self.reason = True, "ok"
        if self.verbose:
            print(f"[MAC] TX mute ready ({self.backend}, {self.uri})", flush=True)
        while True:
            with self._cv:
                while (not self._stop and self._seq == self._done_seq
                       and time.monotonic() - self._last_write < 1.0):
                    self._cv.wait(timeout=0.25)
                if self._stop:
                    return
                seq, att = self._seq, self._want
            if att is not None:
                t = time.perf_counter()
                try:
                    self._write(att)
                    self.writes += 1
                    self.write_ms.append(1e3 * (time.perf_counter() - t))
                except Exception as e:
                    self.errors += 1
                    if self.errors <= 3:
                        print(f"[MAC] TX attenuation write failed: {e}", flush=True)
            self._last_write = time.monotonic()
            with self._cv:
                self._done_seq = max(self._done_seq, seq)


# ======================================================================================== the block
class SrArqMac(gr.sync_block):
    def __init__(self, samp_rate=1e6, samples_per_byte=32, my_id=1, dest_id=255,
                 burst_m=4, window_n=16, txop_limit_s=0.25,
                 p_persist=0.04, slot_s=0.002, sifs_s=0.001, turnaround_s=0.025, difs_s=0.0,
                 cw_min=15, cw_max=1023, k_max=5, retry_limit=7, ba_timeout_s=0.070, max_queue=256, udp_queue=32,
                 phy_preamble_len=128, phy_fec=31, phy_buffer_size=4096, phy_max_payload=2048,
                 cs_margin_db=6.0, cs_hyst_db=2.0, cs_bw_hz=400e3, dc_exclude_hz=10e3, nfft=256, sense_s=0.001,
                 floor_window_s=2.0, floor_percentile=10.0, floor_rise_db_s=1.0,
                 blank_rx=1, tx_delay_s=0.012, auto_delay=1, gate_guard_s=0.005,
                 tx_mute=1, iio_uri="ip:192.168.1.10", tx_att_db=20.0, mute_att_db=89.75,
                 unmute_timeout_s=0.1, echo_margin_db=25.0, verbose=1, stats_every_s=10.0,
                 first_pre_len=-1, dup_after=-1, window_ba=-1, pre_in=-1):
        gr.sync_block.__init__(self, name="CSMA/CA DCF + SR-ARQ (TXOP burst, Block ACK) + TX mute",
                               in_sig=[np.complex64], out_sig=[np.complex64])
        self.samp_rate = float(samp_rate)
        self.samples_per_byte = float(samples_per_byte)
        self.my_id = int(my_id) & 0xFF
        self._dest_id = int(dest_id) & 0xFF
        self.burst_m = max(1, min(64, int(burst_m)))
        self.window_n = max(self.burst_m, min(1024, int(window_n)))
        self.txop_limit_s = float(txop_limit_s)
        self.p_persist = float(p_persist)
        self.slot_s = float(slot_s)
        self.sifs_s = float(sifs_s)
        self.turnaround_s = float(turnaround_s)
        self.difs_s = float(difs_s)
        self.cw_min = max(1, int(cw_min))
        self.cw_max = max(self.cw_min, int(cw_max))
        self.k_max = max(0, int(k_max))
        self.retry_limit = max(0, int(retry_limit))
        self.ba_timeout_s = float(ba_timeout_s)
        self.max_queue = max(1, int(max_queue))
        self.udp_queue = max(1, int(udp_queue))
        self.phy_preamble_len = int(phy_preamble_len)
        self.phy_fec = int(phy_fec)
        self.phy_buffer_size = int(phy_buffer_size)
        self.phy_max_payload = int(phy_max_payload)
        self.cs_margin_db = float(cs_margin_db)
        self.cs_hyst_db = float(cs_hyst_db)
        self.floor_percentile = float(floor_percentile)
        self.floor_rise_db_s = float(floor_rise_db_s)
        self.blank_rx = int(blank_rx)
        self.tx_delay_s = float(tx_delay_s)
        self.auto_delay = int(auto_delay)
        self.gate_guard_s = float(gate_guard_s)
        self.tx_mute = int(tx_mute)
        self.iio_uri = str(iio_uri)
        self.tx_att_db = float(tx_att_db)
        self.mute_att_db = float(mute_att_db)
        self.unmute_timeout_s = float(unmute_timeout_s)
        self.echo_margin_db = float(echo_margin_db)
        self.verbose = int(verbose)
        self.stats_every_s = float(stats_every_s)
        self._sense_s = float(sense_s)
        # v6.9.2: -1 (or empty) = take the value from the environment (CDP_MAC_PRE1 / _DUP_AFTER / _WBA)
        def pick(v, key, default):
            try:
                v = float(v)
            except (TypeError, ValueError):
                v = -1.0
            if v < 0:
                try:
                    v = float(os.environ.get(key, "").strip() or default)
                except ValueError:
                    v = float(default)
            return int(v)
        self.first_pre_len = pick(first_pre_len, "CDP_MAC_PRE1", 256)
        self.dup_after = max(0, pick(dup_after, "CDP_MAC_DUP_AFTER", 4))
        self.window_ba = 1 if pick(window_ba, "CDP_MAC_WBA", 1) else 0
        self._pre1 = self.first_pre_len if self.first_pre_len > self.phy_preamble_len else None
        self.pre_in = pick(pre_in, "CDP_MAC_PRE_IN", 64)                    # v6.9.3
        self._pre_in = self.pre_in if 0 <= self.pre_in < self.phy_preamble_len else None
        if self.window_n >= 0x8000:
            raise ValueError("window_n must be < 32768")

        # ---- carrier-sense front end (v6.5)
        self._nfft = int(nfft)
        self._dt = self._nfft / self.samp_rate
        f = np.fft.fftfreq(self._nfft, 1.0 / self.samp_rate)
        sel = (np.abs(f) <= float(cs_bw_hz) / 2) & (np.abs(f) >= float(dc_exclude_hz))
        self._bins = np.flatnonzero(sel)
        if self._bins.size < 8:
            raise ValueError("cs_bw_hz / dc_exclude_hz / nfft leave fewer than 8 bins to sense")
        self._win = np.hanning(self._nfft).astype(np.float32)
        self._norm = 1.0 / (np.log(2.0) * self._nfft * float(np.mean(self._win ** 2)))
        self._smooth = collections.deque(maxlen=max(1, int(round(self._sense_s / self._dt))))
        self._cs_fast = None                    # C++ carrier sense front end (set in start)
        self._carry = np.zeros(0, np.complex64)
        self._cs_db = -200.0
        self._hist = collections.deque(maxlen=max(64, int(float(floor_window_s) / self._dt)))
        self._floor_db = None
        self._floor_next = 0.0
        self._busy = True

        # ---- MAC / ARQ state
        self._lock = threading.RLock()
        self._tx_s = {}                         # dst -> TxSession
        self._rx_s = {}                         # src -> RxSession
        self._uq = {}                           # dst -> deque of unreliable frames (UDP, broadcast; no ARQ)
        self._u_sn = {}                         # dst -> next unreliable sequence number
        self._u_seen = {}                       # src -> deque of recent unreliable SNs (duplicate filter)
        self._txr = None                        # summary of the current channel access (-> 'txop' event)
        self._rr = 0                            # round robin over destinations
        self._bsn = 0
        self._next_id = 0
        self._byid = {}                         # mac_id -> frame entry waiting for the framer
        self._rx_ev = collections.deque()
        self._arq_inbox = collections.deque()   # Block ACK results for the ARQ layer (read after the MAC)
        self._ba_due = []                       # Block ACKs this node owes: dicts
        self._ba_sent = collections.deque(maxlen=64)   # (src, bsn) already answered
        self._k = 0                             # backoff stage (failed TXOPs in a row)
        self._await = None                      # burst waiting for its Block ACK
        self._last_failed = None                # burst whose Block ACK timed out (a late one is still accepted)
        self._nav_until = 0.0
        self._rng = np.random.default_rng()
        self._reset_contention()
        self._n_abs = 0
        self._t = 0.0
        self._self_tx = False
        self._tx = {}
        self._muter = None
        self._req_att = None
        self._mute_seq = 0
        self._seen_ctl = (None, None)
        self._warned_own = False

        # ---- statistics
        self._st = collections.Counter()
        self._echo_delays = collections.deque(maxlen=200)
        self._delay = collections.deque(maxlen=1000)     # enqueue -> acknowledged
        self._rtt = collections.deque(maxlen=500)
        self._bsizes = collections.Counter()
        self._busy_time = 0.0
        self._nav_time = 0.0
        self._stat_t0 = 0.0
        self._wall0 = None
        self._n_wall0 = 0
        self._cpu = 0.0
        self._windows = 0
        self._dbg = collections.Counter()
        self._dbg_t0 = 0.0
        self._dbg_w0 = None
        self._dbg_n0 = 0
        self._prev_busy = None
        self._busy_since = 0.0
        self._busy_max = -200.0
        self._tr_sec = -1
        self._tr_cnt = 0

        for p in ("app_in", "phy_in", "rx_in", "ctrl"):
            self.message_port_register_in(pmt.intern(p))
        self.set_msg_handler(pmt.intern("app_in"), self._on_app)
        self.set_msg_handler(pmt.intern("phy_in"), self._on_phy)
        self.set_msg_handler(pmt.intern("rx_in"), self._on_rx)
        self.set_msg_handler(pmt.intern("ctrl"), self._on_ctrl)
        for p in ("to_phy", "pdu_out", "app_out", "tx_status"):
            self.message_port_register_out(pmt.intern(p))

    # ------------------------------------------------------------------ parameters / derived values
    @property
    def dest_id(self):
        return self._dest_id

    @dest_id.setter
    def dest_id(self, v):
        try:
            v = int(float(v))
        except (TypeError, ValueError):
            return
        if 0 <= v <= 255 and v != self._dest_id:
            self._dest_id = v
            print(f"[MAC] default destination is now ID {id_name(v)}", flush=True)

    def difs(self):
        return self.difs_s if self.difs_s > 0 else self.sifs_s + self.turnaround_s + 2.0 * self.slot_s

    def _cw_for(self, k):
        return min(self.cw_max, (self.cw_min + 1) * (2 ** min(k, self.k_max)) - 1)

    def phy_raw(self, plen, pre=None):
        """Bytes of a frame without its postamble: preamble + train + sync + coded header and body"""
        if self.phy_fec in (15, 31):
            k = {15: 11, 31: 26}[self.phy_fec]
            bits = math.ceil(56 / k) * self.phy_fec + math.ceil((plen + 4) * 8 / k) * self.phy_fec
            body = math.ceil(bits / 8)
        else:
            body = 7 + plen + 4
        return (self.phy_preamble_len if pre is None else pre) + 4 + 4 + body

    def _unit(self):
        return max(1, math.ceil(self.phy_buffer_size / self.samples_per_byte))     # bytes per Pluto sink buffer

    def _burst_post(self, raw_total):
        """postamble after the last frame of a burst: at least one sink buffer, burst = whole number of buffers"""
        if self.phy_buffer_size <= 0:
            return 0
        u = self._unit()
        return math.ceil((raw_total + u) / u) * u - raw_total

    def phy_bytes(self, plen, pre=None, post=None):
        """Bytes on air for a framer payload of plen bytes (same formula as the Packet Framer TX); pre = preamble
        length of this frame if it is not phy_preamble_len, post = its postamble if the MAC sets it."""
        if post is not None:
            return self.phy_raw(plen, pre) + post
        if self.phy_fec in (15, 31):
            k = {15: 11, 31: 26}[self.phy_fec]
            bits = math.ceil(56 / k) * self.phy_fec + math.ceil((plen + 4) * 8 / k) * self.phy_fec
            body = math.ceil(bits / 8)
        else:
            body = 7 + plen + 4
        raw = (self.phy_preamble_len if pre is None else pre) + 4 + 4 + body
        if self.phy_buffer_size <= 0:
            return raw
        unit = max(1, math.ceil(self.phy_buffer_size / self.samples_per_byte))
        return math.ceil((raw + unit) / unit) * unit

    def air_s(self, plen, pre=None, post=None):
        return self.phy_bytes(plen, pre, post) * self.samples_per_byte / self.samp_rate

    def _wba_bits(self):
        return 8 * ((self.window_n + 7) // 8)

    def ba_air(self):
        n = self._wba_bits() // 8 if self.window_ba else (self.burst_m + 7) // 8
        return self.air_s(HDR_BA + n, self._pre1)        # a Block ACK is always the first frame of its transmission

    # ------------------------------------------------------------------ start / stop
    def start(self):
        F = _load_fast()
        if F is not None:
            cs = F.CsFront(self._nfft, self._win, self._bins, self._norm)
            self._cs_fast = cs if cs.ok else None
        print(F.status("MAC carrier sense") if F is not None else "[FAST] MAC carrier sense: original Python code",
              flush=True)
        self._muter = TxMuter(self.iio_uri, self.verbose)
        self._muter.start()
        _bus().sources["csma"] = self
        _emit("csma", "config", mac="srarq", mac_version="6.9", udp_queue=self.udp_queue, samp_rate=self.samp_rate, samples_per_byte=self.samples_per_byte,
              my_id=self.my_id, dest_id=self._dest_id, burst_m=self.burst_m, window_n=self.window_n,
              txop_limit_s=self.txop_limit_s, p_persist=self.p_persist, slot_s=self.slot_s, sifs_s=self.sifs_s,
              turnaround_s=self.turnaround_s, difs_s=self.difs(), cw_min=self.cw_min, cw_max=self.cw_max,
              k_max=self.k_max, retry_limit=self.retry_limit, ba_timeout_s=self.ba_timeout_s,
              ack_timeout_s=self.ba_timeout_s, max_queue=self.max_queue, ba_air_ms=round(1e3 * self.ba_air(), 2),
              phy_preamble_len=self.phy_preamble_len, phy_fec=self.phy_fec, phy_buffer_size=self.phy_buffer_size,
              cs_margin_db=self.cs_margin_db, cs_hyst_db=self.cs_hyst_db, nfft=self._nfft, sense_s=self._sense_s,
              floor_percentile=self.floor_percentile, blank_rx=self.blank_rx, tx_delay_s=self.tx_delay_s,
              auto_delay=self.auto_delay, gate_guard_s=self.gate_guard_s, tx_mute=self.tx_mute,
              iio_uri=self.iio_uri, tx_att_db=self.tx_att_db, mute_att_db=self.mute_att_db,
              unmute_timeout_s=self.unmute_timeout_s, echo_margin_db=self.echo_margin_db, gate_mode=1)
        cws = ", ".join(str(self._cw_for(k)) for k in range(self.k_max + 1))
        print(f"[MAC] ID {self.my_id}: SR-ARQ burst M={self.burst_m}, window N={self.window_n}, TXOP limit "
              f"{1e3 * self.txop_limit_s:g} ms | p-persist {self.p_persist:g}, slot {1e3 * self.slot_s:g} ms, SIFS "
              f"{1e3 * self.sifs_s:g} ms, DIFS {1e3 * self.difs():.1f} ms, CW {cws}, retry limit {self.retry_limit}, "
              f"Block ACK timeout {1e3 * self.ba_timeout_s:g} ms (Block ACK {1e3 * self.ba_air():.1f} ms on air)",
              flush=True)
        print(f"[MAC] v6.9.2: first frame of a transmission with "
              f"{f'a {self._pre1} B preamble' if self._pre1 else 'the normal preamble'}, "
              f"{f'a duplicate after {self.dup_after} retries' if self.dup_after else 'no duplicates'}, "
              f"Block ACK {'with the window bitmap' if self.window_ba else 'per burst (v6.9 format)'}", flush=True)
        print(f"[MAC] v6.9.3: frames 2..{self.burst_m} of a burst "
              f"{f'with a {self._pre_in} B preamble and no postamble' if self._pre_in is not None else 'unchanged'}"
              f" | {self.samples_per_byte:g} samples per byte, preamble {self.phy_preamble_len} B", flush=True)
        _emit("csma", "config_v692", first_pre_len=self._pre1 or self.phy_preamble_len, dup_after=self.dup_after,
              window_ba=self.window_ba)
        return True

    def stop(self):
        if self._muter is not None:
            self._muter.stop(restore_att=self.tx_att_db)
        return True

    # ------------------------------------------------------------------ read-outs (Function Probe)
    def cs_db(self):
        return float(self._cs_db)

    def floor_db(self):
        return float(self._floor_db) if self._floor_db is not None else -200.0

    def threshold_db(self):
        return self.floor_db() + self.cs_margin_db

    def is_busy(self):
        return int(self._busy or self._self_tx or self._t < self._nav_until)

    def is_tx(self):
        return int(self._self_tx)

    def queue_len(self):
        return (sum(s.outstanding() for s in list(self._tx_s.values()))
                + sum(len(q) for q in list(self._uq.values())))

    def is_muted(self):
        return int(self._mute_on() and self._req_att == self.mute_att_db)

    def nav_ms(self):
        return max(0.0, 1e3 * (self._nav_until - self._t))

    def cw(self):
        return self._cw_for(self._k)

    def window_info(self):
        return "; ".join(f"to {d}: WS {s.ws}, in window {sdiff(s.next_sn, s.ws)}/{self.window_n}, backlog "
                         f"{len(s.backlog)}" for d, s in list(self._tx_s.items())) or "idle"

    # ------------------------------------------------------------------ debug bus
    def dbg_snapshot(self):
        d, self._dbg = self._dbg, collections.Counter()
        now_w, n = _time.time(), self._n_abs
        span = max(1e-9, self._t - self._dbg_t0)
        rate = None
        if self._dbg_w0 is not None:
            rate = round((n - self._dbg_n0) / max(1e-9, now_w - self._dbg_w0) / self.samp_rate * 100.0, 2)
        self._dbg_w0, self._dbg_n0, self._dbg_t0 = now_w, n, self._t
        m = self._muter
        ed = list(self._echo_delays)
        out = {"ts": round(self._t, 6), "cs_db": round(self._cs_db, 2),
               "floor_db": round(self._floor_db, 2) if self._floor_db is not None else None,
               "thr_db": round(self.threshold_db(), 2), "busy_pct": round(100.0 * d.pop("busy_s", 0.0) / span, 2),
               "nav_pct": round(100.0 * d.pop("nav_s", 0.0) / span, 2),
               "self_tx_pct": round(100.0 * d.pop("tx_s", 0.0) / span, 2), "queue": self.queue_len(),
               "ba_due": len(self._ba_due), "cw": self.cw(), "k": self._k,
               "phase": self._ph if self._has_work() else "idle", "awaiting_ba": int(self._await is not None),
               "windows": {str(dd): {"ws": s.ws, "used": sdiff(s.next_sn, s.ws), "backlog": len(s.backlog)}
                           for dd, s in list(self._tx_s.items())},
               "rx_windows": {str(ss): {"rws": r.rws, "buffered": len(r.buf)} for ss, r in list(self._rx_s.items())},
               "udp_q": {str(dd): len(q) for dd, q in list(self._uq.items()) if q},
               "busy": int(self._busy), "self_tx": int(self._self_tx), "rate_pct": rate,
               "tx_delay_ms": round(1e3 * self._tx_delay(), 2),
               "echo_med_ms": round(1e3 * float(np.median(ed)), 2) if ed else None,
               "mute": ("off" if m is None or m.failed else ("connecting" if not m.ok else
                        ("on" if self.tx_mute else "disabled"))),
               "req_att": self._req_att, "mute_writes": m.writes if m else 0, "mute_errors": m.errors if m else 0,
               "mute_write_ms_max": round(max(m.write_ms), 2) if m and m.write_ms else None}
        out.update({k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()})
        return out

    def _ev(self, _k, /, **kw):
        kw["ts"] = round(self._t, 6)
        _emit("csma", _k, **kw)

    @staticmethod
    def _keep_py(keep):
        return {k: pmt.to_python(v) for k, v in keep.items()}

    def _keep_list(self, recs):
        """stress test metadata of several frames, as lists (for the logs)"""
        out = collections.defaultdict(list)
        for r in recs:
            for k, v in r.get("keep", {}).items():
                out[k + "s"].append(pmt.to_python(v))
        return dict(out)

    def _status(self, keep, status, dst, r=None):
        """Delivery report for frames the app tagged with app_ref (the socket bridge uses it for SENT / FAIL)."""
        if "app_ref" not in keep:
            return
        d = pmt.make_dict()
        d = pmt.dict_add(d, pmt.intern("app_ref"), keep["app_ref"])
        d = pmt.dict_add(d, pmt.intern("app_seg"), keep.get("app_seg", pmt.from_long(0)))
        d = pmt.dict_add(d, pmt.intern("status"), pmt.intern(status))
        d = pmt.dict_add(d, pmt.intern("dst"), pmt.from_long(int(dst)))
        if r is not None:
            d = pmt.dict_add(d, pmt.intern("tx"), pmt.from_long(int(r.get("tx", 0))))
            d = pmt.dict_add(d, pmt.intern("ms"), pmt.from_double(round(1e3 * (self._t - r["t_enq"]), 1)))
        self.message_port_pub(pmt.intern("tx_status"), d)

    # ------------------------------------------------------------------ TX mute helpers (v6.5)
    def _mute_on(self):
        return bool(self.tx_mute) and self._muter is not None and self._muter.ok

    def _desired_att(self):
        if self._muter is None or not self._muter.ok:
            return None
        if not self.tx_mute:
            return self.tx_att_db
        if self._self_tx and self._tx.get("phase") in ("prep", "air"):
            return self.tx_att_db
        return self.mute_att_db

    def _sync_mute(self, force=False):
        att = self._desired_att()
        if att is not None and (force or att != self._req_att):
            self._mute_seq = self._muter.request(att)
            self._req_att = att
        return self._mute_seq

    # ------------------------------------------------------------------ data from the application
    @staticmethod
    def _extract(msg):
        meta_dst, keep = None, {}
        if pmt.is_pair(msg):
            meta, data = pmt.car(msg), pmt.cdr(msg)
            if pmt.is_dict(meta):
                v = pmt.dict_ref(meta, pmt.intern("dest_id"), pmt.PMT_NIL)
                if pmt.is_null(v):
                    v = pmt.dict_ref(meta, pmt.intern("dst"), pmt.PMT_NIL)
                if pmt.is_integer(v):
                    meta_dst = pmt.to_long(v) & 0xFF
                for key in ("stress_seq", "stress_type", "app_ref", "app_seg", "noack"):
                    w = pmt.dict_ref(meta, pmt.intern(key), pmt.PMT_NIL)
                    if not pmt.is_null(w):
                        keep[key] = w
            if pmt.is_u8vector(data):
                return bytes(pmt.u8vector_elements(data)), meta_dst, False, keep
            return str(pmt.to_python(data)).encode("utf-8"), meta_dst, True, keep
        if pmt.is_symbol(msg):
            return pmt.symbol_to_string(msg).encode("utf-8"), None, True, keep
        val = pmt.to_python(msg)
        if isinstance(val, (bytes, bytearray)):
            return bytes(val), None, False, keep
        return str(val).encode("utf-8"), None, True, keep

    @staticmethod
    def _parse_at(payload):
        if not payload.startswith(b"@"):
            return None, payload
        head, _, rest = payload[1:].partition(b" ")
        h = head.decode("ascii", "replace").lower()
        if h in ("all", "*"):
            return BCAST, rest
        if h.isdigit() and 0 <= int(h) <= 255:
            return int(h), rest
        return None, payload

    def _on_app(self, msg):
        try:
            payload, dst, is_text, keep = self._extract(msg)
        except Exception as e:
            print(f"[MAC] could not parse the message: {e}", flush=True)
            return
        if dst is None and is_text:
            dst, payload = self._parse_at(payload)
        if dst is None:
            dst = self._dest_id
        if not payload:
            return
        if dst == self.my_id:
            print(f"[MAC] message addressed to this node (ID {dst}) not sent", flush=True)
            return
        room = self.phy_max_payload - HDR_DATA
        if len(payload) > room:
            print(f"[MAC] payload {len(payload)} B truncated to {room} B (framer max_payload_len - MAC header)",
                  flush=True)
            payload = payload[:room]
        noack = False
        if "noack" in keep:
            try:
                noack = bool(pmt.to_long(keep.pop("noack")))
            except Exception:
                noack = False
        with self._lock:
            if self.queue_len() >= self.max_queue:
                self._st["qdrop"] += 1
                self._dbg["dropped"] += 1
                self._status(keep, "qfull", dst)
                self._ev("drop", dst=dst, q=self.queue_len(), **self._keep_py(keep))
                if self._st["qdrop"] <= 5 or self._st["qdrop"] % 50 == 0:
                    print(f"[MAC] queue full ({self.max_queue}): new frame dropped ({self._st['qdrop']} so far)",
                          flush=True)
                return
            if dst == BCAST or noack:
                q = self._uq.setdefault(dst, collections.deque())
                if len(q) >= self.udp_queue:            # like a full UDP socket buffer: the oldest goes
                    old = q.popleft()
                    self._st["udp_drop"] += 1
                    self._ev("udp_drop", dst=dst, q=len(q), age_ms=round(1e3 * (self._t - old["t_enq"]), 2),
                             **self._keep_py(old["keep"]))
                sn = self._u_sn.get(dst, 0)
                self._u_sn[dst] = (sn + 1) & MASK
                q.append({"payload": payload, "keep": keep, "t_enq": self._t, "sn": sn})
                where = f"{'broadcast' if dst == BCAST else 'unreliable'} queue {len(q)}"
            else:
                s = self._tx_s.get(dst)
                if s is None:
                    s = self._tx_s[dst] = TxSession(dst, self.window_n, self.retry_limit)
                s.backlog.append((payload, keep, self._t))
                s.admit()
                where = f"window {sdiff(s.next_sn, s.ws)}/{self.window_n}, backlog {len(s.backlog)}"
        self._dbg["enq"] += 1
        self._ev("enq", dst=dst, nbytes=len(payload), q=self.queue_len(), busy=int(self._busy),
                 **self._keep_py(keep))
        if self.verbose and not payload.startswith(b"CDPST,D,"):
            shown = (payload.split(b"|")[0].decode("ascii", "replace") if payload.startswith(b"CDPST,") else
                     payload.decode("utf-8", "replace") if is_text else f"{len(payload)} B binary")
            print(f"[MAC] queued -> ID {id_name(dst)}: '{shown}' ({where})", flush=True)

    # ------------------------------------------------------------------ framer round trip
    def _new_id(self):
        self._next_id += 1
        return self._next_id

    def _frame(self, e, data, dst, keep):
        meta = pmt.make_dict()
        meta = pmt.dict_add(meta, pmt.intern("dest_id"), pmt.from_long(int(dst)))
        meta = pmt.dict_add(meta, pmt.intern("mac_id"), pmt.from_long(int(e["id"])))
        meta = pmt.dict_add(meta, pmt.intern("mac_type"), pmt.intern(e["kind"]))
        meta = pmt.dict_add(meta, pmt.intern("mac_seq"), pmt.from_long(int(e.get("sn", 0))))
        if e.get("pre") is not None:
            meta = pmt.dict_add(meta, pmt.intern("pre_len"), pmt.from_long(int(e["pre"])))
        if e.get("post") is not None:
            meta = pmt.dict_add(meta, pmt.intern("post_len"), pmt.from_long(int(e["post"])))
        for k, v in keep.items():
            meta = pmt.dict_add(meta, pmt.intern(k), v)
        with self._lock:
            e["coded"] = None
            self._byid[e["id"]] = e
        self.message_port_pub(pmt.intern("to_phy"), pmt.cons(meta, pmt.init_u8vector(len(data), list(data))))

    def _on_phy(self, msg):
        if not pmt.is_pair(msg) or not pmt.is_u8vector(pmt.cdr(msg)):
            return
        meta = pmt.car(msg)
        v = pmt.dict_ref(meta, pmt.intern("mac_id"), pmt.PMT_NIL) if pmt.is_dict(meta) else pmt.PMT_NIL
        if not pmt.is_integer(v):
            print("[MAC] phy_in: frame without mac_id ignored (wire the framer's pdu_out only to phy_in)", flush=True)
            return
        with self._lock:
            e = self._byid.pop(pmt.to_long(v), None)
            if e is None:
                return
            e["coded"] = msg
            e["nbytes"] = int(pmt.length(pmt.cdr(msg)))
            if e["kind"] == "D" and e.get("plen") is not None and self.phy_bytes(e["plen"], e.get("pre"), e.get("post")) != e["nbytes"]:
                self._st["len_mismatch"] += 1

    def _on_rx(self, msg):
        with self._lock:
            self._rx_ev.append(msg)

    def _on_ctrl(self, msg):
        try:
            v = pmt.to_python(pmt.car(msg) if pmt.is_pair(msg) else msg)
        except Exception:
            v = ""
        if isinstance(v, dict) and "cancel_ref" in v:          # socket bridge: this message failed, stop sending it
            self._cancel(int(v["cancel_ref"]))
            return
        txt = str(v).lower()
        if "flush" in txt:
            with self._lock:
                for s in self._tx_s.values():
                    s.backlog.clear()
                    for r in s.frames.values():
                        if r["state"] in ("new", "retx"):
                            r["state"] = "dropped"
                    s.slide()
                self._uq.clear()
            print("[MAC] queues flushed", flush=True)

    def _cancel(self, ref):
        """Remove every frame of app message `ref` that is not on air yet (backlog and window); each one is
        reported as 'dropped' on tx_status so the bridge's in-flight count stays right."""
        rep = []
        is_ref = lambda k: "app_ref" in k and pmt.to_long(k["app_ref"]) == ref
        with self._lock:
            for d, s in self._tx_s.items():
                kept = collections.deque()
                for item in s.backlog:
                    if is_ref(item[1]):
                        rep.append((item[1], d, None))
                    else:
                        kept.append(item)
                s.backlog = kept
                for r in s.frames.values():
                    if r["state"] in ("new", "retx") and is_ref(r["keep"]):
                        r["state"] = "dropped"
                        rep.append((r["keep"], d, r))
                s.slide()
                s.admit()
        for k, d, r in rep:
            self._status(k, "dropped", d, r)
        if rep:
            self._ev("cancel", ref=ref, frames=len(rep))
            if self.verbose:
                print(f"[MAC] message {ref}: {len(rep)} queued frames cancelled (the bridge gave it up)", flush=True)

    # ------------------------------------------------------------------ receiving (sample-clock thread)
    def _process_rx(self, msg):
        if not pmt.is_pair(msg) or not pmt.is_u8vector(pmt.cdr(msg)):
            return
        meta = pmt.car(msg)
        md = {}
        try:
            if pmt.is_dict(meta):
                md = pmt.to_python(meta)
        except Exception:
            md = {}
        src, dst = int(md.get("src_id", -1)), int(md.get("dest_id", -1))
        raw = bytes(pmt.u8vector_elements(pmt.cdr(msg)))
        if src == self.my_id:
            self._st["own_rx"] += 1
            if not self._warned_own:
                self._warned_own = True
                print(f"[MAC] WARNING: received a frame with this node's own ID {src}: another PC uses the same "
                      f"CDP_MY_ID, or this node's own burst leaked past the RX blanking (raise gate_guard_s).",
                      flush=True)
            return
        h = parse_mac(raw)
        if h is None:
            if dst in (self.my_id, BCAST):
                self._deliver(src, dst, None, raw, md)
            return
        if dst != self.my_id and dst != BCAST:              # overheard: virtual carrier sense
            if h["dur"] > 0:
                until = self._t + h["dur"]
                if until > self._nav_until:
                    self._nav_until = until
                self._st["nav_set"] += 1
                self._dbg["nav_set"] += 1
                self._ev("nav", src=src, dst=dst, typ=h["type"], dur_ms=round(1e3 * h["dur"], 2))
            return
        if h["type"] in (T_BA, T_WBA):
            if dst == self.my_id:
                self._got_ba(src, h)
            return
        body = raw[HDR_DATA:]
        if dst == BCAST or h.get("noack"):                 # unreliable: deliver at once, no reorder, no Block ACK
            seen = self._u_seen.setdefault((src, dst == BCAST), collections.deque(maxlen=64))
            dup = h["sn"] in seen
            if not dup:
                seen.append(h["sn"])
                self._deliver(src, dst, h, body, md)
            self._st["rx_udp_dup" if dup else "rx_udp"] += 1
            self._ev("rx_data", src=src, dst=dst, seq=h["sn"], bsn=h["bsn"], idx=h["idx"], cnt=h["cnt"],
                     bcast=int(dst == BCAST), noack=1, dup=int(dup), fec=md.get("fec_corrected"))
            return
        # unicast DATA for this node: reorder buffer + Block ACK scoreboard
        rs = self._rx_s.get(src)
        if rs is None:
            rs = self._rx_s[src] = RxSession(src, self.window_n, h["ws"])
        rws0 = rs.rws
        out, dup, gave_up = rs.on_frame(h["sn"], h["ws"], (h, body, md))
        self._st["rx_dup" if dup else "rx_data"] += 1
        self._dbg["rx_dup" if dup else "rx_data"] += 1
        if gave_up:
            self._st["rx_gave_up"] += gave_up
        for (hh, bb, mm) in out:
            self._deliver(src, dst, hh, bb, mm)
        self._ev("rx_data", src=src, seq=h["sn"], bsn=h["bsn"], idx=h["idx"], cnt=h["cnt"], ws=h["ws"],
                 rws_before=rws0, rws=rs.rws, dup=int(dup), delivered=len(out), gave_up=gave_up,
                 buffered=len(rs.buf), retry=int(h["retry"]), fec=md.get("fec_corrected"))
        key = (src, h["bsn"])
        if key in self._ba_sent:
            self._st["late_frame"] += 1
            return
        ba = next((b for b in self._ba_due if b["key"] == key), None)
        if ba is None:
            ba = {"key": key, "src": src, "bsn": h["bsn"], "cnt": h["cnt"], "bits": 0, "kind": "BA",
                  "id": self._new_id(), "t_first": self._t, "framed": False, "coded": None, "nbytes": 0,
                  "pre": self._pre1}
            self._ba_due.append(ba)
        ba["bits"] |= 1 << h["idx"]
        ba["cnt"] = max(ba["cnt"], h["cnt"])
        ba["earliest"] = self._t + h["rem"] + self.sifs_s     # burst end (as seen from this frame) + SIFS

    def _deliver(self, src, dst, h, body, md):
        meta = pmt.make_dict()
        for k, v in (("src_id", src), ("dest_id", dst), ("mac_seq", h["sn"] if h else -1),
                     ("mac_retry", int(h["retry"]) if h else 0), ("msg_id", int(md.get("msg_id", -1))),
                     ("noack", int(bool(h and (h.get("noack") or dst == BCAST)))),
                     ("fec_corrected", int(md.get("fec_corrected", 0)))):
            meta = pmt.dict_add(meta, pmt.intern(k), pmt.from_long(int(v)))
        self.message_port_pub(pmt.intern("app_out"), pmt.cons(meta, pmt.init_u8vector(len(body), list(body))))
        if self.verbose and not body.startswith(b"CDPST,D,"):
            shown = (body.split(b"|")[0].decode("ascii", "replace") if body.startswith(b"CDPST,") else
                     body.decode("utf-8", "replace"))
            to = "all" if dst == BCAST else f"ID {dst}"
            seq = f"SN {h['sn']}" if h else "no MAC header"
            print(f"\n{'=' * 40}\n[RX RECEIVED] {seq} from ID {src} to {to}: {shown}\n{'=' * 40}\n", flush=True)

    # ------------------------------------------------------------------ Block ACK at the sender: MAC first ...
    @staticmethod
    def _pos_bits(h, pos):
        """Block ACK -> bit i = burst frame i acknowledged (for the logs; also for the window format)"""
        if h["type"] == T_WBA:
            return sum(1 << i for i, r in enumerate(pos) if wba_has(h, r["sn"]))
        return h["bits"] & ((1 << len(pos)) - 1)

    def _got_ba(self, src, h):
        a = self._await
        if a is not None and a["dst"] == src and a["bsn"] == h["bsn"]:
            pb = self._pos_bits(h, a["pos"])
            rtt = self._t - a["air_end"]
            self._rtt.append(rtt)
            self._await = None
            self._k = 0                                      # TXOP succeeded: CW back to cw_min
            self._st["ba_rx"] += 1
            self._dbg["ba_rx"] += 1
            self._ev("ba_rx", bsn=h["bsn"], src=src, rtt_ms=round(1e3 * rtt, 2), rws=h["rws"],
                     bitmap=bitstr(pb, a["n"]), n=a["n"], acked=bin(pb).count("1"), window=int(h["type"] == T_WBA))
            self._arq_inbox.append(("ba", src, a["pos"], h, a["bsn"]))
            self._txop_done("ba", acked=bin(pb).count("1"), rtt_ms=round(1e3 * rtt, 1), bitmap=bitstr(pb, a["n"]))
            self._reset_contention()                         # ... the MAC goes straight back to its other work
            return
        lf = self._last_failed
        if lf is not None and lf["dst"] == src and lf["bsn"] == h["bsn"]:
            self._st["late_ba"] += 1
            self._k = 0
            self._ev("ba_rx", bsn=h["bsn"], src=src, late=1, rws=h["rws"],
                     bitmap=bitstr(self._pos_bits(h, lf["pos"]), lf["n"]), n=lf["n"])
            self._arq_inbox.append(("late_ba", src, lf["pos"], h, lf["bsn"]))
            self._last_failed = None
            return
        self._st["stray_ba"] += 1
        self._ev("ba_stray", bsn=h["bsn"], src=src, rws=h["rws"])
        self._arq_inbox.append(("cum", src, [], h, h["bsn"]))

    # ------------------------------------------------------------------ ... ARQ reads it afterwards
    def _arq_process(self):
        while self._arq_inbox:
            what, dst, pos, h, bsn = self._arq_inbox.popleft()
            s = self._tx_s.get(dst)
            if s is None:
                continue
            with self._lock:
                if what == "noba":
                    acked = []
                    nacked, dropped = s.on_no_ba(pos)
                elif h["type"] == T_WBA:
                    # window Block ACK: late / stray ones only acknowledge (their frames were counted already)
                    acked, nacked, dropped = s.on_window_ack(pos if what == "ba" else [], h)
                elif what == "late_ba":
                    # its frames were already counted as failed: take only the positive acknowledgements
                    bits = h["bits"]
                    hit = {id(r) for i, r in enumerate(pos) if bits >> i & 1}
                    acked = [r for r in _unique(pos) if id(r) in hit and r["state"] not in ("acked", "dropped")]
                    for r in acked:
                        r["state"] = "acked"
                    a2, nacked, dropped = s.on_block_ack([], 0, h["rws"])
                    acked += a2
                elif what == "cum":
                    acked, nacked, dropped = s.on_block_ack([], 0, h["rws"])
                else:
                    acked, nacked, dropped = s.on_block_ack(pos, h["bits"], h["rws"])
                ws0 = s.ws
                moved = s.slide()
                admitted = s.admit()
            for r in acked:
                self._delay.append(self._t - r["t_enq"])
                self._st["acked"] += 1
                self._status(r["keep"], "acked", dst, r)
            for r in dropped:
                self._status(r["keep"], "dropped", dst, r)
            self._st["nacked"] += len(nacked)
            for r in dropped:
                self._st["retry_drop"] += 1
                self._dbg["retry_drop"] += 1
                self._ev("retry_drop", dst=dst, seq=r["sn"], retries=r["retries"] - 1, **self._keep_py(r["keep"]))
                if self.verbose:
                    print(f"[ARQ] SN {r['sn']} -> ID {dst} DROPPED after {r['retries']} attempts", flush=True)
            self._ev("arq", what=what, dst=dst, bsn=bsn, acked=[r["sn"] for r in acked],
                     nacked=[r["sn"] for r in nacked], dropped=[r["sn"] for r in dropped], ws_before=ws0, ws=s.ws,
                     moved=moved, admitted=len(admitted), used=sdiff(s.next_sn, s.ws), backlog=len(s.backlog),
                     **self._keep_list(acked))
            if self.verbose >= 2:
                print(f"[ARQ] -> ID {dst}: {what} burst {bsn}: acked {[r['sn'] for r in acked]}, retransmit "
                      f"{[r['sn'] for r in nacked]}, window {s.ws}..{(s.ws + self.window_n - 1) & MASK}", flush=True)

    # ------------------------------------------------------------------ contention
    def _reset_contention(self):
        self._ph = "persist"
        self._p_first = True
        self._p_acc = 0.0
        self._p_draws = 0
        self._idle_d = 0.0
        self._bo = None
        self._bo0 = None
        self._bo_acc = 0.0
        self._freezes = 0
        self._att_t0 = getattr(self, "_t", 0.0)
        self._att_busy = 0.0
        self._att_nav = 0.0
        self._p_done_t = None

    def _after_own_tx(self):
        if self._ph == "backoff":
            self._ph = "difs"
        self._idle_d = 0.0
        self._p_acc = 0.0
        self._p_first = True

    def _has_work(self):
        with self._lock:
            return any(self._uq.values()) or any(s.pending() for s in self._tx_s.values())

    # ------------------------------------------------------------------ TXOP
    def _start_txop(self):
        """Backoff reached 0: build the burst (ARQ picks the frames), frame it, unmute, then send."""
        with self._lock:
            return self._start_txop_locked()

    def _start_txop_locked(self):
        cands = []
        for d, q in self._uq.items():
            if q:
                cands.append((q[0]["t_enq"], 1, d))
        for d, s in self._tx_s.items():
            el = s.eligible() if s.pending() else []
            if el:
                cands.append((min(r["t_enq"] for r in el), 0, d))
        if not cands:
            return False
        _, unrel, dst = min(cands)                          # oldest waiting frame first
        bsn = self._bsn
        self._bsn = (self._bsn + 1) & MASK
        frames, recs = [], []
        pre1 = self._pre1                                     # v6.9.2: longer preamble on the first frame
        tot = 0.0 if pre1 is None else (pre1 - self.phy_preamble_len) * self.samples_per_byte / self.samp_rate
        if unrel:
            q = self._uq[dst]
            while q and len(frames) < self.burst_m:
                b = q[0]
                plen = HDR_DATA + len(b["payload"])
                a = self.air_s(plen)
                if frames and tot + a > self.txop_limit_s:
                    break
                q.popleft()
                frames.append({"kind": "D", "id": self._new_id(), "sn": b["sn"], "plen": plen, "rec": None,
                               "payload": b["payload"], "keep": b["keep"], "air": a, "t_enq": b["t_enq"]})
                tot += a
        else:
            s = self._tx_s[dst]
            dups = []                                         # v6.9.2: frames that go twice
            for r in s.eligible():
                if len(recs) + len(dups) >= self.burst_m:
                    break
                a = self.air_s(HDR_DATA + len(r["payload"]))
                if recs and tot + a > self.txop_limit_s:
                    break
                recs.append(r)
                tot += a
                if (self.dup_after and r["retries"] >= self.dup_after and len(recs) + len(dups) < self.burst_m
                        and tot + a <= self.txop_limit_s):
                    dups.append(r)
                    tot += a
            for i, r in enumerate(recs + dups):               # the second copies go to the end of the burst
                plen = HDR_DATA + len(r["payload"])
                frames.append({"kind": "D", "id": self._new_id(), "sn": r["sn"], "plen": plen, "rec": r,
                               "payload": r["payload"], "keep": r["keep"], "air": self.air_s(plen),
                               "t_enq": r["t_enq"], "dup": i >= len(recs)})
            if dups:
                self._st["tx_dup"] += len(dups)
        if pre1 is not None and frames:
            frames[0]["pre"] = pre1
            frames[0]["air"] = self.air_s(frames[0]["plen"], pre1)
        if self._pre_in is not None and len(frames) > 1:     # v6.9.3: short preamble inside, one postamble at the end
            for f in frames[1:]:
                f["pre"] = self._pre_in
            raws = [self.phy_raw(f["plen"], f.get("pre")) for f in frames]
            for f in frames:
                f["post"] = 0
            frames[-1]["post"] = self._burst_post(sum(raws))
            for f in frames:
                f["air"] = self.air_s(f["plen"], f.get("pre"), f["post"])
        n = len(frames)
        total = sum(f["air"] for f in frames)
        rem = total
        ws = self._tx_s[dst].ws if not unrel else 0
        for i, f in enumerate(frames):
            rem -= f["air"]
            if dst == BCAST:
                dur = 0.0
            elif unrel:
                dur = rem                                     # NAV only for the rest of the burst
            else:
                dur = rem + self.sifs_s + self.turnaround_s + self.ba_air()
            retry = f["rec"] is not None and f["rec"]["retries"] > 0
            hdr = data_header(f["sn"], dur, ws, bsn, i, n, rem, retry, noack=bool(unrel))
            f["off"] = total - rem - f["air"]
            if f["rec"] is not None:
                f["rec"]["state"] = "out"
                f["rec"]["tx"] += 1
            self._frame(f, hdr + f["payload"], dst, f["keep"])
        self._bsizes[n] += 1
        self._self_tx = True
        self._tx = {"phase": "prep", "frames": frames, "dst": dst, "bsn": bsn, "recs": recs, "t_rel": self._t,
                    "mute": self._mute_on(), "air_total": total, "kind": "D", "unrel": bool(unrel),
                    "pos": [f["rec"] for f in frames]}
        p_done = self._p_done_t if self._p_done_t is not None else self._t
        self._txr = {"t": round(_time.time(), 3), "kind": ("B" if dst == BCAST else "U") if unrel else "R",
                     "dst": dst, "bsn": bsn, "n": n, "bytes": sum(len(f["payload"]) for f in frames),
                     "retx": sum(1 for f in frames if f["rec"] is not None and f["rec"]["retries"] > 0),
                     "q": self.queue_len(), "wait_ms": round(1e3 * (self._t - min(f["t_enq"] for f in frames)), 1),
                     "att_ms": round(1e3 * (self._t - self._att_t0), 1),
                     "p_ms": round(1e3 * (p_done - self._att_t0), 1), "p_draws": self._p_draws,
                     "bo": self._bo0 if self._bo0 is not None else 0, "cw": self._cw_for(self._k),
                     "defer": self._freezes, "busy_ms": round(1e3 * self._att_busy, 1),
                     "nav_ms": round(1e3 * self._att_nav, 1), "sns": [f["sn"] for f in frames]}
        self._ev("release", kind="D", bsn=bsn, dst=dst, n=n, sns=[f["sn"] for f in frames],
                 retries=[f["rec"]["retries"] if f["rec"] else 0 for f in frames], cw=self._cw_for(self._k),
                 stage=min(self._k, self.k_max), backoff=self._bo0, defer=self._freezes, persist_draws=self._p_draws,
                 attempt_ms=round(1e3 * (self._t - self._att_t0), 2), cs_db=round(self._cs_db, 2),
                 floor_db=round(self._floor_db, 2) if self._floor_db is not None else None,
                 air_ms=round(1e3 * total, 2), q=self.queue_len(), mute=int(self._tx["mute"]),
                 wait_ms=round(1e3 * max((self._t - f["t_enq"]) for f in frames), 2), unrel=int(bool(unrel)),
                 dups=[f["sn"] for f in frames if f.get("dup")], pre1=frames[0].get("pre"),
                 **self._keep_list([{"keep": f["keep"]} for f in frames]))
        if self._tx["mute"]:
            self._tx["seq"] = self._sync_mute()
        return True

    def _start_ba(self, ba):
        self._self_tx = True
        self._tx = {"phase": "prep", "frames": [ba], "dst": ba["src"], "bsn": ba["bsn"], "recs": [], "t_rel": self._t,
                    "mute": self._mute_on(), "air_total": ba["nbytes"] * self.samples_per_byte / self.samp_rate,
                    "kind": "BA", "ba": ba}
        self._txr = {"t": round(_time.time(), 3), "kind": "A", "dst": ba["src"], "bsn": ba["bsn"], "n": 1,
                     "bytes": 0, "retx": 0, "q": self.queue_len(),
                     "wait_ms": round(1e3 * (self._t - ba["earliest"] + self.sifs_s), 1), "att_ms": 0, "p_ms": 0,
                     "p_draws": 0, "bo": 0, "cw": 0, "defer": 0, "busy_ms": 0, "nav_ms": 0,
                     "bitmap": bitstr(ba["bits"], ba["cnt"])}
        self._ev("release", kind="BA", bsn=ba["bsn"], dst=ba["src"], bitmap=bitstr(ba["bits"], ba["cnt"]),
                 rws=ba["rws"], sifs_ms=round(1e3 * (self._t - ba["earliest"] + self.sifs_s), 2),
                 air_ms=round(1e3 * self._tx["air_total"], 2), mute=int(self._tx["mute"]))
        if self._tx["mute"]:
            self._tx["seq"] = self._sync_mute()

    def _publish(self):
        tx = self._tx
        for f in tx["frames"]:
            self.message_port_pub(pmt.intern("pdu_out"), f["coded"])
        air = sum(f["nbytes"] for f in tx["frames"]) * self.samples_per_byte / self.samp_rate
        delay = self._tx_delay()
        tx.update({"phase": "air", "t0": self._t, "air": air, "echo_t": None, "air_end": self._t + delay + air,
                   "win_end": self._t + delay + air + self.gate_guard_s})
        if self._txr is not None:
            self._txr["unmute_ms"] = round(1e3 * (self._t - tx["t_rel"]), 1)
            self._txr["air_ms"] = round(1e3 * air, 1)
        if tx["kind"] == "BA":
            self._st["tx_BA"] += 1
            self._dbg["tx_BA"] += 1
            self._ev("publish", kind="BA", bsn=tx["bsn"], dst=tx["dst"], air_ms=round(1e3 * air, 2),
                     unmute_ms=round(1e3 * (self._t - tx["t_rel"]), 2), tx_delay_ms=round(1e3 * delay, 2))
            return
        off = 0.0
        for i, f in enumerate(tx["frames"]):
            fa = f["nbytes"] * self.samples_per_byte / self.samp_rate
            r = f["rec"]
            self._st["tx_D"] += 1
            self._dbg["tx_D"] += 1
            if r is not None and r["retries"] > 0:
                self._st["tx_retx"] += 1
            self._ev("publish", kind="D", seq=f["sn"], bsn=tx["bsn"], idx=i, n=len(tx["frames"]), dst=tx["dst"],
                     retry=r["retries"] if r else 0, off_ms=round(1e3 * off, 2), air_ms=round(1e3 * fa, 2),
                     unmute_ms=round(1e3 * (self._t - tx["t_rel"]), 2), tx_delay_ms=round(1e3 * delay, 2),
                     rx_off_ms=round(1e3 * (delay + air + self.gate_guard_s), 2), att=self._req_att,
                     **self._keep_py(f["keep"]))
            off += fa
        if self.verbose >= 2:
            print(f"[MAC] TXOP burst {tx['bsn']} -> ID {id_name(tx['dst'])}: SN "
                  f"{[f['sn'] for f in tx['frames']]} ({air * 1e3:.1f} ms on air, CW {self._cw_for(self._k)})",
                  flush=True)

    def _tx_delay(self):
        if self.auto_delay and len(self._echo_delays) >= 3:
            d = float(np.median(self._echo_delays))
            return max(d, self.tx_delay_s) if self._mute_on() else d
        return self.tx_delay_s

    def _end_tx(self):
        tx, self._tx = self._tx, {}
        self._self_tx = False
        self._prev_busy = None
        self._ev("rx_on", kind=tx["kind"], bsn=tx["bsn"], off_ms=round(1e3 * (self._t - tx["t_rel"]), 2),
                 echo=int(tx.get("echo_t") is not None), cs_db=round(self._cs_db, 2))
        if self._txr is not None:
            self._txr["off_ms"] = round(1e3 * (self._t - tx["t_rel"]), 1)
            self._txr["echo_ms"] = round(1e3 * (tx["echo_t"] - tx["t0"]), 1) if tx.get("echo_t") else None
        if tx["kind"] == "BA":
            ba = tx["ba"]
            with self._lock:
                if ba in self._ba_due:
                    self._ba_due.remove(ba)
            self._ba_sent.append(ba["key"])
            self._txop_done("-")
            self._after_own_tx()
            return
        if tx.get("unrel"):
            self._st["bc_sent" if tx["dst"] == BCAST else "udp_sent"] += len(tx["frames"])
            self._txop_done("-")
            self._reset_contention()
            return
        self._await = {"dst": tx["dst"], "bsn": tx["bsn"], "recs": tx["recs"], "pos": tx["pos"], "n": len(tx["frames"]),
                       "air_end": tx["air_end"], "deadline": tx["air_end"] + self.ba_timeout_s}
        self._ev("ba_wait", bsn=tx["bsn"], timeout_ms=round(1e3 * (self._await["deadline"] - self._t), 2))

    def _ba_timeout(self):
        a, self._await = self._await, None
        self._k = min(self._k + 1, self.k_max)                 # failed TXOP: CW doubles
        self._st["ba_timeout"] += 1
        self._dbg["ba_timeout"] += 1
        self._last_failed = a
        self._ev("ba_timeout", bsn=a["bsn"], dst=a["dst"], n=a["n"], next_cw=self._cw_for(self._k),
                 sns=[r["sn"] for r in a["recs"]], **self._keep_list(a["recs"]))
        self._arq_inbox.append(("noba", a["dst"], a["pos"], None, a["bsn"]))
        self._txop_done("timeout", acked=0)
        self._reset_contention()

    def _txop_done(self, res, **kw):
        r, self._txr = self._txr, None
        if r is None:
            return
        r["res"] = res
        r.update(kw)
        _emit("csma", "txop", **r)

    def _abort_tx(self, why):
        tx, self._tx = self._tx, {}
        self._self_tx = False
        back = set()
        for f in tx["frames"]:
            r = f.get("rec")
            if r is None:
                continue
            if r["state"] == "out":
                r["state"] = "retx" if r["retries"] > 0 else "new"
                back.add(id(r))
            if id(r) in back:
                r["tx"] -= 1
        self._st["aborted"] += 1
        self._ev("abort", why=why, kind=tx["kind"], bsn=tx["bsn"])
        print(f"[MAC] transmission aborted: {why}", flush=True)
        self._reset_contention()

    # ------------------------------------------------------------------ one sense step (nfft samples)
    def _step(self, cs_db):
        dt = self._dt
        while True:
            with self._lock:
                if not self._rx_ev:
                    break
                msg = self._rx_ev.popleft()
            try:
                self._process_rx(msg)
            except Exception as ex:
                print(f"[MAC] bad received frame ignored: {ex}", flush=True)
        thr_on = (self._floor_db if self._floor_db is not None else 1e9) + self.cs_margin_db

        if self._self_tx:
            self._dbg["tx_s"] += dt
            self._tx_phase(cs_db)
            self._arq_process()
            return

        # ---- physical carrier sense
        self._hist.append(cs_db)
        if self._floor_db is None:
            self._busy = True
        elif self._busy:
            self._busy = cs_db > thr_on - self.cs_hyst_db
        else:
            self._busy = cs_db > thr_on
        if self._busy:
            self._busy_time += dt
            self._dbg["busy_s"] += dt
            self._busy_max = max(self._busy_max, cs_db)
        self._transitions(cs_db, thr_on)
        nav = self._t < self._nav_until
        if nav:
            self._nav_time += dt
            self._dbg["nav_s"] += dt
        medium_busy = self._busy or nav

        if self._await is not None and self._t >= self._await["deadline"]:
            self._ba_timeout()
        self._arq_process()                                   # ARQ reads Block ACK results after the MAC

        # ---- Block ACKs this node owes go first (SIFS after the burst; no carrier sense, NAV or backoff)
        if self._ba_due:
            ba = min(self._ba_due, key=lambda b: b["earliest"])
            if self._t >= ba["earliest"]:
                if not ba["framed"]:
                    rs = self._rx_s.get(ba["src"])
                    ba["rws"] = rs.rws if rs else 0
                    ba["framed"] = True
                    if self.window_ba:                        # v6.9.2: bitmap of the whole reorder window
                        nbits = self._wba_bits()
                        wbits = 0
                        for sn in (rs.buf if rs else ()):
                            d = sdiff(sn, ba["rws"])
                            if 0 <= d < nbits:
                                wbits |= 1 << d
                        body = wba_frame(ba["bsn"], ba["rws"], nbits, wbits)
                    else:
                        body = ba_frame(ba["bsn"], ba["rws"], ba["cnt"], ba["bits"])
                    self._frame(ba, body, ba["src"], {})
                if ba["coded"] is not None:
                    self._start_ba(ba)
            return

        if self._await is not None or not self._has_work():
            if not self._has_work():
                self._reset_contention()
            return
        if medium_busy:
            self._att_busy += dt
            if nav:
                self._att_nav += dt

        # ---- 1. p-persistence
        if self._ph == "persist":
            if medium_busy:
                self._p_first, self._p_acc = True, 0.0
                return
            draw = False
            if self._p_first:
                self._p_first, draw = False, True
            else:
                self._p_acc += dt
                if self._p_acc >= self.slot_s:
                    self._p_acc -= self.slot_s
                    draw = True
            if draw:
                self._p_draws += 1
                if self._rng.random() < self.p_persist:
                    self._ph, self._idle_d = "difs", 0.0
                    self._p_done_t = self._t
                    self._ev("persist", draws=self._p_draws, wait_ms=round(1e3 * (self._t - self._att_t0), 2))
            return
        # ---- 2. DIFS
        if self._ph == "difs":
            if medium_busy:
                self._idle_d = 0.0
                return
            self._idle_d += dt
            if self._idle_d + 1e-9 < self.difs():
                return
            self._ph, self._bo_acc = "backoff", 0.0
            if self._bo is None:
                cw = self._cw_for(self._k)
                self._bo = self._bo0 = int(self._rng.integers(0, cw + 1))
                self._ev("backoff", slots=self._bo, cw=cw, stage=min(self._k, self.k_max))
        # ---- 3. backoff
        if self._ph == "backoff":
            if medium_busy:
                self._ph, self._idle_d = "difs", 0.0
                self._freezes += 1
                self._dbg["deferrals"] += 1
                self._ev("defer", left=self._bo, cs_db=round(cs_db, 2), nav=int(nav))
                return
            if self._bo > 0:
                self._bo_acc += dt
                while self._bo_acc >= self.slot_s and self._bo > 0:
                    self._bo_acc -= self.slot_s
                    self._bo -= 1
            if self._bo == 0:
                self._start_txop()

    def _tx_phase(self, cs_db):
        tx = self._tx
        ph = tx["phase"]
        if ph == "prep":                                      # framer round trip + TX unmute, in parallel
            ready = all(f["coded"] is not None for f in tx["frames"])
            muted_ok = (not tx["mute"]) or self._muter.done(tx["seq"])
            waited = self._t - tx["t_rel"]
            if ready and (muted_ok or waited >= self.unmute_timeout_s):
                if not muted_ok:
                    self._st["late_unmute"] += 1
                    self._ev("late_unmute", wait_ms=round(1e3 * waited, 2))
                self._publish()
            elif not ready and waited >= 0.5:
                self._abort_tx("the framer did not return the frames within 0.5 s")
                if tx["mute"]:
                    self._sync_mute()
            return
        if ph == "air":
            echo_thr = (self._floor_db if self._floor_db is not None else 1e9) + \
                (max(self.cs_margin_db, self.echo_margin_db) if tx["mute"] else self.cs_margin_db)
            if (tx["echo_t"] is None and cs_db > echo_thr and self._t < tx["win_end"]
                    and self._t - tx["t0"] >= 0.002):
                tx["echo_t"] = self._t
                self._echo_delays.append(self._t - tx["t0"])
                self._ev("echo", delay_ms=round(1e3 * (self._t - tx["t0"]), 2), cs_db=round(cs_db, 2),
                         thr_db=round(echo_thr, 2))
            if self._t >= tx["win_end"]:
                self._ev("win_end", echo=tx["echo_t"] is not None, cs_db=round(cs_db, 2),
                         off_ms=round(1e3 * (self._t - tx["t_rel"]), 2))
                if tx["mute"]:
                    tx["phase"] = "mute"
                    tx["seq"] = self._sync_mute()
                else:
                    self._end_tx()
            return
        if ph == "mute":
            if self._muter.done(tx["seq"]) or self._t - tx["win_end"] >= self.unmute_timeout_s:
                self._end_tx()

    def _transitions(self, cs_db, thr_on):
        if self._busy == self._prev_busy:
            return
        if self._prev_busy is not None:
            self._dbg["busy_on" if self._busy else "busy_off"] += 1
            sec = int(self._t)
            if sec != self._tr_sec:
                if self._tr_cnt > 100:
                    self._ev("transitions_suppressed", n=self._tr_cnt - 100, sec=self._tr_sec)
                self._tr_sec, self._tr_cnt = sec, 0
            self._tr_cnt += 1
            if self._tr_cnt <= 100:
                if self._busy:
                    self._ev("busy", cs_db=round(cs_db, 2), thr_db=round(thr_on, 2), q=self.queue_len())
                else:
                    self._ev("idle", cs_db=round(cs_db, 2), dur_ms=round(1e3 * (self._t - self._busy_since), 2),
                             max_db=round(self._busy_max, 2), q=self.queue_len())
        self._prev_busy = self._busy
        self._busy_since = self._t
        self._busy_max = cs_db

    def _update_floor(self):
        if len(self._hist) < self._hist.maxlen // 4:
            return
        raw = float(np.percentile(np.fromiter(self._hist, float, len(self._hist)), self.floor_percentile))
        if self._floor_db is None or raw <= self._floor_db:
            self._floor_db = raw
        else:
            self._floor_db = min(raw, self._floor_db + self.floor_rise_db_s * 0.05)

    # ------------------------------------------------------------------ stream
    def _work_fast(self, buf, nb, first_abs, blank):
        """The loop of work() with the C++ kernels (cdp_fast.cpp): the same steps, the same arithmetic, the same
        results (tools/test_fast_blocks.py compares them event by event).
        - carrier sense: the FFT medians and the smoothed cs_db of all nb sense steps at once (C++)
        - steps that only do bookkeeping run in C++ (_fast_steps): carrier-sense statistics, the contention timers
          (p-persistence slot timer, DIFS, backoff countdown) and the TX time while the own burst is on the air.
          C++ stops before every step that has a side effect - busy/idle change, random draw, event, start of a
          transmission, Block ACK deadline, received frame - and _step() runs that step exactly as before.
          Messages (app_in, rx_in, phy_in, ctrl) are handled by GNU Radio between work() calls, never during one,
          so nothing can change under a stretch of steps."""
        med = self._cs_fast(buf, nb)
        cs_all = self._cs_fast.smooth_db(self._smooth, med)
        self._smooth.extend(med.tolist())
        i = 0
        while i < nb:
            k = self._fast_steps(cs_all, i, first_abs, blank)
            if k:
                i += k
                if self._t >= self._floor_next:
                    self._floor_next = self._t + 0.05
                    self._update_floor()
                continue
            self._cs_db = cs_all[i]
            self._t = (first_abs + (i + 1) * self._nfft) / self.samp_rate
            was_tx = self._self_tx
            self._step(self._cs_db)
            if self.blank_rx and (was_tx or self._self_tx):
                s = first_abs + i * self._nfft
                blank.append((s, s + self._nfft))
            if self._t >= self._floor_next:
                self._floor_next = self._t + 0.05
                self._update_floor()
            i += 1

    def _fast_steps(self, cs_all, i, first_abs, blank):
        """-> number of steps (from step i) done in C++; 0 = step i needs _step()"""
        if self._rx_ev or self._arq_inbox:
            return 0
        F = self._cs_fast
        if self._self_tx:                                  # own burst on the air: only the TX time is counted
            tx = self._tx
            if tx.get("phase") != "air":
                return 0
            echo_thr = (self._floor_db if self._floor_db is not None else 1e9) + \
                (max(self.cs_margin_db, self.echo_margin_db) if tx["mute"] else self.cs_margin_db)
            st = np.array([self._dbg.get("tx_s", 0), self._t], np.float64)
            k = F.air(cs_all, i, first_abs, self._nfft, self.samp_rate, tx["echo_t"] is None, echo_thr,
                      tx["win_end"], tx["t0"], self._floor_next, self._dt, st)
            if k:
                self._dbg["tx_s"] = float(st[0])
                self._t = float(st[1])
                self._cs_db = cs_all[i + k - 1]
                if self.blank_rx:
                    blank.append((first_abs + i * self._nfft, first_abs + (i + k) * self._nfft))
            return k
        if self._ba_due or self._prev_busy is None or self._prev_busy != self._busy:
            return 0
        aw = self._await
        if aw is not None:                                 # waiting for a Block ACK (until its deadline)
            contend, deadline = False, aw["deadline"]
        else:
            contend, deadline = self._has_work(), math.inf
            if contend and self._ph not in F.PHASES:
                return 0
        st = np.array([self._busy_time, self._dbg.get("busy_s", 0), self._busy_max, self._nav_time,
                       self._dbg.get("nav_s", 0), self._t, self._att_busy, self._att_nav, self._p_acc,
                       self._idle_d, self._bo_acc], np.float64)
        ist = np.array([F.PHASES.get(self._ph, 0), 1 if self._p_first else 0, -1 if self._bo is None else self._bo],
                       np.int32)
        k, cnt = F.steps(cs_all, i, first_abs, self._nfft, self.samp_rate, self._floor_db, self.cs_margin_db,
                         self.cs_hyst_db, self._busy, self._nav_until, deadline, self._floor_next, self._dt,
                         contend, self.slot_s, self.difs(), st, ist)
        if k == 0:
            return 0
        self._hist.extend(cs_all[i:i + k])
        if cnt[0]:
            self._busy_time, self._dbg["busy_s"], self._busy_max = float(st[0]), float(st[1]), st[2]
        if cnt[1]:
            self._nav_time, self._dbg["nav_s"] = float(st[3]), float(st[4])
        self._t = float(st[5])
        self._cs_db = cs_all[i + k - 1]
        if contend:
            self._att_busy, self._att_nav = float(st[6]), float(st[7])
            self._p_acc, self._idle_d, self._bo_acc = float(st[8]), float(st[9]), float(st[10])
            self._p_first = bool(ist[1])
            self._bo = None if ist[2] < 0 else int(ist[2])
        elif aw is None:
            self._reset_contention()                       # what each of these steps did (nothing to send)
        return k

    def work(self, input_items, output_items):
        t_cpu = time.perf_counter()
        if self._wall0 is None:
            self._wall0, self._n_wall0 = time.monotonic(), self._n_abs
        if self._muter is not None:
            seen = (self.tx_mute, self.tx_att_db)
            self._sync_mute(force=(seen != self._seen_ctl))
            self._seen_ctl = seen
        x = input_items[0]
        out = output_items[0]
        n = len(x)
        out[:] = x
        first_abs = self._n_abs - len(self._carry)
        buf = np.concatenate((self._carry, x)) if len(self._carry) else x
        nb = len(buf) // self._nfft
        blank = []
        if nb and self._cs_fast is not None:
            self._work_fast(buf, nb, first_abs, blank)     # C++ front end + quiet steps: same results, see below
        elif nb:
            blocks = buf[:nb * self._nfft].reshape(nb, self._nfft) * self._win
            p = np.abs(np.fft.fft(blocks, axis=1)[:, self._bins]) ** 2
            med = np.median(p, axis=1) * self._norm
            for i in range(nb):
                self._smooth.append(float(med[i]))
                self._cs_db = 10.0 * np.log10(max(sum(self._smooth) / len(self._smooth), 1e-20))
                self._t = (first_abs + (i + 1) * self._nfft) / self.samp_rate
                was_tx = self._self_tx
                self._step(self._cs_db)
                if self.blank_rx and (was_tx or self._self_tx):
                    s = first_abs + i * self._nfft
                    blank.append((s, s + self._nfft))
                if self._t >= self._floor_next:
                    self._floor_next = self._t + 0.05
                    self._update_floor()
        self._carry = buf[nb * self._nfft:].copy()
        self._n_abs += n
        if self.blank_rx and self._self_tx:
            blank.append((self._n_abs - len(self._carry), self._n_abs))
        x0 = self._n_abs - n
        for s, e in blank:
            a, b = max(s, x0) - x0, min(e, self._n_abs) - x0
            if b > a:
                out[a:b] = 0
        self._cpu += time.perf_counter() - t_cpu
        if self.verbose and self.stats_every_s > 0 and self._t - self._stat_t0 >= self.stats_every_s:
            self._print_stats()
        return n

    def _print_stats(self):
        span = max(1e-9, self._t - self._stat_t0)
        st = self._st
        floor = f"{self._floor_db:.1f}" if self._floor_db is not None else "learning"
        dl = list(self._delay)
        rtt = list(self._rtt)
        bs = ", ".join(f"{k}: {v}" for k, v in sorted(self._bsizes.items())) or "-"
        print(f"[MAC] floor {floor} dB (+{self.cs_margin_db:g}), now {self._cs_db:.1f} dB, busy "
              f"{100 * self._busy_time / span:.1f} %, NAV {100 * self._nav_time / span:.1f} % | bursts by size {{{bs}}}, "
              f"Block ACKs received {st['ba_rx']} (late {st['late_ba']}), timeouts {st['ba_timeout']}, CW "
              f"{self.cw()} | Block ACKs sent {st['tx_BA']}, NAV set {st['nav_set']}x", flush=True)
        print(f"[ARQ] frames sent {st['tx_D']} (retransmissions {st['tx_retx']}, duplicates {st['tx_dup']}), "
              f"acknowledged {st['acked']}, "
              f"dropped after {self.retry_limit} retries {st['retry_drop']}, queue drops {st['qdrop']} | received "
              f"{st['rx_data']} new + {st['rx_dup']} duplicates, given up {st['rx_gave_up']} | "
              f"{self.window_info()} | delay median {1e3 * np.median(dl) if dl else 0:.0f} ms, Block ACK round trip "
              f"median {1e3 * np.median(rtt) if rtt else 0:.1f} ms (timeout {1e3 * self.ba_timeout_s:g})", flush=True)
        m = self._muter
        mute = ("TX mute OFF" if m is None or m.failed else "TX mute connecting" if not m.ok else
                f"TX mute {'on' if self.tx_mute else 'switched off'} (write max "
                f"{max(m.write_ms) if m.write_ms else 0:.1f} ms, late {st['late_unmute']})")
        wall = max(1e-9, time.monotonic() - self._wall0)
        rate = (self._n_abs - self._n_wall0) / wall
        share = 100.0 * rate / self.samp_rate
        ed = list(self._echo_delays)
        print(f"[MAC] own echo {1e3 * np.median(ed) if ed else 0:.1f} ms | {mute} | radio {share:.1f} % of "
              f"samp_rate, MAC CPU {100.0 * self._cpu / wall:.0f} %"
              + (f" | WARNING frame length formula off {st['len_mismatch']}x: phy_* must match the framer"
                 if st["len_mismatch"] else ""), flush=True)
        self._windows += 1
        if self._windows >= 2 and share < 99.0:
            print(f"[MAC] WARNING: only {share:.1f} % of the samples arrive: the PC is not keeping up.", flush=True)
        self._wall0, self._n_wall0, self._cpu = time.monotonic(), self._n_abs, 0.0
        self._stat_t0 = self._t
        self._busy_time = self._nav_time = 0.0
