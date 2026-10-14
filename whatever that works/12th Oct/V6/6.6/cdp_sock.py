#!/usr/bin/env python3
"""
cdp_sock.py - use the CDP radio's transport blocks from any program (the daemon side)

  ARQ block (reliable messages): TCP  CDP_HOST:CDP_ARQ_PORT          (default 127.0.0.1:5100)
  UDP block (datagrams):         send to CDP_HOST:CDP_UDP_PORT          (default 5200)
                                 receive on CDP_UDP_BIND:CDP_UDP_OUT_PORT (default 127.0.0.1:5201)

Library
  from cdp_sock import ArqClient, UdpClient
  a = ArqClient()                      # connects, reads HELLO: a.node, a.mss, a.window, a.max_msg
  ref = a.send(5, b"hello")            # queue a message for node 5, returns its ref
  a.wait(ref, timeout=120)             # -> ("sent", ref, 5) or ("fail", ref, 5, reason) or None (timeout)
  for ev in a.events(): ...            # ("recv", src, data) | ("sent", ref, dest) | ("fail", ref, dest, reason)
  u = UdpClient()
  u.send(255, b"hi all")               # one datagram, no guarantee
  u.recv(timeout=1.0)                  # -> (src, dest, data) or None

Command line (settings from the environment variables above)
  python3 cdp_sock.py arq-send --dest 5 --text "hello"
  python3 cdp_sock.py arq-send --dest 5 --file photo.jpg
  python3 cdp_sock.py arq-listen --out-dir ~/cdp_inbox
  python3 cdp_sock.py udp-send --dest 255 --text "hi all"
  python3 cdp_sock.py udp-listen

Socket protocol (for writing your own daemon in any language)
  ARQ, both directions: 4-byte big-endian length + body
    SEND  01 | ref 4B | dest 1B | data            daemon -> block
    HELLO 80 | node 1B | mss 2B | window 1B | max_msg 4B
    RECV  81 | src 1B | data
    SENT  82 | ref 4B | dest 1B
    FAIL  83 | ref 4B | dest 1B | reason 1B       (1 no ACK, 2 bad dest, 3 queue full, 4 too large, 5 empty)
  UDP datagrams: to the block  dest 1B | data ;  from the block  src 1B | dest 1B | data
  File convention used by this tool (not by the blocks): data = b"CDPFILE\\0" + name + b"\\0" + file bytes
"""
import argparse
import os
import random
import select
import socket
import struct
import sys
import time

FAIL_REASONS = {1: "no ACK after all retries", 2: "bad destination", 3: "send queue full", 4: "message too large",
                5: "empty message"}
FILE_MAGIC = b"CDPFILE\0"


def _env(name, default):
    return os.environ.get(name, default)


class ArqClient:
    def __init__(self, host=None, port=None, timeout=5.0):
        host = host or _env("CDP_HOST", "127.0.0.1")
        port = int(port or _env("CDP_ARQ_PORT", "5100"))
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(None)
        self._buf = bytearray()
        self._pending = []
        self._ref = random.getrandbits(31)
        body = self._read(timeout)
        if body is None or body[0] != 0x80:
            raise ConnectionError("no HELLO from the ARQ block")
        _, self.node, self.mss, self.window, self.max_msg = struct.unpack(">BBHBI", body[:9])

    def close(self):
        self.sock.close()

    def send(self, dest, data, ref=None):
        if ref is None:
            self._ref = (self._ref + 1) & 0xFFFFFFFF
            ref = self._ref
        body = struct.pack(">BIB", 0x01, ref, dest) + bytes(data)
        self.sock.sendall(len(body).to_bytes(4, "big") + body)
        return ref

    def _read(self, timeout):
        """One body, or None on timeout. Raises ConnectionError if the block closed the socket."""
        end = None if timeout is None else time.monotonic() + timeout
        while True:
            if len(self._buf) >= 4:
                n = int.from_bytes(self._buf[:4], "big")
                if len(self._buf) >= 4 + n:
                    body = bytes(self._buf[4:4 + n])
                    del self._buf[:4 + n]
                    return body
            left = None if end is None else end - time.monotonic()
            if left is not None and left <= 0:
                return None
            r, _, _ = select.select([self.sock], [], [], left)
            if not r:
                return None
            chunk = self.sock.recv(262144)
            if not chunk:
                raise ConnectionError("ARQ block closed the connection")
            self._buf += chunk

    @staticmethod
    def _decode(body):
        t = body[0]
        if t == 0x81:
            return ("recv", body[1], body[2:])
        if t == 0x82:
            ref, dest = struct.unpack(">IB", body[1:6])
            return ("sent", ref, dest)
        if t == 0x83:
            ref, dest, reason = struct.unpack(">IBB", body[1:7])
            return ("fail", ref, dest, reason)
        return ("other", body)

    def next_event(self, timeout=None):
        if self._pending:
            return self._pending.pop(0)
        body = self._read(timeout)
        return None if body is None else self._decode(body)

    def events(self, timeout=None):
        while True:
            ev = self.next_event(timeout)
            if ev is None:
                return
            yield ev

    def wait(self, ref, timeout=None):
        """Wait for SENT/FAIL of ref; other events are kept for next_event()/events()."""
        end = None if timeout is None else time.monotonic() + timeout
        while True:
            left = None if end is None else max(0.0, end - time.monotonic())
            body = self._read(left)
            if body is None:
                return None
            ev = self._decode(body)
            if ev[0] in ("sent", "fail") and ev[1] == ref:
                return ev
            self._pending.append(ev)


