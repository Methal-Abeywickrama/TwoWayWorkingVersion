"""
Embedded Python Block: App Socket Bridge (v6.9) - lets programs outside GNU Radio use the link.

  Reliable messages (any size, delivered in order, SENT / FAIL per message): TCP host:arq_port
      protocol = cdp_sock.py: 4-byte big-endian length + body
          SEND  01 | ref 4B | dest 1B | data        program -> bridge
          HELLO 80 | node 1B | mss 2B | window 1B | max_msg 4B
          RECV  81 | src 1B | data
          SENT  82 | ref 4B | dest 1B
          FAIL  83 | ref 4B | dest 1B | reason 1B   (1 no ACK, 2 bad dest, 3 queue full, 4 too large, 5 empty)
          PROG  84 | ref 4B | dest 1B | acked 2B | segments 2B   (big messages, at most once a second; cdp_sock
                                                                    clients that do not know it see an "other" event)
      Several programs may connect; every RECV goes to all of them, SENT / FAIL to the one that sent.
  Datagrams (UDP-like, no retransmission, no reordering): send  dest 1B | data  to udp_bind:udp_port,
      received ones arrive as  src 1B | dest 1B | data  on udp_bind:udp_out_port.

Over the air (inside the MAC payload) every message segment / datagram carries a send timestamp:
      segment   'S' | msg id 4B | index 2B | count 2B | tx time (us since epoch) 8B | data   (17 B header)
      datagram  'U' | datagram seq 2B | tx time (us) 8B | data                                 (11 B header)
The timestamps are only logged (one-way delay, gaps); nothing in the link uses them.
Messages are cut into segments of mss bytes; at most inflight segments per destination are handed to the MAC at a
time (the rest wait here), so a big file never floods the MAC queue. The message with the fewest segments left goes
first, so a text is not stuck behind a file.
v6.9.1: a segment the MAC drops (retry limit) or cannot queue is handed to the MAC again (end-to-end retransmission;
the receiver removes duplicates). A message fails only when none of its segments got through for stall_s seconds
(or after msg_timeout_s, 0 = never); then its frames still queued in the MAC are cancelled (MAC ctrl).
The receiver reassembles several messages per sender at once and ignores segments of messages it already delivered.

Wiring: to_mac -> MAC app_in;  MAC app_out -> from_mac;  MAC tx_status -> status;  mac_ctrl -> MAC ctrl
Logging: events go to the debug bus (blk "bridge": udp_tx, udp_rx, msg_tx, msg_rx, client), written by the
Session Logger block into the session folder.
"""
import collections
import os
import random
import select
import socket
import struct
import threading

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

SVC_SEG, SVC_UDP = 0x53, 0x55
SEG_HDR, UDP_HDR = 17, 11
REASON = {1: "no ACK", 2: "bad destination", 3: "queue full", 4: "too large", 5: "empty", 6: "timeout"}
RX_DONE_KEEP = 256          # message ids remembered per sender (duplicate segments of delivered messages)
RX_STALE_S = 600.0          # an incomplete received message is discarded after this long without a new segment


def _now_us():
    return _time.time_ns() // 1000


