#!/usr/bin/env python3
"""
folder_sync_daemon.py - file transfer front-end for the CDP transceiver flowgraph.

Put this file next to the .grc / generated .py. It uses two folders beside it:
    outbox/   drop files here -> they are sent to the peer, then moved to outbox/sent/
              (or outbox/unconfirmed/ if the FSM can't report delivery - see below)
    inbox/    files received from the peer are written here

It talks to the flowgraph over the two ZMQ message blocks:
    daemon PUSH  --connect-->  tcp://127.0.0.1:TL_ZMQ_TX_PORT  (ZMQ PULL Message Source, bind)
    daemon PULL  --connect-->  tcp://127.0.0.1:TL_ZMQ_RX_PORT  (ZMQ PUSH Message Sink, bind)
Start order does not matter.

App frame sent into the FSM's app_in (what the FSM expects):
    [dst_addr:1][dst_port:1][media_type:1][reserved:1][length:4, big-endian] + body
    body = [name_len:2, big-endian][file name, utf-8][file bytes]

Destination: every file goes to ONE transport address/port (the "link"):
    TL_PEER_ADDR     address of the other node (default 0 = broadcast)
    TL_PEER_PORT     destination port (default 0 = any; the receiver only checks it
                     when its flowgraph has TL_LOCAL_PORT set to non-zero)
    link.json        optional, in the root folder: {"peer": 2, "port": 0}. Re-read
                     before every file, so it changes the destination without a
                     restart (cdp_chat.py's "Link" button writes it).

Retry policy (one message = one bounded attempt):
    - the FSM itself retries SYN / each DATA packet TL_MAX_RETRIES times (default 10);
      if that fails the message goes straight to outbox/failed/  (--max-attempts 1)
    - while the FSM is busy with another session ("tx_busy") the message waits, but
      only up to --max-age seconds in total (TL_MSG_MAX_AGE, default 60)
    - "no answer from the flowgraph" also counts as a failed attempt

Old messages are never resent automatically:
    - at startup, anything still queued in outbox/ from a previous run is moved to
      outbox/failed/  (use --keep-queue to send it instead)
    - a queued file older than --max-age seconds (TL_MSG_MAX_AGE, default 60) is
      moved to outbox/failed/ instead of being sent
    cdp_chat.py shows these as "not delivered" with a retry link (retry = fresh age).

Files the daemon keeps in the root folder:
    .daemon_status.json   heartbeat + current settings (read by cdp_chat.py)
    .rx_log.jsonl         one line per received file: who sent it (src addr/port)

Environment variables (same names the flowgraph reads, so one export sets both):
    TL_LOCAL_ADDR    this node's address (shown in status only)
    TL_ZMQ_TX_PORT   default 52001
    TL_ZMQ_RX_PORT   default 52002
    TL_RTO_MS        default 500   FSM retransmit timeout - used to size how long to wait
    TL_MAX_RETRIES   default 10    FSM retries per packet  - for the FSM's result
    TL_MTU           default 200   (only used for time estimates)
    TL_PREAMBLE      default 1024  (only used for time estimates)

Requires: python3-zmq (pyzmq) and GNU Radio's pmt module.
"""
import argparse
import json
import os
import random
import struct
import sys
import threading
import time
from pathlib import Path

try:
    import zmq
except ImportError:
    sys.exit("pyzmq missing: sudo apt install python3-zmq   (or: pip install pyzmq)")
try:
    import pmt
except ImportError:
    sys.exit("GNU Radio's pmt module missing: run this with the same python3 that runs GNU Radio")

MEDIA_BY_EXT = {
    ".txt": 0x01, ".md": 0x01, ".csv": 0x01, ".json": 0x01, ".log": 0x01,
    ".png": 0x02, ".jpg": 0x02, ".jpeg": 0x02, ".bmp": 0x02, ".gif": 0x02, ".webp": 0x02,
    ".wav": 0x03, ".mp3": 0x03, ".ogg": 0x03, ".flac": 0x03, ".m4a": 0x03,
}
BIT_RATE = 1.5e6 / 4          # samp_rate / sps for BPSK
MAX_PKTS = 0xFFFF             # total_pkts is a 16-bit field in the transport header


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ---------------------------------------------------------------- framing
def build_app_frame(dst_addr, name, data, dst_port=0):
    name_b = name.encode("utf-8")[:65535]
    body = struct.pack(">H", len(name_b)) + name_b + data
    media = MEDIA_BY_EXT.get(Path(name).suffix.lower(), 0x00)
    return struct.pack(">BBBBI", dst_addr & 0xFF, dst_port & 0xFF, media, 0, len(body)) + body


