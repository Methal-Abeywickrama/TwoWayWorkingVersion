"""
Embedded Python Block: ARQ Transport (TCP-like reliable messages) + socket for an external daemon

Wiring
  Packet Deframer RX pdu_out -> pdu_in        pdu_out -> Packet Framer TX msg_in        (mac_ctrl: optional -> CSMA ctrl)

What it does
  The daemon hands over whole messages (any size up to max_msg_bytes) for one node ID. The block cuts them into
  segments of at most mss bytes, sends them with a sliding window (selective-repeat ARQ), retransmits what is not
  acknowledged, and the receiving block puts each message back together and hands it, complete and in order, to
  its own daemon. The sender's daemon is told when a message is confirmed (SENT) or given up (FAIL).
  Unicast only (dest 0..254). One sender session per block start: a restarted node is recognised and its peer
  resets its receive state, so sequence numbers never get mixed between runs.

On air (inside the framer payload; the framer adds src_id / dest_id)
  DATA  C5 02 | session 4B | seq 2B | base 2B | flags 1B | data        (11-byte header; flags: 1 = first segment
                                                                         of a message, 2 = last, 4 = ACK now)
  ACK   C5 03 | session 4B | cum 2B | sack 4B                          (12 bytes)
    seq:  segment number (16 bit, per sender session and destination)
    base: the sender's lowest segment still in play; it will never send anything below it again, so the receiver
          gives up on segments below base it is still missing (abandoned) and uses the ones it already has
    cum:  next segment the receiver expects (all before it received); sack: bit i = segment cum+1+i received
  Each ACK costs a whole frame (channel access + airtime), so ACKs are few: the sender sets "ACK now" on the last
  segment of each burst it hands to the radio (window full or nothing more to send, and on retransmissions); the
  receiver ACKs on that flag, after ack_every segments, or at the latest ack_delay_s after a segment. A duplicate
  (the sender missed our ACK) also brings the ACK forward.
  Retransmission: per-segment timer with an adaptive RTO (srtt + 4 * rttvar, min_rto_s..max_rto_s, doubled on
  timeout), plus SACK-based early retransmission of holes. A congestion window (cwnd, at most window) is halved
  on a timeout and grows by one per ACK, so delay spikes on the shared channel don't trigger bursts of
  duplicates. After max_retries transmissions the message is reported FAIL and its segments are abandoned.

Daemon socket: TCP server on bind_addr:port. Every frame both ways = 4-byte big-endian length + body.
  daemon -> block   SEND   01 | ref 4B | dest 1B | message bytes
  block -> daemon   HELLO  80 | src_id 1B | mss 2B | window 1B | max_msg 4B          (on connect)
                    RECV   81 | src 1B | message bytes                              (to every connected client)
                    SENT   82 | ref 4B | dest 1B                                     (to the client that sent it)
                    FAIL   83 | ref 4B | dest 1B | reason 1B   (1 no ACK after max_retries, 2 bad dest, 3 queue full,
                                                                  4 too large, 5 empty)
  RECV messages that arrive while no client is connected are kept (up to rx_keep_msgs) and handed to the next one.
"""
import collections
import os
import random
import selectors
import socket
import struct
import threading
import time

import numpy as np
import pmt
from gnuradio import gr

MAGIC = 0xC5
P_DATA, P_ACK = 0x02, 0x03
F_FIRST, F_LAST, F_ACKNOW = 0x01, 0x02, 0x04
DATA_HDR = struct.Struct(">BBIHHB")
ACK_PKT = struct.Struct(">BBIHI")
C_SEND = 0x01
E_HELLO, E_RECV, E_SENT, E_FAIL = 0x80, 0x81, 0x82, 0x83
FAIL_RETRIES, FAIL_DEST, FAIL_QUEUE, FAIL_SIZE, FAIL_EMPTY = 1, 2, 3, 4, 5
RX_WIN = 64


def sdiff(a, b):
    """a - b in 16-bit sequence space, -32768..32767."""
    return ((a - b + 0x8000) & 0xFFFF) - 0x8000


class _Seg:
    __slots__ = ("data", "flags", "ref", "n_tx", "t_sent", "acked", "dead", "fast", "need")

    def __init__(self, data, flags, ref):
        self.data, self.flags, self.ref = data, flags, ref
        self.n_tx, self.t_sent, self.acked, self.dead, self.fast = 0, 0.0, False, False, False
        self.need = True                    # waiting to be (re)sent


