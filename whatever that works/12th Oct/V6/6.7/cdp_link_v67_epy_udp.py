"""
Embedded Python Block: UDP Transport (unreliable datagrams) + UDP sockets for an external daemon

Wiring
  Packet Deframer RX pdu_out -> pdu_in        pdu_out -> Packet Framer TX msg_in

What it does
  One daemon datagram = one radio frame, sent once: no ACK, no retransmission, no ordering, no splitting.
  dest 255 = every node (broadcast). Use it for chat lines, beacons, status, anything where a lost or late
  packet is better than a delayed one. Larger data or anything that must arrive: use the ARQ block.

On air (inside the framer payload; the framer adds src_id / dest_id):   C5 01 | payload

Daemon sockets (UDP, plain datagrams)
  daemon -> block : send to  bind_addr:listen_port   datagram = dest 1B | payload
  block -> daemon : sent to  deliver_addr:deliver_port   datagram = src 1B | dest 1B | payload
                    (dest = this node's ID, or 255 if it was a broadcast)
  payload up to max_payload_len - 2 bytes; larger datagrams are dropped with a message.
"""
import collections
import socket
import threading
import time

import pmt
from gnuradio import gr


def _cdp_register(name, blk):
    """Shared registry (one per flowgraph process) read by the Telemetry block."""
    import sys as _sys, types as _types
    reg = _sys.modules.get("cdp_registry")
    if reg is None:
        reg = _types.ModuleType("cdp_registry")
        reg.blocks = {}
        _sys.modules["cdp_registry"] = reg
    reg.blocks[name] = blk

MAGIC, P_UDP = 0xC5, 0x01


class UdpTransport(gr.basic_block):
    def __init__(self, src_id=1, listen_port=5200, deliver_port=5201, bind_addr="127.0.0.1",
                 deliver_addr="127.0.0.1", max_payload_len=2048, verbose=1, stats_every_s=10.0):
        gr.basic_block.__init__(self, name="UDP Transport (datagrams, socket)", in_sig=None, out_sig=None)
        self.src_id = int(src_id)
        self.listen_port = int(listen_port)
        self.deliver_port = int(deliver_port)
        self.bind_addr = str(bind_addr)
        self.deliver_addr = str(deliver_addr)
        self.max_data = max(1, int(max_payload_len) - 2)
        self.verbose = int(verbose)
        self.stats_every_s = float(stats_every_s)
        self._sock = None
        self._out = None
        self._thr = None
        self._run = False
        self._st = collections.Counter()
        self._st_last = collections.Counter()

        self.message_port_register_in(pmt.intern("pdu_in"))
        self.set_msg_handler(pmt.intern("pdu_in"), self._on_pdu)
        self.message_port_register_out(pmt.intern("pdu_out"))

    def start(self):
        self._out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._sock.bind((self.bind_addr, self.listen_port))
            self._sock.settimeout(0.2)
        except OSError as e:
            print(f"[UDP] cannot listen on {self.bind_addr}:{self.listen_port}: {e}. Sending from the daemon "
                  f"disabled.", flush=True)
            self._sock.close()
            self._sock = None
        self._run = True
        _cdp_register("udp", self)
        self._thr = threading.Thread(target=self._loop, name="udp", daemon=True)
        self._thr.start()
        print(f"[UDP] node {self.src_id}: daemon sends to udp://{self.bind_addr}:{self.listen_port} "
              f"(dest 1B + payload, max {self.max_data} B), received datagrams go to "
              f"udp://{self.deliver_addr}:{self.deliver_port} (src 1B + dest 1B + payload)", flush=True)
        return True

    def stop(self):
        self._run = False
        if self._thr is not None:
            self._thr.join(timeout=1.0)
        for s in (self._sock, self._out):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        return True

    def telemetry(self):
        return {"counters": dict(self._st), "listening": self._sock is not None,
                "listen_port": self.listen_port, "deliver_port": self.deliver_port, "max_data": self.max_data}

    # daemon -> radio
    def _loop(self):
        t_stats = time.monotonic()
        while self._run:
            if self._sock is not None:
                try:
                    dgram, addr = self._sock.recvfrom(65535)
                except socket.timeout:
                    dgram = None
                except OSError:
                    dgram = None
                    time.sleep(0.2)
                if dgram:
                    self._from_daemon(dgram)
            else:
                time.sleep(0.2)
            if self.verbose and self.stats_every_s > 0 and time.monotonic() - t_stats >= self.stats_every_s:
                t_stats = time.monotonic()
                d = {k: self._st[k] - self._st_last[k] for k in ("tx", "rx", "bcast", "rejected", "senderr")}
                if any(d.values()):
                    print(f"[UDP] since last line: sent {d['tx']} (broadcast {d['bcast']}), received {d['rx']}, "
                          f"rejected {d['rejected']}, local send errors {d['senderr']}", flush=True)
                self._st_last = collections.Counter(self._st)

    def _from_daemon(self, dgram):
        dest, data = dgram[0], dgram[1:]
        if not data or len(data) > self.max_data or dest == self.src_id:
            self._st["rejected"] += 1
            why = ("empty" if not data else f"{len(data)} B > {self.max_data} B" if len(data) > self.max_data
                   else "dest is this node")
            print(f"[UDP] datagram to {dest} dropped: {why}", flush=True)
            return
        raw = bytes([MAGIC, P_UDP]) + data
        meta = pmt.make_dict()
        meta = pmt.dict_add(meta, pmt.intern("dest_id"), pmt.from_long(int(dest)))
        self.message_port_pub(pmt.intern("pdu_out"), pmt.cons(meta, pmt.init_u8vector(len(raw), list(raw))))
        self._st["tx"] += 1
        if dest == 255:
            self._st["bcast"] += 1

    # radio -> daemon
    def _on_pdu(self, msg):
        try:
            meta, data = pmt.car(msg), pmt.cdr(msg)
            if not pmt.is_u8vector(data):
                return
            raw = bytes(pmt.u8vector_elements(data))
            if len(raw) < 2 or raw[0] != MAGIC or raw[1] != P_UDP:
                return
            src = pmt.to_long(pmt.dict_ref(meta, pmt.intern("src_id"), pmt.from_long(0)))
            dst = pmt.to_long(pmt.dict_ref(meta, pmt.intern("dest_id"), pmt.from_long(self.src_id)))
        except Exception:
            return
        self._st["rx"] += 1
        try:
            self._out.sendto(bytes([src & 0xFF, dst & 0xFF]) + raw[2:], (self.deliver_addr, self.deliver_port))
        except OSError:
            self._st["senderr"] += 1