class AppBridge(gr.basic_block):
    def __init__(self, my_id=1, host="127.0.0.1", arq_port=5100, udp_bind="127.0.0.1", udp_port=5200,
                 udp_out_port=5201, mss=1000, inflight=32, window=16, max_msg=16000000, msg_timeout_s=0.0,
                 stall_s=60.0, verbose=1):
        gr.basic_block.__init__(self, name="App Socket Bridge (TCP ARQ + UDP)", in_sig=None, out_sig=None)
        self.my_id = int(my_id) & 0xFF
        self.host = str(host)
        self.arq_port = int(arq_port)
        self.udp_bind = str(udp_bind)
        self.udp_port = int(udp_port)
        self.udp_out_port = int(udp_out_port)
        self.mss = max(16, int(mss))
        self.inflight = max(1, int(inflight))
        self.window = int(window)
        self.max_msg = int(max_msg)
        self.msg_timeout_s = float(msg_timeout_s)
        self.stall_s = float(stall_s)
        self.verbose = int(verbose)
        self.message_port_register_out(pmt.intern("to_mac"))
        self.message_port_register_out(pmt.intern("mac_ctrl"))
        self.message_port_register_in(pmt.intern("from_mac"))
        self.set_msg_handler(pmt.intern("from_mac"), self._on_mac)
        self.message_port_register_in(pmt.intern("status"))
        self.set_msg_handler(pmt.intern("status"), self._on_status)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._threads = []
        self._clients = []                  # [sock, send-lock]
        self._mid = random.getrandbits(31)
        self._pending = {}                  # mid -> message being sent
        self._active = {}                   # dest -> [mid, ...] messages with segments to send / in the MAC
        self._inflight_n = {}               # dest -> segments in the MAC
        self._dest_prog = {}                # dest -> last time a segment to it was acknowledged
        self._rx = {}                       # (src, mid) -> reassembly state
        self._rx_done = {}                  # src -> (deque, set) of delivered message ids
        self._useq = 0
        self._udp_last = {}                 # src -> (rx time us, useq)
        self._udp_tx = self._udp_rx = None
        self._lsock = None

    # ------------------------------------------------------------------ start / stop
    def start(self):
        _bus().sources["bridge"] = self
        _emit("bridge", "config", my_id=self.my_id, arq=f"{self.host}:{self.arq_port}",
              udp_in=f"{self.udp_bind}:{self.udp_port}", udp_out=f"{self.udp_bind}:{self.udp_out_port}",
              mss=self.mss, inflight=self.inflight, window=self.window, max_msg=self.max_msg, stall_s=self.stall_s,
              msg_timeout_s=self.msg_timeout_s)
        self._stop.clear()
        try:
            self._lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._lsock.bind((self.host, self.arq_port))
            self._lsock.listen(4)
        except OSError as e:
            print(f"[BRIDGE] cannot listen on TCP {self.host}:{self.arq_port}: {e} (another flowgraph still "
                  f"running?)", flush=True)
            _emit("bridge", "error", what="tcp_bind", err=str(e))
            self._lsock = None
        try:
            self._udp_rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._udp_rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._udp_rx.bind((self.udp_bind, self.udp_port))
            self._udp_tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except OSError as e:
            print(f"[BRIDGE] cannot bind UDP {self.udp_bind}:{self.udp_port}: {e}", flush=True)
            _emit("bridge", "error", what="udp_bind", err=str(e))
            self._udp_rx = None
        for fn, nm in ((self._accept_loop, "bridge-accept"), (self._udp_loop, "bridge-udp"),
                       (self._timer_loop, "bridge-timer")):
            t = threading.Thread(target=fn, name=nm, daemon=True)
            t.start()
            self._threads.append(t)
        print(f"[BRIDGE] node {self.my_id}: reliable messages on tcp://{self.host}:{self.arq_port}, datagrams in "
              f"udp://{self.udp_bind}:{self.udp_port} -> out udp://{self.udp_bind}:{self.udp_out_port} (mss "
              f"{self.mss} B)", flush=True)
        return True

    def stop(self):
        self._stop.set()
        for s in [self._lsock, self._udp_rx] + [c[0] for c in self._clients]:
            try:
                if s:
                    s.close()
            except OSError:
                pass
        return True

    # ------------------------------------------------------------------ TCP clients
    def _accept_loop(self):
        while not self._stop.is_set() and self._lsock is not None:
            try:
                r, _, _ = select.select([self._lsock], [], [], 0.5)
                if not r:
                    continue
                c, addr = self._lsock.accept()
            except OSError:
                return
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            entry = [c, threading.Lock()]
            with self._lock:
                self._clients.append(entry)
            self._send_to(entry, struct.pack(">BBHBI", 0x80, self.my_id, self.mss, min(255, self.window),
                                             self.max_msg))
            _emit("bridge", "client", ev="connect", peer=f"{addr[0]}:{addr[1]}", n=len(self._clients))
            if self.verbose:
                print(f"[BRIDGE] program connected from {addr[0]}:{addr[1]}", flush=True)
            t = threading.Thread(target=self._client_loop, args=(entry,), daemon=True)
            t.start()

    def _client_loop(self, entry):
        sock, buf = entry[0], bytearray()
        try:
            while not self._stop.is_set():
                r, _, _ = select.select([sock], [], [], 0.5)
                if not r:
                    continue
                chunk = sock.recv(262144)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= 4:
                    n = int.from_bytes(buf[:4], "big")
                    if len(buf) < 4 + n:
                        break
                    body = bytes(buf[4:4 + n])
                    del buf[:4 + n]
                    if body and body[0] == 0x01 and len(body) >= 6:
                        ref, dest = struct.unpack(">IB", body[1:6])
                        self._submit(entry, ref, dest, body[6:])
        except OSError:
            pass
        with self._lock:
            if entry in self._clients:
                self._clients.remove(entry)
        _emit("bridge", "client", ev="disconnect", n=len(self._clients))
        try:
            sock.close()
        except OSError:
            pass

    def _send_to(self, entry, body):
        try:
            with entry[1]:
                entry[0].sendall(len(body).to_bytes(4, "big") + body)
            return True
        except OSError:
            return False

    def _fail(self, entry, ref, dest, reason, m=None):
        self._send_to(entry, struct.pack(">BIBB", 0x83, ref, dest & 0xFF, reason))
        if m is not None:
            self._log_msg_tx(m, "fail:" + REASON.get(reason, str(reason)))

    # ------------------------------------------------------------------ sending a message
    def _submit(self, entry, ref, dest, data):
        if not data:
            return self._fail(entry, ref, dest, 5)
        if dest == self.my_id or dest > 255:
            return self._fail(entry, ref, dest, 2)
        if len(data) > self.max_msg:
            return self._fail(entry, ref, dest, 4)
        n = -(-len(data) // self.mss)
        if n > 0xFFFF:
            return self._fail(entry, ref, dest, 4)
        now = _time.time()
        with self._lock:
            self._mid = (self._mid + 1) & 0xFFFFFFFF
            mid = self._mid
            m = {"mid": mid, "ref": ref, "entry": entry, "dest": dest, "data": data, "n": n, "acked": set(),
                 "todo": collections.deque(range(n)), "out": set(), "t0": now, "t_first": None, "t_prog": now,
                 "t_hold": 0.0, "t_report": 0.0, "tx": 0, "resent": 0, "done": False, "bytes": len(data)}
            self._pending[mid] = m
            self._active.setdefault(dest, []).append(mid)
            queued = sum(len(self._pending[x]["todo"]) for x in self._active[dest] if x in self._pending)
        _emit("bridge", "msg_submit", mid=mid, ref=ref, dst=dest, bytes=len(data), segs=n, queued_segs=queued)
        self._pump(dest)

    def _next_segment(self, dest, now):
        """The message with the fewest segments left that has one to send (texts overtake files)."""
        best = None
        for mid in self._active.get(dest, []):
            m = self._pending.get(mid)
            if m is None or m["done"] or not m["todo"] or m["t_hold"] > now:
                continue
            left = m["n"] - len(m["acked"])
            if best is None or left < best[0]:
                best = (left, m)
        if best is None:
            return None
        m = best[1]
        return m, m["todo"].popleft()

    def _pump(self, dest):
        """Hand segments to the MAC while fewer than inflight are outstanding for this destination."""
        out = []
        now = _time.time()
        with self._lock:
            while dest == 255 or self._inflight_n.get(dest, 0) < self.inflight:
                nxt = self._next_segment(dest, now)
                if nxt is None:
                    break
                m, idx = nxt
                if idx in m["acked"]:
                    continue
                m["out"].add(idx)
                if dest != 255:
                    self._inflight_n[dest] = self._inflight_n.get(dest, 0) + 1
                if m["t_first"] is None:
                    m["t_first"] = now
                out.append((m, idx))
        for m, idx in out:
            chunk = m["data"][idx * self.mss:(idx + 1) * self.mss]
            hdr = struct.pack(">BIHHQ", SVC_SEG, m["mid"], idx, m["n"], _now_us())
            meta = pmt.make_dict()
            meta = pmt.dict_add(meta, pmt.intern("dest_id"), pmt.from_long(m["dest"]))
            meta = pmt.dict_add(meta, pmt.intern("app_ref"), pmt.from_long(m["mid"]))
            meta = pmt.dict_add(meta, pmt.intern("app_seg"), pmt.from_long(idx))
            body = hdr + chunk
            self.message_port_pub(pmt.intern("to_mac"), pmt.cons(meta, pmt.init_u8vector(len(body), list(body))))
        if dest == 255:                                  # broadcast is best effort: done once handed over
            for m, _ in out:
                with self._lock:
                    if not m["done"] and not m["todo"]:
                        self._finish(m)
                        self._send_to(m["entry"], struct.pack(">BIB", 0x82, m["ref"], 255))
                        self._log_msg_tx(m, "sent(broadcast, unconfirmed)")

    def _finish(self, m):
        """Caller holds the lock."""
        m["done"] = True
        self._pending.pop(m["mid"], None)
        lst = self._active.get(m["dest"], [])
        if m["mid"] in lst:
            lst.remove(m["mid"])

    def _on_status(self, msg):
        try:
            d = pmt.to_python(msg)
        except Exception:
            return
        mid, seg, st = int(d.get("app_ref", -1)), int(d.get("app_seg", 0)), str(d.get("status"))
        dest = int(d.get("dst", -1))
        fin, prog = None, None
        now = _time.time()
        with self._lock:
            if dest in self._inflight_n and st in ("acked", "dropped", "qfull"):
                self._inflight_n[dest] = max(0, self._inflight_n[dest] - 1)
            m = self._pending.get(mid)
            if m is not None and not m["done"]:
                m["tx"] += int(d.get("tx", 1) or 1)
                m["out"].discard(seg)
                if st == "acked":
                    if seg not in m["acked"]:
                        m["acked"].add(seg)
                        m["t_prog"] = now
                        self._dest_prog[dest] = now
                    if len(m["acked"]) >= m["n"]:
                        self._finish(m)
                        fin = (m, "sent")
                    elif m["n"] >= 8 and now - m["t_report"] >= 1.0:
                        m["t_report"] = now
                        prog = (m, len(m["acked"]))
                elif st in ("dropped", "qfull") and seg not in m["acked"]:
                    m["todo"].appendleft(seg)              # end-to-end retransmission: send it again
                    m["resent"] += 1
                    if st == "qfull":
                        m["t_hold"] = now + 0.3            # MAC queue full: give it a moment
                    if m["resent"] <= 3 or m["resent"] % 100 == 0:
                        _emit("bridge", "seg_resend", mid=mid, seg=seg, why=st, resent=m["resent"],
                              acked=len(m["acked"]), segs=m["n"])
        if prog is not None:
            pm, acked = prog
            self._send_to(pm["entry"], struct.pack(">BIBHH", 0x84, pm["ref"], pm["dest"] & 0xFF, acked & 0xFFFF,
                                                   pm["n"] & 0xFFFF))
        if fin is not None:
            m, res = fin
            self._send_to(m["entry"], struct.pack(">BIB", 0x82, m["ref"], m["dest"]))
            self._log_msg_tx(m, "sent")
        if dest >= 0:
            self._pump(dest)

    def _give_up(self, m, reason, why):
        """Caller does NOT hold the lock."""
        with self._lock:
            if m["done"]:
                return
            self._finish(m)
        if self.verbose:
            print(f"[BRIDGE] message {m['mid']} to ID {m['dest']} failed ({why}): {len(m['acked'])}/{m['n']} "
                  f"segments acknowledged, {m['resent']} resent", flush=True)
        d = pmt.make_dict()
        d = pmt.dict_add(d, pmt.intern("cancel_ref"), pmt.from_long(m["mid"]))
        self.message_port_pub(pmt.intern("mac_ctrl"), d)
        self._fail(m["entry"], m["ref"], m["dest"], reason, m)

    def _log_msg_tx(self, m, res):
        now = _time.time()
        dur = now - m["t0"]
        _emit("bridge", "msg_tx", mid=m["mid"], ref=m["ref"], dst=m["dest"], bytes=m["bytes"], segs=m["n"],
              acked=len(m["acked"]), frames_tx=m["tx"], resent=m["resent"],
              wait_s=round((m["t_first"] or now) - m["t0"], 3),
              dur_s=round(dur, 3), kBps=round(m["bytes"] / max(dur, 1e-6) / 1e3, 2), res=res)

    def _timer_loop(self):
        while not self._stop.wait(0.5):
            now = _time.time()
            late, dests = [], set()
            with self._lock:
                for mid, m in list(self._pending.items()):
                    if m["done"]:
                        continue
                    dests.add(m["dest"])
                    if self.msg_timeout_s > 0 and now - m["t0"] > self.msg_timeout_s:
                        late.append((m, 6, f"not done after {self.msg_timeout_s:g} s"))
                    elif self.stall_s > 0 and now - max(self._dest_prog.get(m["dest"], 0.0), m["t0"]) > self.stall_s:
                        late.append((m, 1, f"nothing reached ID {m['dest']} for {self.stall_s:g} s"))
                for (src, mid), st in list(self._rx.items()):
                    if now - st["t_last"] / 1e6 > RX_STALE_S:
                        self._rx.pop((src, mid), None)
                        _emit("bridge", "msg_rx", src=src, mid=mid, bytes=sum(len(p) for p in st["parts"].values()),
                              segs=st["cnt"], got=len(st["parts"]), res="incomplete (no segment for "
                              f"{RX_STALE_S:g} s)", dur_s=round((st["t_last"] - st["t_first"]) / 1e6, 3))
            for m, reason, why in late:
                self._give_up(m, reason, why)
            for d in dests:                                  # segments held back after a 'queue full'
                self._pump(d)

    # ------------------------------------------------------------------ datagrams
    def _udp_loop(self):
        while not self._stop.is_set() and self._udp_rx is not None:
            try:
                r, _, _ = select.select([self._udp_rx], [], [], 0.5)
                if not r:
                    continue
                d, _ = self._udp_rx.recvfrom(65535)
            except OSError:
                return
            if len(d) < 2:
                continue
            dest, data = d[0], d[1:]
            if dest == self.my_id:
                continue
            with self._lock:
                useq = self._useq
                self._useq = (self._useq + 1) & 0xFFFF
            t_us = _now_us()
            body = struct.pack(">BHQ", SVC_UDP, useq, t_us) + data
            meta = pmt.make_dict()
            meta = pmt.dict_add(meta, pmt.intern("dest_id"), pmt.from_long(dest))
            meta = pmt.dict_add(meta, pmt.intern("noack"), pmt.from_long(1))
            self.message_port_pub(pmt.intern("to_mac"), pmt.cons(meta, pmt.init_u8vector(len(body), list(body))))
            _emit("bridge", "udp_tx", dst=dest, useq=useq, bytes=len(data), tx_us=t_us)

    # ------------------------------------------------------------------ from the radio
    def _on_mac(self, msg):
        if not pmt.is_pair(msg) or not pmt.is_u8vector(pmt.cdr(msg)):
            return
        rx_us = _now_us()
        try:
            md = pmt.to_python(pmt.car(msg)) if pmt.is_dict(pmt.car(msg)) else {}
        except Exception:
            md = {}
        src, dst = int(md.get("src_id", -1)), int(md.get("dest_id", -1))
        raw = bytes(pmt.u8vector_elements(pmt.cdr(msg)))
        if len(raw) >= UDP_HDR and raw[0] == SVC_UDP:
            _, useq, tx_us = struct.unpack(">BHQ", raw[:UDP_HDR])
            data = raw[UDP_HDR:]
            if self._udp_tx is not None:
                try:
                    self._udp_tx.sendto(bytes([src & 0xFF, dst & 0xFF]) + data, (self.udp_bind, self.udp_out_port))
                except OSError:
                    pass
            last = self._udp_last.get(src)
            self._udp_last[src] = (rx_us, useq)
            iat = None if last is None else round((rx_us - last[0]) / 1e3, 2)
            gap = None if last is None else ((useq - last[1] - 1) & 0xFFFF)
            if gap is not None and gap > 0x8000:
                gap -= 0x10000                                # negative: arrived after a later one
            _emit("bridge", "udp_rx", src=src, useq=useq, bytes=len(data), tx_us=tx_us,
                  owd_ms=round((rx_us - tx_us) / 1e3, 2), iat_ms=iat, gap=gap, mac_sn=md.get("mac_seq"))
            return
        if len(raw) >= SEG_HDR and raw[0] == SVC_SEG:
            _, mid, idx, cnt, tx_us = struct.unpack(">BIHHQ", raw[:SEG_HDR])
            self._rx_segment(src, dst, mid, idx, cnt, tx_us, raw[SEG_HDR:], rx_us)
            return
        self._deliver(src, raw)                               # plain message (e.g. typed in the GRC box)

    def _rx_segment(self, src, dst, mid, idx, cnt, tx_us, chunk, rx_us):
        with self._lock:
            data, st = self._rx_add(src, mid, idx, cnt, tx_us, chunk, rx_us)
        if data is None:
            return
        self._deliver(src, data)
        dur = (rx_us - st["t_first"]) / 1e6
        _emit("bridge", "msg_rx", src=src, dst=dst, mid=mid, bytes=len(data), segs=cnt, got=cnt, res="ok",
              dups=st["dups"], dur_s=round(dur, 3), span_s=round((rx_us - st["tx_first"]) / 1e6, 3),
              owd_first_ms=round(st["owd_first"] / 1e3, 1), owd_max_ms=round(st["owd_max"] / 1e3, 1),
              kBps=round(len(data) / max(dur, 1e-3) / 1e3, 2))

    def _rx_add(self, src, mid, idx, cnt, tx_us, chunk, rx_us):
        """Caller holds the lock. Returns (complete message or None, state)."""
        dq, done = self._rx_done.setdefault(src, (collections.deque(), set()))
        if mid in done or idx >= cnt:
            return None, None                                 # duplicate of a delivered message (resent segment)
        key = (src, mid)
        st = self._rx.get(key)
        if st is None:
            st = self._rx[key] = {"mid": mid, "cnt": cnt, "parts": {}, "t_first": rx_us, "owd_first": rx_us - tx_us,
                                  "tx_first": tx_us, "t_last": rx_us, "owd_max": rx_us - tx_us, "dups": 0}
        if idx in st["parts"]:
            st["dups"] += 1
        st["parts"][idx] = chunk
        st["t_last"] = rx_us
        st["owd_max"] = max(st["owd_max"], rx_us - tx_us)
        if len(st["parts"]) >= st["cnt"]:
            data = b"".join(st["parts"][i] for i in range(st["cnt"]))
            self._rx.pop(key, None)
            done.add(mid)
            dq.append(mid)
            while len(dq) > RX_DONE_KEEP:
                done.discard(dq.popleft())
            return data, st
        return None, st

    def _deliver(self, src, data):
        body = bytes([0x81, src & 0xFF]) + data
        with self._lock:
            clients = list(self._clients)
        for c in clients:
            self._send_to(c, body)