def parse_app_frame(frame):
    """Returns (name, data). Raises ValueError on anything malformed."""
    if len(frame) < 8:
        raise ValueError(f"frame too short ({len(frame)} B)")
    _dst, _port, _media, _res, length = struct.unpack_from(">BBBBI", frame, 0)
    body = frame[8:]
    if len(body) != length:
        raise ValueError(f"length field {length} != body {len(body)}")
    if len(body) < 2:
        raise ValueError("body too short")
    (nlen,) = struct.unpack_from(">H", body, 0)
    if 2 + nlen > len(body):
        raise ValueError("name length overruns body")
    name = body[2:2 + nlen].decode("utf-8", errors="replace")
    return name, body[2 + nlen:]


def meta_int(meta, key):
    """Integer field from the FSM's app_out metadata, or None."""
    try:
        if pmt.is_dict(meta) and pmt.dict_has_key(meta, pmt.intern(key)):
            return int(pmt.to_uint64(pmt.dict_ref(meta, pmt.intern(key), pmt.PMT_NIL)))
    except Exception:
        pass
    return None


def safe_name(name):
    name = Path(name.replace("\\", "/")).name.strip()   # drop any path parts
    name = "".join(c for c in name if c not in '<>:"|?*\x00')
    return name or f"rx_{time.strftime('%Y%m%d_%H%M%S')}.bin"


def unique_path(folder, name):
    p = folder / name
    stem, suf, i = p.stem, p.suffix, 1
    while p.exists():
        p = folder / f"{stem}_{i}{suf}"
        i += 1
    return p