class UdpClient:
    def __init__(self, host=None, port=None, out_port=None, bind=None):
        self.addr = (host or _env("CDP_HOST", "127.0.0.1"), int(port or _env("CDP_UDP_PORT", "5200")))
        self.out = (bind or _env("CDP_UDP_BIND", "127.0.0.1"), int(out_port or _env("CDP_UDP_OUT_PORT", "5201")))
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx = None

    def send(self, dest, data):
        self.tx.sendto(bytes([dest & 0xFF]) + bytes(data), self.addr)

    def recv(self, timeout=None):
        if self.rx is None:
            self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.rx.bind(self.out)
        self.rx.settimeout(timeout)
        try:
            d, _ = self.rx.recvfrom(65535)
        except socket.timeout:
            return None
        return d[0], d[1], d[2:]


def _show(data):
    try:
        t = data.decode("utf-8")
        if t.isprintable() or "\n" in t:
            return repr(t) if len(t) <= 200 else repr(t[:200]) + f"... ({len(data)} B)"
    except UnicodeDecodeError:
        pass
    return f"{len(data)} B binary"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("arq-send", help="send one reliable message (text or file) and wait for SENT/FAIL")
    s.add_argument("--dest", type=int, required=True)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--text")
    g.add_argument("--file")
    s.add_argument("--timeout", type=float, default=600.0)
    s = sub.add_parser("arq-listen", help="print / save every reliable message received")
    s.add_argument("--out-dir", default=None, help="save files (and binary messages) here")
    s = sub.add_parser("udp-send", help="send one datagram")
    s.add_argument("--dest", type=int, required=True)
    s.add_argument("--text", required=True)
    sub.add_parser("udp-listen", help="print every datagram received")
    a = ap.parse_args()

    if a.cmd == "arq-send":
        c = ArqClient()
        if a.text is not None:
            data = a.text.encode()
        else:
            data = FILE_MAGIC + os.path.basename(a.file).encode() + b"\0" + open(a.file, "rb").read()
        print(f"node {c.node} -> node {a.dest}: {len(data)} B ({-(-len(data) // c.mss)} segments of {c.mss} B, "
              f"window {c.window})", flush=True)
        t0 = time.monotonic()
        ev = c.wait(c.send(a.dest, data), a.timeout)
        dt = time.monotonic() - t0
        if ev is None:
            print(f"no answer within {a.timeout:.0f} s (still queued in the block)")
            sys.exit(2)
        if ev[0] == "sent":
            print(f"SENT: confirmed by node {ev[2]} after {dt:.1f} s ({len(data) / dt / 1e3:.2f} kB/s)")
        else:
            print(f"FAIL after {dt:.1f} s: {FAIL_REASONS.get(ev[3], ev[3])}")
            sys.exit(1)
    elif a.cmd == "arq-listen":
        c = ArqClient()
        print(f"listening as node {c.node} (ARQ)", flush=True)
        n = 0
        for ev in c.events():
            if ev[0] != "recv":
                continue
            src, data = ev[1], ev[2]
            n += 1
            if data.startswith(FILE_MAGIC):
                name, _, body = data[len(FILE_MAGIC):].partition(b"\0")
                name = os.path.basename(name.decode("utf-8", "replace")) or f"file_{n}"
                out = os.path.expanduser(a.out_dir or ".")
                os.makedirs(out, exist_ok=True)
                path = os.path.join(out, name)
                with open(path, "wb") as f:
                    f.write(body)
                print(f"[{time.strftime('%H:%M:%S')}] from node {src}: file {name} ({len(body)} B) -> {path}", flush=True)
            else:
                print(f"[{time.strftime('%H:%M:%S')}] from node {src}: {_show(data)}", flush=True)
                if a.out_dir:
                    out = os.path.expanduser(a.out_dir)
                    os.makedirs(out, exist_ok=True)
                    with open(os.path.join(out, f"msg_{src}_{int(time.time())}_{n}.bin"), "wb") as f:
                        f.write(data)
    elif a.cmd == "udp-send":
        UdpClient().send(a.dest, a.text.encode())
        print(f"datagram to {a.dest} handed to the radio ({len(a.text.encode())} B)")
    elif a.cmd == "udp-listen":
        u = UdpClient()
        print(f"listening for datagrams on udp://{u.out[0]}:{u.out[1]}", flush=True)
        while True:
            r = u.recv(None)
            if r:
                src, dest, data = r
                to = "all" if dest == 255 else dest
                print(f"[{time.strftime('%H:%M:%S')}] from node {src} to {to}: {_show(data)}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except OSError as e:
        print(f"cannot reach the transport block: {e} (is the flowgraph running? check CDP_HOST / CDP_ARQ_PORT)")
        sys.exit(1)