class _TxPeer:
    def __init__(self, init_rto, window):
        self.cwnd = window                  # segments allowed in flight now (halved on timeout, +1 per ACK)
        self.next_seq = 0
        self.base = 0
        self.segs = collections.OrderedDict()   # seq -> _Seg, from base upwards
        self.bytes = 0
        self.srtt = None
        self.rttvar = 0.0
        self.rto = init_rto
        self.st = collections.Counter()


class _RxPeer:
    def __init__(self, session, expected):
        self.session = session
        self.expected = expected
        self.buf = {}
        self.reasm = None
        self.n_since_ack = 0
        self.ack_due = None
        self.last_ack = 0.0
        self.st = collections.Counter()


class _Client:
    def __init__(self, sock):
        self.sock = sock
        self.inbuf = bytearray()
        self.outbuf = bytearray()


class ArqTransport(gr.basic_block):
    def __init__(self, src_id=1, port=5100, bind_addr="127.0.0.1", mss=1000, window=8, ack_every=4,
                 ack_delay_s=0.5, init_rto_s=2.0, min_rto_s=1.0, max_rto_s=8.0, max_retries=8,
                 max_msg_bytes=4194304, max_queue_bytes=8388608, rx_keep_msgs=1000, verbose=1, stats_every_s=10.0):
        gr.basic_block.__init__(self, name="ARQ Transport (TCP-like, socket)", in_sig=None, out_sig=None)
        self.src_id = int(src_id)
        self.port = int(port)
        self.bind_addr = str(bind_addr)
        self.mss = max(16, int(mss))
        self.window = max(1, min(32, int(window)))
        self.ack_every = max(1, int(ack_every))
        self.ack_delay_s = float(ack_delay_s)
        self.init_rto_s = float(init_rto_s)
        self.min_rto_s = float(min_rto_s)
        self.max_rto_s = float(max_rto_s)
        self.max_retries = max(1, int(max_retries))
        self.max_msg_bytes = int(max_msg_bytes)
        self.max_queue_bytes = int(max_queue_bytes)
        self.rx_keep_msgs = int(rx_keep_msgs)
        self.verbose = int(verbose)
        self.stats_every_s = float(stats_every_s)

        self.session = 0
        self._tx = {}                 # dest -> _TxPeer
        self._rx = {}                 # src  -> _RxPeer
        self._msgs = {}               # (dest, ref) -> {"client", "left", "failed", "segs", "t0", "size"}
        self._clients = {}
        self._kept = collections.deque()
        self._inq = collections.deque()
        self._sel = None
        self._lsock = None
        self._wake_r = self._wake_w = None
        self._thr = None
        self._run = False
        self._t_stats = 0.0

        self.message_port_register_in(pmt.intern("pdu_in"))
        self.set_msg_handler(pmt.intern("pdu_in"), self._on_pdu)
        self.message_port_register_out(pmt.intern("pdu_out"))
        self.message_port_register_out(pmt.intern("mac_ctrl"))

    # ================================================================== GNU Radio side
    def start(self):
        self.session = random.getrandbits(32)
        self._sel = selectors.DefaultSelector()
        self._lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._lsock.bind((self.bind_addr, self.port))
        except OSError as e:
            print(f"[ARQ] cannot listen on {self.bind_addr}:{self.port}: {e}. Daemon socket disabled.", flush=True)
            self._lsock.close()
            self._lsock = None
        if self._lsock is not None:
            self._lsock.listen(8)
            self._lsock.setblocking(False)
            self._sel.register(self._lsock, selectors.EVENT_READ, "listen")
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._wake_w.setblocking(False)
        self._sel.register(self._wake_r, selectors.EVENT_READ, "wake")
        self._run = True
        self._t_stats = time.monotonic()
        self._thr = threading.Thread(target=self._loop, name="arq", daemon=True)
        self._thr.start()
        print(f"[ARQ] node {self.src_id}: daemon socket tcp://{self.bind_addr}:{self.port}, mss {self.mss} B, "
              f"window {self.window}, session {self.session:08x}", flush=True)
        return True

    def stop(self):
        self._run = False
        self._wake()
        if self._thr is not None:
            self._thr.join(timeout=2.0)
        for c in list(self._clients.values()):
            try:
                c.sock.close()
            except OSError:
                pass
        for s in (self._lsock, self._wake_r, self._wake_w):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        return True

    def _on_pdu(self, msg):
        """GNU Radio thread: keep only our packets, hand them to the worker."""
        try:
            meta, data = pmt.car(msg), pmt.cdr(msg)
            if not pmt.is_u8vector(data):
                return
            raw = bytes(pmt.u8vector_elements(data))
            if len(raw) < 2 or raw[0] != MAGIC or raw[1] not in (P_DATA, P_ACK):
                return
            src = pmt.to_long(pmt.dict_ref(meta, pmt.intern("src_id"), pmt.from_long(-1)))
        except Exception:
            return
        if src < 0:
            return
        self._inq.append((src, raw))
        self._wake()

    def _wake(self):
        try:
            self._wake_w.send(b"x")
        except (OSError, AttributeError):
            pass

    def _send_air(self, dest, raw):
        meta = pmt.make_dict()
        meta = pmt.dict_add(meta, pmt.intern("dest_id"), pmt.from_long(int(dest)))
        self.message_port_pub(pmt.intern("pdu_out"), pmt.cons(meta, pmt.init_u8vector(len(raw), list(raw))))

    def _mac(self, word):
        self.message_port_pub(pmt.intern("mac_ctrl"), pmt.intern(word))

    # ================================================================== worker thread
    def _loop(self):
        while self._run:
            for key, ev in self._sel.select(timeout=0.02):
                try:
                    if key.data == "listen":
                        self._accept()
                    elif key.data == "wake":
                        try:
                            while self._wake_r.recv(4096):
                                pass
                        except (BlockingIOError, OSError):
                            pass
                    else:
                        self._client_io(key.data, ev)
                except Exception as e:
                    print(f"[ARQ] socket error: {e!r}", flush=True)
            now = time.monotonic()
            while self._inq:
                src, raw = self._inq.popleft()
                try:
                    if raw[1] == P_DATA:
                        self._on_data(src, raw, now)
                    else:
                        self._on_ack(src, raw, now)
                except Exception as e:
                    print(f"[ARQ] bad packet from {src}: {e!r}", flush=True)
            self._timers(now)
            if self.verbose and self.stats_every_s > 0 and now - self._t_stats >= self.stats_every_s:
                self._t_stats = now
                self._print_stats()

    # ------------------------------------------------------------------ daemon sockets
    def _accept(self):
        while True:
            try:
                s, addr = self._lsock.accept()
            except (BlockingIOError, OSError):
                return
            s.setblocking(False)
            c = _Client(s)
            self._clients[s] = c
            self._sel.register(s, selectors.EVENT_READ, c)
            self._emit(c, struct.pack(">BBHBI", E_HELLO, self.src_id & 0xFF, self.mss, self.window,
                                      self.max_msg_bytes))
            if self.verbose:
                print(f"[ARQ] daemon connected from {addr[0]}:{addr[1]}", flush=True)
            while self._kept:
                self._emit(c, self._kept.popleft())

    def _drop_client(self, c):
        try:
            self._sel.unregister(c.sock)
        except Exception:
            pass
        try:
            c.sock.close()
        except OSError:
            pass
        self._clients.pop(c.sock, None)
        if self.verbose:
            print("[ARQ] daemon disconnected", flush=True)

    def _client_io(self, c, ev):
        if ev & selectors.EVENT_READ:
            try:
                data = c.sock.recv(262144)
            except (BlockingIOError, InterruptedError):
                data = None
            except OSError:
                data = b""
            if data == b"":
                self._drop_client(c)
                return
            if data:
                c.inbuf += data
                self._parse_client(c)
        if ev & selectors.EVENT_WRITE and c.sock in self._clients:
            self._flush(c)

    def _parse_client(self, c):
        while len(c.inbuf) >= 4:
            n = int.from_bytes(c.inbuf[:4], "big")
            if n > self.max_msg_bytes + 64:
                print(f"[ARQ] daemon frame of {n} B is too large; closing that connection", flush=True)
                self._drop_client(c)
                return
            if len(c.inbuf) < 4 + n:
                return
            body = bytes(c.inbuf[4:4 + n])
            del c.inbuf[:4 + n]
            if len(body) >= 6 and body[0] == C_SEND:
                ref = int.from_bytes(body[1:5], "big")
                self._submit(c, ref, body[5], body[6:])

    def _emit(self, c, body):
        c.outbuf += len(body).to_bytes(4, "big") + body
        self._flush(c)

    def _flush(self, c):
        try:
            while c.outbuf:
                k = c.sock.send(c.outbuf)
                del c.outbuf[:k]
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            self._drop_client(c)
            return
        try:
            self._sel.modify(c.sock, selectors.EVENT_READ | (selectors.EVENT_WRITE if c.outbuf else 0), c)
        except Exception:
            pass

    def _deliver(self, src, msg):
        body = bytes([E_RECV, src & 0xFF]) + msg
        if self._clients:
            for c in list(self._clients.values()):
                self._emit(c, body)
        else:
            self._kept.append(body)
            while len(self._kept) > self.rx_keep_msgs:
                self._kept.popleft()

    # ------------------------------------------------------------------ sending
    def _fail_now(self, c, ref, dest, reason):
        if c is not None and c.sock in self._clients:
            self._emit(c, struct.pack(">BIBB", E_FAIL, ref, dest & 0xFF, reason))

    def _submit(self, c, ref, dest, msg):
        if not 0 <= dest <= 254 or dest == self.src_id:
            return self._fail_now(c, ref, dest, FAIL_DEST)
        if not msg:
            return self._fail_now(c, ref, dest, FAIL_EMPTY)
        if len(msg) > self.max_msg_bytes:
            return self._fail_now(c, ref, dest, FAIL_SIZE)
        tp = self._tx.get(dest)
        if tp is None:
            tp = self._tx[dest] = _TxPeer(self.init_rto_s, self.window)
        if tp.bytes + len(msg) > self.max_queue_bytes:
            return self._fail_now(c, ref, dest, FAIL_QUEUE)
        n = -(-len(msg) // self.mss)
        seqs = []
        for i in range(n):
            flags = (F_FIRST if i == 0 else 0) | (F_LAST if i == n - 1 else 0)
            seq = tp.next_seq
            tp.next_seq = (tp.next_seq + 1) & 0xFFFF
            tp.segs[seq] = _Seg(msg[i * self.mss:(i + 1) * self.mss], flags, ref)
            seqs.append(seq)
        tp.bytes += len(msg)
        self._msgs[(dest, ref)] = {"client": c, "left": n, "failed": False, "segs": seqs,
                                   "t0": time.monotonic(), "size": len(msg)}
        tp.st["msgs"] += 1
        if self.verbose >= 2:
            print(f"[ARQ] message ref {ref} to node {dest}: {len(msg)} B in {n} segments", flush=True)

    def _xmit(self, dest, tp, seq, sg, now, ack_now=False):
        hdr = DATA_HDR.pack(MAGIC, P_DATA, self.session, seq, tp.base, sg.flags | (F_ACKNOW if ack_now else 0))
        self._send_air(dest, hdr + sg.data)
        sg.n_tx += 1
        sg.t_sent = now
        sg.need = False
        tp.st["tx"] += 1
        if sg.n_tx > 1:
            tp.st["retx"] += 1

    def _fail_msg(self, dest, tp, ref, reason):
        m = self._msgs.get((dest, ref))
        if m is None or m["failed"]:
            return
        m["failed"] = True
        for s in m["segs"]:
            sg = tp.segs.get(s)
            if sg is not None and not sg.acked:
                sg.dead = True
                tp.bytes -= len(sg.data)
        self._fail_now(m["client"], ref, dest, reason)
        tp.st["failed"] += 1
        self._msgs.pop((dest, ref), None)
        print(f"[ARQ] message ref {ref} to node {dest} FAILED (no ACK after {self.max_retries} tries)", flush=True)

    def _advance_base(self, tp):
        while tp.segs:
            seq, sg = next(iter(tp.segs.items()))
            if not (sg.acked or sg.dead):
                break
            tp.segs.popitem(last=False)
            tp.base = (seq + 1) & 0xFFFF
        if not tp.segs:
            tp.base = tp.next_seq

    def _on_ack(self, src, raw, now):
        _, _, session, cum, sack = ACK_PKT.unpack(raw[:ACK_PKT.size])
        tp = self._tx.get(src)
        if tp is None or session != self.session:
            return
        progress = False
        for seq, sg in list(tp.segs.items()):
            if sg.acked or sg.dead:
                continue
            d = sdiff(seq, cum)
            hit = d < 0 or (0 < d <= 32 and (sack >> (d - 1)) & 1)
            if not hit:
                continue
            sg.acked = True
            progress = True
            tp.bytes -= len(sg.data)
            if sg.n_tx == 1:
                self._rtt_sample(tp, now - sg.t_sent)
            m = self._msgs.get((src, sg.ref))
            if m is not None:
                m["left"] -= 1
                if m["left"] == 0:
                    c = m["client"]
                    if c is not None and c.sock in self._clients:
                        self._emit(c, struct.pack(">BIB", E_SENT, sg.ref, src & 0xFF))
                    tp.st["sent"] += 1
                    if self.verbose:
                        dt = now - m["t0"]
                        print(f"[ARQ] message ref {sg.ref} to node {src} confirmed: {m['size']} B in {dt:.1f} s "
                              f"({m['size'] / max(dt, 1e-6) / 1e3:.2f} kB/s)", flush=True)
                    self._msgs.pop((src, sg.ref), None)
        if progress:
            tp.rto = self._rto(tp)
            tp.cwnd = min(self.window, tp.cwnd + 1)
            self._mac("ok")
        # SACK-based early retransmission: holes below the highest segment the receiver already has
        if sack:
            high = (cum + sack.bit_length()) & 0xFFFF
            guard = max(0.2, 0.5 * (tp.srtt or self.init_rto_s))
            for seq, sg in tp.segs.items():
                if sdiff(seq, high) >= 0:
                    break
                if not (sg.acked or sg.dead or sg.need) and sg.n_tx > 0 and not sg.fast and now - sg.t_sent > guard:
                    sg.fast, sg.need = True, True           # lost: send again at the next _pump
                    tp.st["fast"] += 1
        self._advance_base(tp)
        self._pump(src, tp, now)

    def _rtt_sample(self, tp, r):
        if tp.srtt is None:
            tp.srtt, tp.rttvar = r, r / 2
        else:
            tp.rttvar = 0.75 * tp.rttvar + 0.25 * abs(tp.srtt - r)
            tp.srtt = 0.875 * tp.srtt + 0.125 * r

    def _rto(self, tp):
        if tp.srtt is None:
            return self.init_rto_s
        return min(self.max_rto_s, max(self.min_rto_s, tp.srtt + 4 * tp.rttvar))

    # ------------------------------------------------------------------ receiving
    def _on_data(self, src, raw, now):
        _, _, session, seq, base, flags = DATA_HDR.unpack(raw[:DATA_HDR.size])
        data = raw[DATA_HDR.size:]
        st = self._rx.get(src)
        if st is None or st.session != session:
            if st is not None and self.verbose:
                print(f"[ARQ] node {src} restarted (new session {session:08x}): receive state reset", flush=True)
            st = self._rx[src] = _RxPeer(session, base)
        while sdiff(base, st.expected) > 0:                  # sender will never resend below base
            if st.expected in st.buf:
                self._consume(src, st)
            else:                                            # abandoned segment: its message is lost
                if st.reasm is not None:
                    st.st["discarded"] += 1
                st.reasm = None
                st.st["skipped"] += 1
                st.expected = (st.expected + 1) & 0xFFFF
        d = sdiff(seq, st.expected)
        dup = False
        if d < 0 or seq in st.buf:
            st.st["dup"] += 1
            dup = True
        elif d >= RX_WIN:
            st.st["outside"] += 1
        else:
            st.buf[seq] = (flags, data)
            while st.expected in st.buf:
                self._consume(src, st)
        st.n_since_ack += 1
        if dup:                                              # sender is resending: our ACK got lost, repeat it soon
            due = max(now, st.last_ack + self.ack_delay_s)
            st.ack_due = due if st.ack_due is None else min(st.ack_due, due)
        elif flags & F_ACKNOW or st.n_since_ack >= self.ack_every:
            self._send_ack(src, st, now)
        elif st.ack_due is None:
            st.ack_due = now + self.ack_delay_s              # at the latest ack_delay_s after this segment

    def _consume(self, src, st):
        """Take segment 'expected' from the buffer and add it to the message being rebuilt."""
        f, dd = st.buf.pop(st.expected)
        st.expected = (st.expected + 1) & 0xFFFF
        if f & F_FIRST:
            if st.reasm is not None:
                st.st["discarded"] += 1
            st.reasm = bytearray()
        if st.reasm is None:
            return                                           # middle of a message we lost the start of
        st.reasm += dd
        if len(st.reasm) > self.max_msg_bytes:
            st.reasm = None
            st.st["discarded"] += 1
            return
        if f & F_LAST:
            self._deliver(src, bytes(st.reasm))
            st.st["delivered"] += 1
            st.st["bytes"] += len(st.reasm)
            st.reasm = None

    def _send_ack(self, src, st, now):
        sack = 0
        for s in st.buf:
            d = sdiff(s, st.expected)
            if 1 <= d <= 32:
                sack |= 1 << (d - 1)
        self._send_air(src, ACK_PKT.pack(MAGIC, P_ACK, st.session, st.expected, sack))
        st.n_since_ack, st.ack_due, st.last_ack = 0, None, now
        st.st["acks"] += 1

    # ------------------------------------------------------------------ timers
    def _timers(self, now):
        for src, st in self._rx.items():
            if st.ack_due is not None and now >= st.ack_due:
                self._send_ack(src, st, now)
        for dest, tp in self._tx.items():
            if not tp.segs:
                continue
            late = [(seq, sg) for seq, sg in tp.segs.items()
                    if sg.n_tx and not (sg.acked or sg.dead or sg.need) and now - sg.t_sent >= tp.rto]
            if late:
                # Timeout: everything overdue is sent again, oldest first, but only as fast as the (halved)
                # congestion window allows, so a delay spike does not turn into a burst of duplicates.
                for seq, sg in late:
                    sg.need, sg.fast = True, False
                tp.rto = min(self.max_rto_s, tp.rto * 2)
                tp.cwnd = max(2, tp.cwnd // 2)
                tp.st["timeouts"] += 1
                self._mac("fail")
            self._advance_base(tp)
            self._pump(dest, tp, now)

    def _pump(self, dest, tp, now):
        """Send what is waiting (retransmissions first, oldest first), keeping at most cwnd segments in flight."""
        inflight = sum(1 for sg in tp.segs.values() if sg.n_tx and not (sg.acked or sg.dead or sg.need))
        burst = []
        for seq, sg in tp.segs.items():
            if inflight + len(burst) >= min(tp.cwnd, self.window) or sdiff(seq, tp.base) >= self.window:
                break
            if not sg.need or sg.acked or sg.dead:
                continue
            if sg.n_tx >= self.max_retries:
                self._fail_msg(dest, tp, sg.ref, FAIL_RETRIES)
                continue
            burst.append((seq, sg))
        for i, (seq, sg) in enumerate(burst):                     # last of the burst asks for an ACK
            self._xmit(dest, tp, seq, sg, now, ack_now=(i == len(burst) - 1))
        if burst:
            self._advance_base(tp)

    def _print_stats(self):
        parts = []
        for dest, tp in self._tx.items():
            infl = sum(1 for sg in tp.segs.values() if sg.n_tx and not (sg.acked or sg.dead or sg.need))
            srtt = f"{tp.srtt:.2f}" if tp.srtt is not None else "-"
            parts.append(f"to {dest}: msgs {tp.st['msgs']} sent {tp.st['sent']} failed {tp.st['failed']}, "
                         f"segs tx {tp.st['tx']} retx {tp.st['retx']} (early {tp.st['fast']}), in flight {infl}, "
                         f"queued {sum(1 for sg in tp.segs.values() if not sg.n_tx)}, cwnd {tp.cwnd}, srtt {srtt} s rto {tp.rto:.2f} s")
        for src, st in self._rx.items():
            parts.append(f"from {src}: delivered {st.st['delivered']} msgs / {st.st['bytes']} B, dup {st.st['dup']}, "
                         f"acks {st.st['acks']}, discarded {st.st['discarded']}, skipped {st.st['skipped']}")
        if parts:
            print(f"[ARQ] {len(self._clients)} daemon(s) | " + " | ".join(parts), flush=True)