def estimate_airtime_s(frame_len, mtu, preamble):
    n = max(1, -(-frame_len // mtu))
    per_pkt = (preamble + 4 + 6 + 18 + mtu + 4 + 160) * 8 / BIT_RATE   # +160 B postamble
    return n, n * per_pkt


# ---------------------------------------------------------------- daemon
class Daemon:
    def __init__(self, a):
        self.a = a
        self.outbox = a.root / "outbox"
        self.inbox = a.root / "inbox"
        self.sent = self.outbox / "sent"
        self.failed = self.outbox / "failed"
        self.unconfirmed = self.outbox / "unconfirmed"
        for d in (self.outbox, self.inbox, self.sent, self.failed, self.unconfirmed):
            d.mkdir(parents=True, exist_ok=True)

        ctx = zmq.Context.instance()
        self.tx = ctx.socket(zmq.PUSH)
        self.tx.setsockopt(zmq.LINGER, 0)
        self.tx.setsockopt(zmq.SNDHWM, 4)
        self.tx.connect(f"tcp://127.0.0.1:{a.tx_port}")
        self.rx = ctx.socket(zmq.PULL)
        self.rx.setsockopt(zmq.LINGER, 0)
        self.rx.connect(f"tcp://127.0.0.1:{a.rx_port}")

        self.events = []                  # tx_done / tx_failed / tx_busy from the FSM
        self.ev_cond = threading.Condition()
        self.events_seen = False          # True once the FSM shows it emits events
        self.stop = False
        self.link_file = a.root / "link.json"
        self.status_file = a.root / ".daemon_status.json"
        self.rx_log = a.root / ".rx_log.jsonl"
        self._last_status = 0
        self._status_lock = threading.Lock()
        self._link_err = None

    # -- destination ------------------------------------------------------
    def current_link(self):
        """(peer_addr, peer_port): link.json if present and valid, else command line/env."""
        peer, port = self.a.peer, self.a.port
        if self.link_file.exists():
            try:
                d = json.loads(self.link_file.read_text())
                peer = int(d.get("peer", peer))
                port = int(d.get("port", port))
                if not (0 <= peer <= 255 and 0 <= port <= 255):
                    raise ValueError("peer/port must be 0-255")
                self._link_err = None
            except Exception as e:
                if str(e) != self._link_err:
                    log(f"[LINK] ignoring bad link.json ({e}); using peer {self.a.peer} port {self.a.port}")
                    self._link_err = str(e)
                peer, port = self.a.peer, self.a.port
        return peer, port

    def heartbeat_loop(self):
        # separate thread: send_file() can block for 15 s+ waiting for the FSM, and the
        # chat app treats a heartbeat older than 8 s as "daemon not detected"
        while not self.stop:
            self.write_status()
            time.sleep(1.0)

    def write_status(self, force=False):
        with self._status_lock:
            self._write_status(force)

    def _write_status(self, force):
        now = time.time()
        if not force and now - self._last_status < 2:
            return
        self._last_status = now
        peer, port = self.current_link()
        st = {"pid": os.getpid(), "ts": now, "local_addr": self.a.local_addr,
              "local_port": self.a.local_port, "peer": peer, "port": port,
              "zmq_tx": self.a.tx_port, "zmq_rx": self.a.rx_port,
              "confirmations": self.events_seen}
        try:
            tmp = self.status_file.with_name(self.status_file.name + ".part")
            tmp.write_text(json.dumps(st))
            tmp.replace(self.status_file)
        except OSError:
            pass

    # -- receive side -----------------------------------------------------
    def rx_loop(self):
        while not self.stop:
            if not self.rx.poll(200, zmq.POLLIN):
                continue
            raw = self.rx.recv()
            try:
                msg = pmt.deserialize_str(raw)
            except Exception as e:
                log(f"[RX] could not deserialize message from flowgraph: {e}")
                continue
            meta = pmt.car(msg) if pmt.is_pair(msg) else pmt.PMT_NIL
            data = pmt.cdr(msg) if pmt.is_pair(msg) else msg

            if pmt.is_dict(meta) and pmt.dict_has_key(meta, pmt.intern("event")):
                ev = pmt.symbol_to_string(pmt.dict_ref(meta, pmt.intern("event"), pmt.intern("?")))
                with self.ev_cond:
                    self.events_seen = True
                    self.events.append(ev)
                    self.ev_cond.notify_all()
                continue

            if not pmt.is_u8vector(data):
                log("[RX] message without a byte payload - ignored")
                continue
            frame = bytes(pmt.u8vector_elements(data))
            src = {k: meta_int(meta, k) for k in ("src_addr", "src_port", "dst_addr", "dst_port")}
            try:
                name, content = parse_app_frame(frame)
                path = unique_path(self.inbox, safe_name(name))
            except ValueError as e:
                path = unique_path(self.inbox, f"rx_{time.strftime('%Y%m%d_%H%M%S')}.bin")
                content = frame
                log(f"[RX] unrecognised frame ({e}); saving raw bytes")
            tmp = path.with_name(path.name + ".part")
            tmp.write_bytes(content)
            tmp.replace(path)
            frm = (f"from addr {src['src_addr']} port {src['src_port']}"
                   if src["src_addr"] is not None else "from unknown sender")
            log(f"[RX] received {path.name} ({len(content)} B) {frm} -> {path}")
            try:
                with open(self.rx_log, "a") as f:
                    f.write(json.dumps({"file": path.name, "ts": time.time(), **src}) + "\n")
            except OSError:
                pass

    # -- transmit side ----------------------------------------------------
    def ready_files(self, seen):
        """Files whose size+mtime have not changed since the previous scan."""
        out = []
        for p in sorted(self.outbox.iterdir(), key=lambda p: p.stat().st_mtime):
            if not p.is_file() or p.name.startswith(".") or p.name.endswith((".part", "~", ".tmp")):
                continue
            st = p.stat()
            sig = (st.st_size, st.st_mtime)
            if seen.get(p.name) == sig:
                out.append(p)
            seen[p.name] = sig
        return out

    def wait_event(self, timeout):
        end = time.time() + timeout
        with self.ev_cond:
            while not self.events:
                left = end - time.time()
                if left <= 0:
                    return None
                self.ev_cond.wait(left)
            return self.events.pop(0)

    def clear_stale_queue(self):
        moved = 0
        for p in sorted(self.outbox.iterdir()):
            if p.is_file() and not p.name.startswith(".") and not p.name.endswith((".part", "~", ".tmp")):
                p.replace(unique_path(self.failed, p.name))
                moved += 1
        if moved:
            log(f"[TX] cleared {moved} message(s) left in outbox/ from a previous run -> outbox/failed/ "
                "(use --keep-queue to send them instead)")

    def _fail(self, p, why):
        log(f"[TX] {p.name}: {why} - moved to failed/")
        self.attempts.pop(p.name, None)
        p.replace(unique_path(self.failed, p.name))

    def _attempt_failed(self, p, why):
        self.attempts[p.name] = self.attempts.get(p.name, 0) + 1
        if self.attempts[p.name] >= self.a.max_attempts:
            self._fail(p, f"{why} (attempt {self.attempts[p.name]}/{self.a.max_attempts})")
            return
        back = random.uniform(2, 8)          # random back-off avoids both nodes colliding again
        log(f"[TX] {p.name}: {why} - retry {self.attempts[p.name]}/{self.a.max_attempts} in {back:.1f} s")
        time.sleep(back)

    def send_file(self, p):
        try:
            age = time.time() - p.stat().st_mtime
        except OSError:
            return
        if self.a.max_age > 0 and age > self.a.max_age:
            self._fail(p, f"waited {age:.0f} s (> {self.a.max_age:.0f} s) without being delivered")
            return
        data = p.read_bytes()
        peer, port = self.current_link()
        frame = build_app_frame(peer, p.name, data, dst_port=port)
        n_pkts, air = estimate_airtime_s(len(frame), self.a.mtu, self.a.preamble)
        if n_pkts > MAX_PKTS:
            log(f"[TX] {p.name}: {n_pkts} packets exceeds the 16-bit limit - moved to failed/")
            p.replace(unique_path(self.failed, p.name))
            return
        with self.ev_cond:
            self.events.clear()
        msg = pmt.cons(pmt.make_dict(), pmt.init_u8vector(len(frame), list(frame)))
        try:
            self.tx.send(pmt.serialize_str(msg), zmq.NOBLOCK)
        except zmq.Again:
            log("[TX] flowgraph not accepting messages yet (is it running?) - retrying")
            time.sleep(2)
            return
        log(f"[TX] {p.name}: {len(data)} B -> addr {peer} port {port}, {n_pkts} packets, ~{air:.1f} s air time minimum")

        # Wait as long as the FSM can possibly take: it always ends with tx_done/tx_failed.
        # Worst case: SYN phase, every DATA window, and the FIN phase each use all retries.
        rto = self.a.rto_ms / 1000.0
        windows = -(-n_pkts // 8)                        # SR window = 8 packets (TL_M=4)
        timeout = (self.a.fsm_retries + 2) * rto * (2 + windows) + 3 * air + 10
        ev = self.wait_event(timeout)
        if ev == "tx_done":
            log(f"[TX] {p.name}: delivered")
            self.attempts.pop(p.name, None)
            p.replace(unique_path(self.sent, p.name))
        elif ev == "tx_busy":
            # not an attempt: the FSM is busy with another session. Bounded by --max-age.
            back = random.uniform(1, 3)
            log(f"[TX] {p.name}: transport layer busy (another session in progress) - waiting {back:.1f} s")
            time.sleep(back)
        elif ev == "tx_failed":
            self._attempt_failed(p, "peer did not answer (FSM retries exhausted)")
        elif self.events_seen or self.a.assume_events:
            self._attempt_failed(p, f"no result from the flowgraph after {timeout:.0f} s "
                                    "(is it running? are TL_RTO_MS/TL_MAX_RETRIES the same as the flowgraph's?)")
        else:
            # The FSM in this flowgraph does not report completion (event patch not applied):
            # we cannot know whether it arrived. Park it so we don't resend forever.
            log(f"[TX] {p.name}: handed to flowgraph but delivery NOT confirmed "
                "(FSM sends no tx_done/tx_failed events - apply FSM fix E) -> outbox/unconfirmed/")
            p.replace(unique_path(self.unconfirmed, p.name))

    def run(self):
        threading.Thread(target=self.rx_loop, daemon=True).start()
        threading.Thread(target=self.heartbeat_loop, daemon=True).start()
        peer, port = self.current_link()
        log(f"folder_sync_daemon: outbox={self.outbox}  inbox={self.inbox}")
        log(f"this node addr={self.a.local_addr}  ->  sending to addr={peer} port={port}"
            + ("  (from link.json)" if self.link_file.exists() else ""))
        log(f"ZMQ: sending to :{self.a.tx_port}, receiving from :{self.a.rx_port}")
        if not self.a.keep_queue:
            self.clear_stale_queue()
        seen, self.attempts = {}, {}
        try:
            while True:
                self.write_status()
                for p in self.ready_files(seen):
                    if p.exists():
                        self.send_file(p)
                        self.write_status(force=True)
                time.sleep(1.0)
        except KeyboardInterrupt:
            log("stopping")
        finally:
            self.stop = True
            try:
                self.status_file.unlink()
            except OSError:
                pass


def main():
    here = Path(__file__).resolve().parent
    env = os.environ.get
    ap = argparse.ArgumentParser(description="Folder sync over the CDP transceiver flowgraph")
    ap.add_argument("--root", type=Path, default=here,
                    help="folder that holds outbox/ and inbox/ (default: this script's folder)")
    ap.add_argument("--peer", type=int, default=int(env("TL_PEER_ADDR", "0")),
                    help="transport address of the other node (TL_PEER_ADDR)")
    ap.add_argument("--port", type=int, default=int(env("TL_PEER_PORT", "0")),
                    help="destination port (TL_PEER_PORT, 0 = any)")
    ap.add_argument("--local-addr", type=int, default=int(env("TL_LOCAL_ADDR", "0")),
                    help="this node's address, for display (TL_LOCAL_ADDR)")
    ap.add_argument("--local-port", type=int, default=int(env("TL_LOCAL_PORT", "0")),
                    help="this node's port, for display (TL_LOCAL_PORT)")
    ap.add_argument("--tx-port", type=int, default=int(env("TL_ZMQ_TX_PORT", "52001")))
    ap.add_argument("--rx-port", type=int, default=int(env("TL_ZMQ_RX_PORT", "52002")))
    ap.add_argument("--mtu", type=int, default=int(env("TL_MTU", "200")))
    ap.add_argument("--preamble", type=int, default=int(env("TL_PREAMBLE", "1024")))
    ap.add_argument("--rto-ms", type=float, default=float(env("TL_RTO_MS", "500")),
                    help="the flowgraph's FSM RTO (TL_RTO_MS) - sizes the wait for tx_done/tx_failed")
    ap.add_argument("--fsm-retries", type=int, default=int(env("TL_MAX_RETRIES", "10")),
                    help="the flowgraph's FSM retries (TL_MAX_RETRIES)")
    ap.add_argument("--legacy-fsm", dest="assume_events", action="store_false",
                    help="FSM without tx_done/tx_failed events: park unconfirmed sends instead of failing them")
    ap.add_argument("--max-attempts", type=int, default=int(env("TL_MSG_ATTEMPTS", "1")),
                    help="FSM sessions per message before giving up (TL_MSG_ATTEMPTS, default 1; "
                         "each session already retries TL_MAX_RETRIES times)")
    ap.add_argument("--max-age", type=float, default=float(env("TL_MSG_MAX_AGE", "60")),
                    help="seconds a queued file may wait before it is moved to failed/ (0 = never)")
    ap.add_argument("--keep-queue", action="store_true",
                    help="send files left in outbox/ from a previous run instead of clearing them")
    a = ap.parse_args()
    a.root = a.root.resolve()
    Daemon(a).run()


if __name__ == "__main__":
    main()
