#!/usr/bin/env python3
"""
cdp_daemon.py - the one program to start for the CDP link (Linux and macOS).

    python3 cdp_daemon.py                  # login (your node number) -> GNU Radio Companion -> chat
    python3 cdp_daemon.py --node 1         # the login starts with node 1 (or export CDP_MY_ID=1)
    python3 cdp_daemon.py --direct         # run the generated flowgraph script without the GRC window
    python3 cdp_daemon.py --shutdown       # ask a running daemon to close everything (used by other apps)

What it does
  1. creates debug/ next to this file and a session folder debug/<date>_<time>_node<N>/ for this run
  2. login: only this PC's node number (its address on the link), pre-filled from --node, else $CDP_MY_ID, else
     the number used last time here, else one derived from the computer's name (no list of the other nodes)
  3. opens the flowgraph (v6.9_cdp_link.grc next to this file, or --grc FILE) in GNU Radio Companion with the
     right settings (CDP_MY_ID, CDP_DEST=255, CDP_DEBUG_DIR, ...): press Run there if it does not start by itself
  4. chat window: the other nodes are found by themselves. Every daemon broadcasts a small "presence" message
     every CDP_PRESENCE_S s (5); a node that is heard (presence or any message) gets its own chat at once.
     Text / images / files over the reliable link; the chat starts empty every time; received files go to
     received/<session>/
  5. encryption (cdp_crypto.py): every PC has a key pair (private x, public g^x mod p over a 2048-bit prime);
     the public key rides in the presence message; text, files and voice to a node are encrypted with the key
     both sides compute from it (sidebar: "🔒 Encryption: on/off"; fingerprints in "Nodes in this session")
  6. voice (cdp_voice.py): "🎙 Talk" = live voice over the datagram service, on until clicked again; "🎤 Voice msg" =
     record, then send as a reliable chat item. The daemon owns CDP_UDP_OUT_PORT and forwards the game's
     datagrams to CDP_GAME_UDP_PORT (5202), where it tells the game to listen.
  7. "📊 Performance dashboard" (cdp_dashboard.py): live charts of this link in a window of this app, from the
     session logs the flowgraph writes (v6.9.3; the web page of v6.9 is gone)
  8. the login screen also chooses the modulation (BPSK or QPSK, env CDP_MOD / --mod); both PCs must use the same
  9. "Close everything" (or closing the window, or --shutdown from another program) stops the game, the
     flowgraph, GNU Radio Companion and this daemon

Logs in the session folder (see LOGS.md): daemon.log, session.json, chat.csv - plus everything the flowgraph's
Session Logger writes there (csma_*, arq_*, udp.csv, msgs.csv, phy_1s.csv, events.log, flowgraph.json).

Environment (optional): CDP_MY_ID (this node), CDP_PRESENCE_S (5), CDP_GRC (flowgraph file), CDP_GRC_CMD (default gnuradio-companion), CDP_GAME_CMD (command
that starts the game), CDP_ARQ_PORT 5100, CDP_UDP_PORT 5200, CDP_UDP_OUT_PORT 5201, CDP_CTL_PORT 5300,
TL_IMG_MAX_PX 800, TL_IMG_QUALITY 70. Needs PyQt5 (comes with GNU Radio's Qt GUI).
"""
import argparse
import csv
import datetime
import glob
import html
import json
import os
import platform
import queue
import random
import select
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cdp_crypto                                         # noqa: E402  (key pair over a prime, message encryption)
import cdp_metrics                                        # noqa: E402  (performance dashboard: the data)
import cdp_voice                                          # noqa: E402  (push-to-talk, voice messages)
ENV = os.environ.get
ARQ_PORT = int(ENV("CDP_ARQ_PORT", "5100"))
UDP_PORT = int(ENV("CDP_UDP_PORT", "5200"))
UDP_OUT_PORT = int(ENV("CDP_UDP_OUT_PORT", "5201"))
GAME_UDP_PORT = int(ENV("CDP_GAME_UDP_PORT", "5202"))     # the daemon forwards the game's datagrams here
CTL_PORT = int(ENV("CDP_CTL_PORT", "5300"))
IMG_MAX_PX = int(ENV("TL_IMG_MAX_PX", "800"))
IMG_QUALITY = int(ENV("TL_IMG_QUALITY", "70"))
MAGIC = b"CDPC2\0"
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}
TEXT_EXT = {".txt", ".md", ".csv", ".json", ".log", ".py", ".c", ".h", ".cpp", ".m", ".ini", ".cfg", ".yaml",
            ".yml", ".xml", ".html", ".tex", ".grc"}
FAIL_REASONS = {1: "no ACK after all retries", 2: "bad destination", 3: "send queue full", 4: "message too large",
                5: "empty message", 6: "link not connected", 7: "no encryption key from that node"}
MAX_OPPONENTS = 8
PRESENCE_S = float(ENV("CDP_PRESENCE_S", "5"))      # presence broadcast period (s)
ONLINE_S = 3 * PRESENCE_S + 3                       # a node not heard for this long is shown as away


def node_name(n):
    return f"Node {n}"


def file_kind(name):
    ext = Path(str(name)).suffix.lower()
    return "image" if ext in IMAGE_EXT else "text" if ext in TEXT_EXT else "file"


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


# ======================================================================================== session / logs
class Session:
    """debug/<date>_<time>[_node<N>]/ with daemon.log, session.json and chat.csv."""

    CHAT_COLS = ["t", "dir", "peer", "id", "kind", "name", "bytes", "ref", "dur_s", "kBps", "age_s", "res"]

    def __init__(self, root=HERE):
        self.root = Path(root)
        self.debug = self.root / "debug"
        self.debug.mkdir(exist_ok=True)
        self.stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.dir = self.debug / self.stamp
        self.dir.mkdir(exist_ok=True)
        self.t0 = time.time()
        self._lock = threading.Lock()
        self._log = open(self.dir / "daemon.log", "a", buffering=1)
        self._chat = None
        self.received = None
        self.info = {"t0": round(self.t0, 3), "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "host": socket.gethostname(), "os": f"{platform.system()} {platform.release()}",
                     "python": sys.version.split()[0], "daemon": "cdp_daemon v6.9"}
        self.log(f"session folder {self.dir}")

    def rel(self, t=None):
        return round((t if t is not None else time.time()) - self.t0, 3)

    def log(self, msg):
        line = f"{self.rel():9.3f} {msg}"
        with self._lock:
            try:
                self._log.write(line + "\n")
            except ValueError:
                pass
        print(f"[daemon] {msg}", flush=True)

    def login(self, me, opponents, extra):
        """Rename the folder to <stamp>_node<me>, write session.json, open chat.csv."""
        new = self.debug / f"{self.stamp}_node{me}"
        with self._lock:
            self._log.close()
            try:
                self.dir.rename(new)
                self.dir = new
            except OSError:
                pass
            self._log = open(self.dir / "daemon.log", "a", buffering=1)
        self.info.update({"node": me, "opponents": opponents, **extra})
        self.write_info()
        self._chat = open(self.dir / "chat.csv", "w", newline="", buffering=1)
        self._chat_w = csv.writer(self._chat)
        self._chat_w.writerow(self.CHAT_COLS)
        self.received = self.root / "received" / self.dir.name
        self.received.mkdir(parents=True, exist_ok=True)
        self.log(f"start: node {me}; the other nodes are found by their presence messages; logs in {self.dir}")

    def write_info(self):
        try:
            (self.dir / "session.json").write_text(json.dumps(self.info, indent=1))
        except OSError:
            pass

    def chat_row(self, **kw):
        if self._chat is None:
            return
        kw["t"] = self.rel(kw.get("t"))
        with self._lock:
            self._chat_w.writerow([kw.get(c, "") for c in self.CHAT_COLS])

    def close(self):
        self.info["ended"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.write_info()
        with self._lock:
            for f in (self._chat, self._log):
                try:
                    if f:
                        f.close()
                except Exception:
                    pass


# ======================================================================================== processes
class Procs:
    """Starts the flowgraph / GRC / game in their own process groups and stops them all."""

    def __init__(self, session):
        self.s = session
        self.procs = {}           # name -> Popen
        self.logs = {}

    def start(self, name, argv, env, cwd=None, echo=False):
        """echo=True: the output also goes to this terminal (it always goes to debug/<session>/<name>.log)"""
        self.stop(name)
        logf = open(self.s.dir / f"{name}.log", "w", buffering=1)
        self.logs[name] = logf
        try:
            p = subprocess.Popen(argv, env=env, cwd=str(cwd or HERE),
                                 stdout=subprocess.PIPE if echo else logf, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            self.s.log(f"cannot start {name}: {argv} ({e})")
            return None
        self.procs[name] = p
        if echo:
            threading.Thread(target=self._tee, args=(p.stdout, logf), name=f"{name}-output", daemon=True).start()
        self.s.log(f"started {name} (pid {p.pid}): {' '.join(shlex.quote(a) for a in argv)}"
                   + (f" - its output: this terminal and {logf.name}" if echo else f" - its output: {logf.name}"))
        return p

    @staticmethod
    def _tee(pipe, logf):
        """copy the child's output line by line to its log file and to this terminal"""
        for raw in iter(pipe.readline, b""):
            line = raw.decode("utf-8", "replace")
            try:
                logf.write(line)
            except ValueError:                      # log closed (process stopped)
                pass
            try:
                sys.stdout.write(line)
                sys.stdout.flush()
            except (OSError, ValueError):           # no terminal
                pass
        try:
            pipe.close()
        except OSError:
            pass

    def running(self, name):
        p = self.procs.get(name)
        return p is not None and p.poll() is None

    def stop(self, name, timeout=4.0):
        p = self.procs.pop(name, None)
        if p is None:
            return
        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)        # GRC and the flowgraph it started share this group
            except (ProcessLookupError, PermissionError, OSError):
                p.terminate()
            try:
                p.wait(timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    p.kill()
                p.wait(2)
        self.s.log(f"stopped {name} (exit {p.returncode})")
        f = self.logs.pop(name, None)
        if f:
            f.close()

    def stop_all(self):
        for name in ("game", "flowgraph", "grc"):
            self.stop(name)
        for name in list(self.procs):
            self.stop(name)


def find_grc(arg):
    if arg:
        return Path(arg).resolve()
    if ENV("CDP_GRC"):
        return Path(ENV("CDP_GRC")).resolve()
    cands = sorted(HERE.glob("v6.9*cdp_link*.grc")) or sorted(HERE.glob("*.grc"))
    return cands[-1] if cands else None


def generated_script(grc):
    """GRC writes <options id>.py next to the .grc when it runs it."""
    gid = "cdp_link"
    try:
        import re
        txt = Path(grc).read_text(errors="replace")
        m = re.search(r"^options:\s*\n\s+parameters:\s*\n(?:\s+.*\n)*?\s+id:\s*(\S+)", txt, re.M)
        if m:
            gid = m.group(1).strip("'\"")
    except OSError:
        pass
    p = grc.parent / f"{gid}.py"
    return p if p.exists() else None


def find_game():
    cmd = ENV("CDP_GAME_CMD")
    if cmd:
        return shlex.split(cmd)
    for pat in ("cdp_game*.py", "game*.py", "*shooter*.py"):
        c = sorted(HERE.glob(pat))
        if c:
            return [sys.executable, "-u", str(c[0])]
    return None


# ======================================================================================== link to the flowgraph
class Link:
    """TCP client of the flowgraph's App Socket Bridge. Events go into self.events (queue)."""

    def __init__(self, session, host="127.0.0.1", port=ARQ_PORT):
        self.s = session
        self.host, self.port = host, port
        self.events = queue.Queue()
        self.sock = None
        self.hello = None
        self._wlock = threading.Lock()
        self._stop = threading.Event()
        self._ref = random.getrandbits(31)
        self.tx_bytes = 0.0        # reliable bytes delivered (acknowledged), counted as the progress reports come in
        self.rx_bytes = 0          # reliable bytes received
        self._out = {}             # ref -> [bytes, segments acknowledged so far]
        self._thr = threading.Thread(target=self._run, name="link", daemon=True)

    def start(self):
        self._thr.start()

    def stop(self):
        self._stop.set()
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass

    @property
    def connected(self):
        return self.sock is not None and self.hello is not None

    def send(self, dest, data):
        """Returns the ref, or None if not connected."""
        if not self.connected:
            return None
        self._ref = (self._ref + 1) & 0xFFFFFFFF
        body = struct.pack(">BIB", 0x01, self._ref, dest & 0xFF) + data
        try:
            with self._wlock:
                self.sock.sendall(len(body).to_bytes(4, "big") + body)
        except OSError:
            return None
        self._out[self._ref] = [len(data), 0]
        if len(self._out) > 2000:
            self._out.pop(next(iter(self._out)))
        return self._ref

    def _run(self):
        warned = 0.0
        while not self._stop.is_set():
            try:
                sock = socket.create_connection((self.host, self.port), timeout=2.0)
            except OSError:
                if time.time() - warned > 30:
                    warned = time.time()
                    self.s.log(f"link: no flowgraph on tcp://{self.host}:{self.port} yet (press Run in GRC)")
                self.events.put(("state", "waiting"))
                self._stop.wait(1.0)
                continue
            sock.settimeout(None)
            self.sock = sock
            buf = bytearray()
            try:
                while not self._stop.is_set():
                    r, _, _ = select.select([sock], [], [], 0.5)
                    if not r:
                        continue
                    chunk = sock.recv(1 << 20)
                    if not chunk:
                        break
                    buf += chunk
                    while len(buf) >= 4:
                        n = int.from_bytes(buf[:4], "big")
                        if len(buf) < 4 + n:
                            break
                        body = bytes(buf[4:4 + n])
                        del buf[:4 + n]
                        self._decode(body)
            except OSError:
                pass
            self.sock, self.hello = None, None
            try:
                sock.close()
            except OSError:
                pass
            self.s.log("link: flowgraph connection closed")
            self.events.put(("state", "lost"))
            self._stop.wait(1.0)

    def _decode(self, b):
        t = b[0]
        if t == 0x80 and len(b) >= 9:
            _, node, mss, window, max_msg = struct.unpack(">BBHBI", b[:9])
            self.hello = {"node": node, "mss": mss, "window": window, "max_msg": max_msg}
            self.s.log(f"link: connected to the flowgraph (node {node}, mss {mss}, window {window})")
            self.events.put(("hello", self.hello))
        elif t == 0x81 and len(b) >= 2:
            self.rx_bytes += len(b) - 2
            self.events.put(("recv", b[1], b[2:], time.time()))
        elif t == 0x82 and len(b) >= 6:
            ref, dest = struct.unpack(">IB", b[1:6])
            o = self._out.pop(ref, None)
            if o is not None:
                self.tx_bytes += max(0.0, o[0] - o[1])
            self.events.put(("sent", ref, dest, time.time()))
        elif t == 0x83 and len(b) >= 7:
            ref, dest, reason = struct.unpack(">IBB", b[1:7])
            self._out.pop(ref, None)
            self.events.put(("fail", ref, dest, reason, time.time()))
        elif t == 0x84 and len(b) >= 10:
            ref, dest, acked, total = struct.unpack(">IBHH", b[1:10])
            o = self._out.get(ref)
            if o is not None and total > 0:
                done = o[0] * min(acked, total) / total
                if done > o[1]:
                    self.tx_bytes += done - o[1]
                    o[1] = done
            self.events.put(("progress", ref, acked, total, time.time()))


# ======================================================================================== chat model (no Qt)
class Msg:
    __slots__ = ("id", "ts", "peer", "mine", "kind", "text", "name", "size", "path", "status", "ref", "t_queue",
                 "extra", "data")

    def __init__(self, **kw):
        self.text, self.name, self.size, self.path, self.ref, self.extra, self.data = "", "", 0, None, None, {}, None
        for k, v in kw.items():
            setattr(self, k, v)


def encode(header, data=b""):
    h = json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode()
    return MAGIC + len(h).to_bytes(2, "big") + h + data


def decode(raw):
    """(header dict, data) or (None, raw) if it is not a chat message."""
    if not raw.startswith(MAGIC) or len(raw) < len(MAGIC) + 2:
        return None, raw
    n = int.from_bytes(raw[len(MAGIC):len(MAGIC) + 2], "big")
    try:
        h = json.loads(raw[len(MAGIC) + 2:len(MAGIC) + 2 + n].decode())
    except Exception:
        return None, raw
    return h, raw[len(MAGIC) + 2 + n:]


class ChatCore:
    """Chats with every node heard on the link. Nodes are discovered: each daemon broadcasts a presence message
    (chat format, kind 'presence') every PRESENCE_S s, and answers a newly heard node at once."""

    def __init__(self, session, link, me, opponents=(), identity=None):
        self.s, self.link, self.me = session, link, me
        self.ident = identity                            # cdp_crypto.Identity (None: no encryption at all)
        self.keys = {}                                   # node -> cdp_crypto.PeerKey from its presence
        self.encrypt_on = identity is not None and ENV("CDP_ENCRYPT", "1") != "0"
        self.wait_key = {}                               # node -> [(Msg, payload)] waiting for its public key
        self.cstats = {"sealed": 0, "opened": 0, "failed": 0, "plain_rx": 0}
        self.opponents = []                              # in the order they were first heard
        self.chats, self.last_heard, self.unread, self.first_heard, self.peer_info = {}, {}, {}, {}, {}
        self.by_ref = {}
        self.uid = uuid.uuid4().hex[:8]                  # tells two PCs with the same node number apart
        self.host = socket.gethostname()
        self.conflict = None                             # (host, time) of another PC using my node number
        self.presence_due = 0.0
        self.presence_sent = 0
        for p in opponents:
            self.add_peer(p, None)

    # ---- nodes
    def add_peer(self, p, t, info=None):
        if p in self.chats or p == self.me or not 0 <= p < 255:
            return False
        self.opponents.append(p)
        self.chats[p], self.last_heard[p], self.unread[p] = [], t, 0
        self.first_heard[p] = t
        if info:
            self.peer_info[p] = info
        how = f" ({info.get('host')})" if info and info.get("host") else ""
        self.s.log(f"node {p}{how} joined: new chat")
        self.s.info["opponents"] = list(self.opponents)
        self.s.info["peers"] = {str(q): {"first_heard_s": self.s.rel(self.first_heard[q]) if self.first_heard[q]
                                         else None, "host": self.peer_info.get(q, {}).get("host")}
                                for q in self.opponents}
        self.s.write_info()
        self.presence_due = min(self.presence_due, time.time() + random.uniform(0.2, 1.2))   # let it hear me soon
        return True

    def online(self, p, now=None):
        lh = self.last_heard.get(p)
        return lh is not None and (now or time.time()) - lh < ONLINE_S

    def tick(self, now):
        """Send my presence when due (only while the flowgraph is connected)."""
        if not self.link.connected or now < self.presence_due:
            return
        hdr = {"kind": "presence", "uid": self.uid, "host": self.host[:32], "ts": round(now, 3),
               "peers": self.opponents[:16], "v": 1}
        if self.ident is not None:
            hdr["pub"] = self.ident.public_hex()          # my public key g^x mod p (anyone may see it)
        if self.link.send(255, encode(hdr)) is not None:
            self.presence_sent += 1
        self.presence_due = now + PRESENCE_S * random.uniform(0.85, 1.15)

    def on_connected(self):
        self.presence_due = time.time() + random.uniform(0.1, 0.6)

    # ---- encryption
    def _learn_key(self, src, pub_hex):
        if self.ident is None or not pub_hex:
            return
        try:
            pub = int(pub_hex, 16)
        except (TypeError, ValueError):
            return
        old = self.keys.get(src)
        if old is not None and old.pub == pub:
            return
        try:
            pk = cdp_crypto.PeerKey(self.ident, self.me, pub, src)
        except ValueError:
            self.s.log(f"node {src} sent an invalid public key - ignored")
            return
        self.keys[src] = pk
        self.s.log(f"node {src}: public key {pk.fp_text}" + (" (CHANGED - it restarted with a new identity?)"
                                                              if old is not None else ""))
        self._flush_waiting(src)

    def _flush_waiting(self, peer):
        for m, payload in self.wait_key.pop(peer, []):
            if m.status == "key":
                self._transmit(m, payload)

    def set_encryption(self, on):
        self.encrypt_on = bool(on) and self.ident is not None
        self.s.log(f"encryption {'on' if self.encrypt_on else 'off'}")
        if not self.encrypt_on:                          # send what was waiting for a key, unencrypted
            for peer in list(self.wait_key):
                self._flush_waiting(peer)

    def key_for(self, peer):
        return self.keys.get(peer)

    def crypto_info(self):
        if self.ident is None:
            return None
        return {"on": self.encrypt_on, "method": "Diffie-Hellman over a 2048-bit prime + SHA-256 keystream + HMAC",
                "fingerprint": self.ident.fp_text, "peers_keyed": f"{len(self.keys)} of {len(self.opponents)}",
                "sealed": self.cstats["sealed"], "opened": self.cstats["opened"], "failed": self.cstats["failed"]}

    # ---- sending
    def send_text(self, peer, text):
        text = text.strip()
        if not text:
            return None
        m = Msg(id=uuid.uuid4().hex[:10], ts=time.time(), peer=peer, mine=True, kind="text", text=text,
                status="sending", size=len(text.encode()))
        self._send(m, {"id": m.id, "ts": round(m.ts, 3), "kind": "text", "text": text}, b"")
        return m

    def send_bytes(self, peer, name, data, text="", extra=None, kind=None):
        kind = kind or {"text": "doc"}.get(file_kind(name), file_kind(name))      # image / doc / file / voice
        m = Msg(id=uuid.uuid4().hex[:10], ts=time.time(), peer=peer, mine=True, kind=kind, text=text, name=name,
                size=len(data), status="sending", extra=dict(extra or {}))
        hdr = {"id": m.id, "ts": round(m.ts, 3), "kind": kind, "text": text, "name": name, "size": len(data),
               **m.extra}
        p = self.s.received / f"sent_{m.id}_{Path(name).name}" if self.s.received else None
        if p is not None:
            try:
                p.write_bytes(data)
                m.path = str(p)
                if kind == "voice":                          # keep a playable copy
                    wav = p.with_suffix(".wav")
                    cdp_voice.write_wav(wav, cdp_voice.decode_message(data))
                    m.extra["wav"] = str(wav)
            except (OSError, ValueError):
                pass
        self._send(m, hdr, data)
        return m

    def _send(self, m, hdr, data):
        self.chats[m.peer].append(m)
        payload = encode(hdr, data)
        if self.encrypt_on and m.peer not in self.keys:
            m.status = "key"                             # waits for the node's public key (its next presence)
            self.wait_key.setdefault(m.peer, []).append((m, payload))
            return
        self._transmit(m, payload)

    def _transmit(self, m, payload):
        if self.encrypt_on and m.peer in self.keys:
            payload = cdp_crypto.seal(self.keys[m.peer], payload)
            m.extra["enc"] = True
            self.cstats["sealed"] += 1
        m.status = "sending"
        m.t_queue = time.time()
        m.ref = self.link.send(m.peer, payload)
        if m.ref is None:
            m.status = "failed"
            m.extra["why"] = FAIL_REASONS[6]
            self.s.chat_row(dir="tx", peer=m.peer, id=m.id, kind=m.kind, name=m.name, bytes=len(payload),
                            res="fail:not connected")
        else:
            self.by_ref[m.ref] = (m, len(payload))
            self.s.chat_row(dir="tx", peer=m.peer, id=m.id, kind=m.kind, name=m.name, bytes=len(payload),
                            ref=m.ref, res="queued")

    def retry(self, m):
        if not m.mine or m.status != "failed":
            return
        self.chats[m.peer].remove(m)
        data = b""
        if m.kind != "text" and m.path:
            try:
                data = Path(m.path).read_bytes()
            except OSError:
                return
        if m.kind == "text":
            self.send_text(m.peer, m.text)
        else:
            self.send_bytes(m.peer, m.name, data, m.text,
                            {k: v for k, v in m.extra.items() if k in ("w", "h", "orig_size", "dur")},
                            kind="voice" if m.kind == "voice" else None)

    # ---- events from the link
    def on_event(self, ev):
        k = ev[0]
        if k == "progress":
            got = self.by_ref.get(ev[1])
            if got is None:
                return None
            m = got[0]
            m.extra["prog"] = (ev[2], ev[3], ev[4] - m.t_queue)
            return m.peer
        if k == "sent" or k == "fail":
            got = self.by_ref.pop(ev[1], None)
            if got is None:
                return None
            m, nbytes = got
            dur = ev[-1] - m.t_queue
            if k == "sent":
                m.status = "sent"
                res = "sent"
            else:
                m.status = "failed"
                m.extra["why"] = FAIL_REASONS.get(ev[3], str(ev[3]))
                res = "fail:" + m.extra["why"]
            self.s.chat_row(t=ev[-1], dir="tx", peer=m.peer, id=m.id, kind=m.kind, name=m.name, bytes=nbytes,
                            ref=m.ref, dur_s=round(dur, 3),
                            kBps=round(nbytes / max(dur, 1e-3) / 1e3, 2), res=res)
            return m.peer
        if k == "recv":
            src, raw, t = ev[1], ev[2], ev[3]
            enc = False
            if cdp_crypto.is_sealed(raw) and src != self.me:
                pk = self.keys.get(src)
                try:
                    if pk is None:
                        raise ValueError("no public key from this node yet")
                    raw = cdp_crypto.open_sealed(pk, raw)
                    enc = True
                    self.cstats["opened"] += 1
                except ValueError as e:
                    self.cstats["failed"] += 1
                    self.s.log(f"encrypted message from node {src} could not be opened: {e}")
                    self.add_peer(src, t)
                    self.last_heard[src] = t
                    self.chats[src].append(Msg(id=uuid.uuid4().hex[:10], ts=t, peer=src, mine=False, kind="text",
                                               text=f"🔒 an encrypted message could not be opened ({e})",
                                               status="received", extra={"sys": 1}))
                    self.unread[src] += 1
                    return src
            h, data = decode(raw)
            if src == self.me:                               # another PC uses my node number
                if h is not None and h.get("kind") == "presence" and h.get("uid") != self.uid:
                    if self.conflict is None:
                        self.s.log(f"WARNING: {h.get('host', 'another PC')} also uses node {self.me}")
                    self.conflict = (str(h.get("host", "another PC")), t)
                    return "conflict"
                return None
            new = self.add_peer(src, t, h if h is not None and h.get("kind") == "presence" else None)
            self.last_heard[src] = t
            if h is not None and h.get("kind") == "presence":
                self.peer_info[src] = {k2: h.get(k2) for k2 in ("host", "uid")}
                self._learn_key(src, h.get("pub"))
                if self.me not in (h.get("peers") or []):    # it hasn't heard me yet: answer soon
                    self.presence_due = min(self.presence_due, time.time() + random.uniform(0.2, 1.2))
                return src
            if h is None:
                try:
                    txt = raw.decode("utf-8")
                except UnicodeDecodeError:
                    return None                      # binary from another app (game): not for the chat
                if not txt.isprintable() or len(txt) > 2000:
                    return None
                h, data = {"id": uuid.uuid4().hex[:10], "ts": t, "kind": "text", "text": txt, "plain": 1}, b""
            if not enc:
                self.cstats["plain_rx"] += 1
            m = Msg(id=str(h.get("id")), ts=t, peer=src, mine=False, kind=h.get("kind", "text"),
                    text=str(h.get("text", ""))[:4000], name=str(h.get("name", "")), size=len(data) or
                    len(str(h.get("text", "")).encode()), status="received",
                    extra={k: v for k, v in h.items() if k in ("w", "h", "orig_size", "dur")})
            m.extra["enc"] = enc
            if m.kind != "text" and self.s.received is not None:
                safe = "".join(c for c in Path(m.name).name if c not in '<>:"/\\|?*\x00') or f"file_{m.id}"
                p = self.s.received / f"from{src}_{m.id}_{safe}"
                try:
                    p.write_bytes(data)
                    m.path = str(p)
                    if m.kind == "voice":
                        wav = p.with_suffix(".wav")
                        cdp_voice.write_wav(wav, cdp_voice.decode_message(data))
                        m.extra["wav"] = str(wav)
                except (OSError, ValueError) as e:
                    self.s.log(f"could not save {safe}: {e}")
            sent_ts = h.get("ts")
            self.chats[src].append(m)
            self.unread[src] += 1
            self.s.chat_row(t=t, dir="rx", peer=src, id=m.id, kind=m.kind, name=m.name, bytes=len(raw),
                            age_s=round(t - float(sent_ts), 3) if isinstance(sent_ts, (int, float)) else "",
                            res="received")
            return src
        return None


# ======================================================================================== control socket
def ctl_server(events, stop):
    """127.0.0.1:CTL_PORT - 'SHUTDOWN' closes everything (for the game's close button / other tools)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", CTL_PORT))
        s.listen(2)
    except OSError:
        return None

    def run():
        while not stop.is_set():
            r, _, _ = select.select([s], [], [], 0.5)
            if not r:
                continue
            try:
                c, _ = s.accept()
                c.settimeout(2.0)
                line = c.recv(256).decode(errors="replace").strip().upper()
                if line.startswith("SHUTDOWN"):
                    c.sendall(b"OK closing everything\n")
                    events.put(("shutdown", "control socket"))
                elif line.startswith("PING"):
                    c.sendall(b"OK\n")
                c.close()
            except OSError:
                pass
    threading.Thread(target=run, name="ctl", daemon=True).start()
    return s


def request_shutdown():
    try:
        with socket.create_connection(("127.0.0.1", CTL_PORT), timeout=2.0) as c:
            c.sendall(b"SHUTDOWN\n")
            print(c.recv(256).decode().strip() or "sent")
            return 0
    except OSError as e:
        print(f"no daemon on 127.0.0.1:{CTL_PORT} ({e})")
        return 1


# ======================================================================================== Qt user interface
try:
    from PyQt5.QtCore import Qt, QTimer, QUrl, QBuffer, QByteArray, QIODevice
    from PyQt5.QtGui import QDesktopServices, QFont, QBrush, QColor, QImage, QImageReader
    from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QLabel, QListWidget,
                                 QListWidgetItem, QPushButton, QTextBrowser, QPlainTextEdit, QFileDialog, QMessageBox,
                                 QFrame, QDialog, QFormLayout, QSpinBox, QDialogButtonBox, QRadioButton,
                                 QButtonGroup)
    HAVE_QT = True
except ImportError:
    HAVE_QT = False

CSS = """
#sidebar { background: #3F0E40; }
#sidebar QLabel { color: #CFC3CF; }
#title { color: white; font-size: 16px; font-weight: bold; }
#sidebar QListWidget { background: transparent; border: none; color: #CFC3CF; font-size: 14px; outline: 0; }
#sidebar QListWidget::item { padding: 5px 8px; border-radius: 4px; }
#sidebar QListWidget::item:selected { background: #1164A3; color: white; }
#sidebar QPushButton { color: #CFC3CF; background: transparent; border: none; text-align: left; padding: 4px 8px; }
#sidebar QPushButton:hover { color: white; }
#closeall { color: white; background: #B3261E; border-radius: 4px; padding: 6px 8px; font-weight: bold; }
#header { font-size: 15px; font-weight: bold; padding: 10px 16px; border-bottom: 1px solid #DDD; background: white; }
#view { border: none; background: white; padding: 4px 12px; }
#composerBox { border: 1px solid #BBB; border-radius: 8px; background: white; }
#composer { border: none; font-size: 14px; background: white; }
#send { background: #007A5A; color: white; border-radius: 4px; padding: 5px 14px; font-weight: bold; }
#send:disabled { background: #DDD; color: #999; }
#attach { border: none; font-size: 16px; padding: 4px 6px; background: transparent; }
#talk { background: #1164A3; color: white; border-radius: 4px; padding: 5px 10px; font-weight: bold; }
#talk:pressed, #talk[active="true"] { background: #B3261E; }
#talk:disabled, #vmsg:disabled { background: #DDD; color: #999; }
#vmsg { background: #E8E8E8; color: #1D1C1D; border-radius: 4px; padding: 5px 10px; }
#vmsg[active="true"] { background: #B3261E; color: white; }
#voiceLabel { color: #555; font-size: 12px; padding: 0 4px 4px 4px; }
"""

if HAVE_QT:
    class LoginDialog(QDialog):
        """Only this PC's node number (and how to start the flowgraph). The other nodes are found on the link."""

        def __init__(self, node, how, grc, can_direct, direct, mod="bpsk"):
            super().__init__()
            self.setWindowTitle("CDP Link - login")
            self.setMinimumWidth(380)
            lay = QVBoxLayout(self)
            info = QLabel("<b>Who are you on the link?</b><br>Your node number is your address; every PC needs its "
                          "own. The other nodes are found by themselves and each gets its own chat.")
            info.setWordWrap(True)
            lay.addWidget(info)
            form = QFormLayout()
            self.me = QSpinBox()
            self.me.setRange(0, 254)
            self.me.setValue(int(node))
            form.addRow("Your node number", self.me)
            lay.addLayout(form)
            hint = QLabel(f"<span style='color:#888'>suggested: {html.escape(how)}</span>")
            hint.setWordWrap(True)
            lay.addWidget(hint)
            self.mode_grc = QRadioButton(f"Open {grc.name if grc else 'the flowgraph'} in GNU Radio Companion")
            self.mode_direct = QRadioButton("Run the flowgraph directly (no GRC window)")
            g = QButtonGroup(self)
            g.addButton(self.mode_grc)
            g.addButton(self.mode_direct)
            self.mode_direct.setEnabled(can_direct)
            (self.mode_direct if (direct and can_direct) else self.mode_grc).setChecked(True)
            lay.addWidget(self.mode_grc)
            lay.addWidget(self.mode_direct)
            lay.addSpacing(6)
            lay.addWidget(QLabel("<b>Modulation</b> <span style='color:#888'>(every PC on the link must use the "
                                 "same)</span>"))
            self.mod_bpsk = QRadioButton("BPSK - robust, 250 kbit/s on the air at 1 MS/s")
            self.mod_qpsk = QRadioButton("QPSK - twice as fast (500 kbit/s), needs about 3 dB more signal")
            gm = QButtonGroup(self)
            gm.addButton(self.mod_bpsk)
            gm.addButton(self.mod_qpsk)
            (self.mod_qpsk if mod == "qpsk" else self.mod_bpsk).setChecked(True)
            lay.addWidget(self.mod_bpsk)
            lay.addWidget(self.mod_qpsk)
            bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            bb.button(QDialogButtonBox.Ok).setText("Log in")
            bb.accepted.connect(self.accept)
            bb.rejected.connect(self.reject)
            lay.addWidget(bb)

        def values(self):
            return self.me.value(), self.mode_direct.isChecked(), ("qpsk" if self.mod_qpsk.isChecked() else "bpsk")

    def compress_image(path, max_px=IMG_MAX_PX, quality=IMG_QUALITY):
        img = QImage(path)
        if img.isNull():
            return None
        ow, oh = img.width(), img.height()
        if max(ow, oh) > max_px:
            img = img.scaled(max_px, max_px, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        if img.hasAlphaChannel():
            img = img.convertToFormat(QImage.Format_RGB32)
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.WriteOnly)
        img.save(buf, "JPEG", quality)
        buf.close()
        return bytes(ba), img.width(), img.height(), ow, oh

    class Composer(QPlainTextEdit):
        def __init__(self, on_submit, **kw):
            super().__init__(**kw)
            self.on_submit = on_submit

        def keyPressEvent(self, e):
            if e.key() in (Qt.Key_Return, Qt.Key_Enter) and not (e.modifiers() & Qt.ShiftModifier):
                self.on_submit()
                return
            super().keyPressEvent(e)

    class ChatWindow(QMainWindow):
        def __init__(self, app_state):
            super().__init__()
            self.st = app_state
            self.core = app_state.core
            self.peer = self.core.opponents[0] if self.core.opponents else None
            self._closing = False
            self._list_t = 0.0
            self._voice_t = time.time()
            self._voice_bytes = (0, 0)
            self._voice_msg = ""
            self.setWindowTitle(f"CDP Link - {node_name(self.core.me)}")
            self.resize(1000, 680)
            self.setAcceptDrops(True)
            self.setStyleSheet(CSS)

            side = QWidget(objectName="sidebar")
            side.setFixedWidth(240)
            sl = QVBoxLayout(side)
            sl.setContentsMargins(12, 14, 12, 12)
            sl.addWidget(QLabel("CDP Link", objectName="title"))
            me = QLabel(f"● You: {node_name(self.core.me)} · {self.st.mod.upper()}")
            me.setStyleSheet("color:#2BAC76; font-weight:bold;")
            me.setToolTip(f"node number {self.st.node_how}; set it with  export CDP_MY_ID=N  or  --node N")
            sl.addWidget(me)
            sl.addSpacing(10)
            sl.addWidget(QLabel("Chats (nodes heard on the link)"))
            self.list = QListWidget()
            self.list.currentItemChanged.connect(self._pick)
            sl.addWidget(self.list, 1)
            b = QPushButton("👥  Nodes in this session")
            b.clicked.connect(self.show_nodes)
            sl.addWidget(b)
            self.game_btn = QPushButton("🎮  Start game")
            self.game_btn.clicked.connect(self.st.start_game)
            self.game_btn.setVisible(self.st.game_cmd is not None)
            sl.addWidget(self.game_btn)
            b = QPushButton("📊  Performance dashboard")
            b.setToolTip("Live charts of this link (a window of this app)")
            b.clicked.connect(self.open_dashboard)
            sl.addWidget(b)
            self.enc_btn = QPushButton("")
            self.enc_btn.clicked.connect(self.toggle_encryption)
            self.enc_btn.setVisible(self.core.ident is not None)
            sl.addWidget(self.enc_btn)
            b = QPushButton("📂  Open session logs")
            b.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.st.session.dir))))
            sl.addWidget(b)
            line = QFrame()
            line.setFrameShape(QFrame.HLine)
            sl.addWidget(line)
            self.link_label = QLabel("")
            self.link_label.setWordWrap(True)
            sl.addWidget(self.link_label)
            sl.addSpacing(6)
            close = QPushButton("⏻  Close everything", objectName="closeall")
            close.setToolTip("Stops the game, the flowgraph, GNU Radio Companion and this program")
            close.clicked.connect(self.close)
            sl.addWidget(close)

            main = QWidget()
            ml = QVBoxLayout(main)
            ml.setContentsMargins(0, 0, 0, 0)
            ml.setSpacing(0)
            self.header = QLabel("", objectName="header")
            ml.addWidget(self.header)
            self.view = QTextBrowser(objectName="view")
            self.view.setOpenLinks(False)
            self.view.anchorClicked.connect(self._link)
            ml.addWidget(self.view, 1)
            box = QWidget(objectName="composerBox")
            bl = QHBoxLayout(box)
            bl.setContentsMargins(6, 4, 6, 4)
            att = QPushButton("📎", objectName="attach")
            att.setToolTip("Send an image or file (or drag & drop onto the window)")
            att.clicked.connect(self.attach)
            bl.addWidget(att, 0, Qt.AlignBottom)
            self.composer = Composer(self.send, objectName="composer")
            self.composer.setFixedHeight(64)
            bl.addWidget(self.composer, 1)
            self.send_btn = QPushButton("Send", objectName="send")
            self.send_btn.clicked.connect(self.send)
            bl.addWidget(self.send_btn, 0, Qt.AlignBottom)
            self.talk_btn = QPushButton("🎙 Talk", objectName="talk")
            self.talk_btn.setToolTip("Click to start talking live to this chat's node - it stays on until you click "
                                     "again")
            self.talk_btn.clicked.connect(self.talk_toggle)
            bl.addWidget(self.talk_btn, 0, Qt.AlignBottom)
            self.vmsg_btn = QPushButton("🎤 Voice msg", objectName="vmsg")
            self.vmsg_btn.setToolTip("Click to record a voice message (up to 60 s), click again to send it")
            self.vmsg_btn.clicked.connect(self.voice_msg)
            bl.addWidget(self.vmsg_btn, 0, Qt.AlignBottom)
            wrap = QWidget()
            wl = QVBoxLayout(wrap)
            wl.setContentsMargins(16, 8, 16, 8)
            self.voice_label = QLabel("", objectName="voiceLabel")
            wl.addWidget(self.voice_label)
            wl.addWidget(box)
            ml.addWidget(wrap)
            root = QWidget()
            rl = QHBoxLayout(root)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(0)
            rl.addWidget(side)
            rl.addWidget(main, 1)
            self.setCentralWidget(root)

            self.timer = QTimer(self)
            self.timer.timeout.connect(self.poll)
            self.timer.start(120)
            self.refresh_list()
            self.render()

        # ---- link events
        def poll(self):
            changed = set()
            while True:
                try:
                    ev = self.st.events.get_nowait()
                except queue.Empty:
                    break
                if ev[0] == "shutdown":
                    self.st.session.log(f"shutdown requested ({ev[1]})")
                    self._closing = True
                    self.close()
                    return
                if ev[0] == "hello":
                    self.core.on_connected()
                    continue
                if ev[0] == "state":
                    continue
                n_before = len(self.core.opponents)
                p = self.core.on_event(ev)
                if p is not None and p != "conflict":
                    changed.add(p)
                    is_chat = ev[0] == "recv" and self.core.chats.get(p) and not self.core.chats[p][-1].mine \
                        and self.core.unread.get(p)
                    if is_chat and not self.isActiveWindow():
                        QApplication.alert(self)
                if len(self.core.opponents) != n_before and self.peer is None:
                    self.peer = self.core.opponents[0]                 # first node found: open its chat
                    self.render()
            now = time.time()
            self.core.tick(now)
            self.refresh_link()
            self.refresh_voice(now)
            if now - self._list_t > 2.0:                                # online / away marks age
                self._list_t = now
                changed.add(None)
            if changed:
                self.refresh_list()
                if self.peer in changed:
                    self.render()

        def refresh_link(self):
            h = self.st.link.hello
            fg = self.st.flowgraph_state()
            if h:
                warn = ""
                if h["node"] != self.core.me:
                    warn = (f"<br><span style='color:#ECB22E'>the flowgraph runs as node {h['node']}, not "
                            f"{self.core.me}: restart it from here</span>")
                n_on = sum(1 for p in self.core.opponents if self.core.online(p))
                txt = (f"Link: <span style='color:#2BAC76'>connected</span> (mss {h['mss']} B){warn}<br>"
                       f"{n_on} other node{'s' if n_on != 1 else ''} online")
            else:
                txt = f"Link: <span style='color:#ECB22E'>waiting for the flowgraph</span><br>{fg}"
            if self.core.ident is not None:
                on = self.core.encrypt_on
                self.enc_btn.setText(f"{'🔒  Encryption: on' if on else '🔓  Encryption: off'}")
                self.enc_btn.setToolTip(f"Click to switch {'off' if on else 'on'}. Your key fingerprint: "
                                        f"{self.core.ident.fp_text} - compare it with the other PC's "
                                        f"'Nodes in this session'.")
            c = self.core.conflict
            if c and time.time() - c[1] < 60:
                txt += (f"<br><span style='color:#FF6B6B'><b>{html.escape(c[0])} also uses node {self.core.me}!</b> "
                        f"Give each PC its own number (export CDP_MY_ID=N) and restart.</span>")
            self.link_label.setText(txt)

        # ---- dashboard, encryption, voice
        def open_dashboard(self):
            if self.st.metrics is None:
                QMessageBox.warning(self, "Dashboard", "No session logs yet (see daemon.log).")
                return
            if getattr(self, "_dash", None) is None:
                try:
                    import cdp_dashboard
                    self._dash = cdp_dashboard.DashboardWindow(
                        self.st.metrics, f"CDP Link - performance - {node_name(self.core.me)}")
                except Exception as e:
                    self.st.session.log(f"dashboard: {e}")
                    QMessageBox.warning(self, "Dashboard", f"The dashboard could not open: {e}")
                    return
            self._dash.show()
            self._dash.raise_()
            self._dash.activateWindow()

        def toggle_encryption(self):
            self.core.set_encryption(not self.core.encrypt_on)
            self.refresh_link()
            self.render()

        def talk_toggle(self):
            if self.st.voice.talking():
                self.talk_stop()
            else:
                self.talk_start()

        def talk_start(self):
            if self.peer is None:
                self._voice_msg = "no other node yet"
                return
            if not (self.st.hub and self.st.hub.ok):
                self._voice_msg = "live voice is off: " + (self.st.hub.error if self.st.hub else "no datagram port")
                return
            why = self.st.voice.start_talk(self.peer)
            self._voice_msg = why or ""
            if why is None:
                self.st.session.log(f"voice: talking to node {self.peer}")
            self._talk_button(why is None)

        def talk_stop(self):
            v = self.st.voice
            if v.talking():
                self.st.session.log(f"voice: stopped talking after {v.talk_seconds():.0f} s")
            v.stop_talk()
            self._talk_button(False)

        def _talk_button(self, on):
            self.talk_btn.setText("⏹ Stop talking" if on else "🎙 Talk")
            self.talk_btn.setProperty("active", bool(on))
            self.talk_btn.style().polish(self.talk_btn)

        def voice_msg(self):
            v = self.st.voice
            if v.rec_cap is None:
                if self.peer is None:
                    self._voice_msg = "no other node yet"
                    return
                why = v.start_record()
                self._voice_msg = why or ""
                if why is None:
                    self.vmsg_btn.setText("■ Stop & send")
                    self.vmsg_btn.setProperty("active", True)
            else:
                pcm = v.stop_record()
                self.vmsg_btn.setText("🎤 Voice msg")
                self.vmsg_btn.setProperty("active", False)
                secs = len(pcm) / (2 * cdp_voice.RATE)
                if secs >= 0.5:
                    blob = cdp_voice.encode_message(pcm)
                    self.core.send_bytes(self.peer, f"voice_{time.strftime('%H%M%S')}.cdpv", blob,
                                         extra={"dur": round(secs, 1)}, kind="voice")
                    self.render()
                else:
                    self._voice_msg = "too short - nothing sent"
            self.vmsg_btn.style().polish(self.vmsg_btn)

        def refresh_voice(self, now):
            v = self.st.voice
            if v is None:
                return
            parts = []
            for src, lvl, enc in v.active_talkers():
                bars = "▮" * int(1 + lvl * 7)
                parts.append(f"🔊 {node_name(src)} is talking {bars}{'  🔒' if enc else ''}")
            if v.talking():
                err = v.capture_error()
                if err:                                          # the microphone went away: switch off
                    self.talk_stop()
                    self._voice_msg = err
                else:
                    secs = int(v.talk_seconds())
                    parts.append(f"🎙 ON AIR to {node_name(v.dest)} {secs // 60}:{secs % 60:02d} "
                                 f"{'▮' * int(1 + v.mic_level * 7)}  (click ⏹ Stop talking to end)")
            if v.rec_cap is not None:
                parts.append(f"⏺ recording a voice message {v.recording_s():.1f} s (max {cdp_voice.MAX_MSG_S:.0f} s)")
                if v.recording_s() >= cdp_voice.MAX_MSG_S:
                    self.voice_msg()
            if self._voice_msg:
                parts.append(f"⚠ {self._voice_msg}")
            txt = "   ·   ".join(parts)
            if txt != self.voice_label.text():
                self.voice_label.setText(txt)
            ok = self.peer is not None and v.backend.ok
            self.talk_btn.setEnabled(v.talking() or (ok and bool(self.st.hub and self.st.hub.ok)))
            self.vmsg_btn.setEnabled(ok)
            if not v.backend.ok:
                self.talk_btn.setToolTip(f"voice is off: {v.backend.why}")
                self.vmsg_btn.setToolTip(f"voice is off: {v.backend.why}")

        # ---- chat list / view
        def refresh_list(self):
            self.list.blockSignals(True)
            self.list.clear()
            if not self.core.opponents:
                it = QListWidgetItem("  looking for other nodes…")
                it.setFlags(Qt.NoItemFlags)
                self.list.addItem(it)
            for p in self.core.opponents:
                u = self.core.unread[p] if p != self.peer else 0
                dot = "●" if self.core.online(p) else "○"
                it = QListWidgetItem(f"{dot}  {node_name(p)}" + (f"   ({u})" if u else ""))
                host = self.core.peer_info.get(p, {}).get("host")
                lh = self.core.last_heard.get(p)
                it.setToolTip((f"{host} · " if host else "") +
                              ("not heard yet" if lh is None else f"last heard {time.time() - lh:.0f} s ago"))
                it.setData(Qt.UserRole, p)
                if u:
                    f = it.font()
                    f.setBold(True)
                    it.setFont(f)
                    it.setForeground(QBrush(QColor("white")))
                self.list.addItem(it)
                if p == self.peer:
                    self.list.setCurrentItem(it)
            self.list.blockSignals(False)

        def _pick(self, cur, _prev):
            if cur is None:
                return
            self.peer = cur.data(Qt.UserRole)
            self.core.unread[self.peer] = 0
            self.refresh_list()
            self.render()

        def render(self):
            ready = self.peer is not None
            self.composer.setEnabled(ready)
            self.send_btn.setEnabled(ready)
            if not ready:
                self.header.setText(f"Looking for other nodes<span style='color:#888; font-weight:normal; "
                                    f"font-size:12px'>   ·  you: {node_name(self.core.me)}</span>")
                self.view.setHtml(
                    "<div style='font-family:sans-serif; color:#555; margin-top:30px'><p><b>No other node heard yet."
                    "</b></p><p>Every PC running cdp_daemon.py announces itself every "
                    f"{PRESENCE_S:g} s. As soon as one is heard (or sends anything), its chat opens here.</p>"
                    "<p>Check: the flowgraph runs here (press ▶ Run in GNU Radio Companion), it runs on the other "
                    "PC, and each PC has its own node number.</p></div>")
                return
            self.core.unread[self.peer] = 0
            others = ", ".join(node_name(p) for p in self.core.opponents if p != self.peer)
            state = "online" if self.core.online(self.peer) else "away"
            host = self.core.peer_info.get(self.peer, {}).get("host")
            pk = self.core.keys.get(self.peer)
            if self.core.encrypt_on and pk is not None:
                lock = f"   ·  🔒 encrypted (key {pk.fp_text})"
            elif self.core.encrypt_on:
                lock = "   ·  🔑 waiting for its key"
            else:
                lock = "   ·  🔓 not encrypted"
            self.header.setText(f"#  {node_name(self.peer)}<span style='color:#888; font-weight:normal; "
                                f"font-size:12px'>   ·  {state}{' · ' + html.escape(host) if host else ''}{lock}"
                                f"   ·  you: {node_name(self.core.me)}"
                                f"{'  ·  also heard: ' + others if others else ''}</span>")
            sb = self.view.verticalScrollBar()
            self.view.setHtml(render_html(self.core.chats[self.peer], self.core.me, self.peer))
            QTimer.singleShot(0, lambda: sb.setValue(sb.maximum()))

        def show_nodes(self):
            myfp = f" · my key {self.core.ident.fp_text}" if self.core.ident else ""
            rows = [f"<tr><td><b>{node_name(self.core.me)}</b></td><td>you ({html.escape(self.core.host)})</td>"
                    f"<td>node number {html.escape(self.st.node_how)}{myfp}</td></tr>"]
            for p in self.core.opponents:
                lh = self.core.last_heard[p]
                heard = "never" if lh is None else f"{time.time() - lh:.0f} s ago"
                n_tx = sum(1 for m in self.core.chats[p] if m.mine)
                n_rx = sum(1 for m in self.core.chats[p] if not m.mine)
                host = html.escape(self.core.peer_info.get(p, {}).get("host") or "")
                state = "online" if self.core.online(p) else "away"
                pk = self.core.keys.get(p)
                fp = f"key {pk.fp_text}" if pk else "no key yet"
                rows.append(f"<tr><td><b>{node_name(p)}</b></td><td>{state} {host}</td><td>last heard {heard} · "
                            f"{n_tx} sent / {n_rx} received · {fp}</td></tr>")
            if not self.core.opponents:
                rows.append("<tr><td colspan='3'>no other node heard yet</td></tr>")
            QMessageBox.information(self, "Nodes in this session",
                                    "<table cellpadding='4'>" + "".join(rows) + "</table>")

        # ---- sending
        def send(self):
            t = self.composer.toPlainText().strip()
            if not t or self.peer is None:
                return
            self.core.send_text(self.peer, t)
            self.composer.clear()
            self.render()

        def attach(self):
            paths, _ = QFileDialog.getOpenFileNames(self, "Send file(s)", "", "All files (*)")
            self.send_files(paths)

        def send_files(self, paths):
            if self.peer is None:
                QMessageBox.information(self, "No node yet", "No other node has been heard yet.")
                return
            cap = self.composer.toPlainText().strip()
            for i, p in enumerate(paths):
                self.send_one(Path(p), cap if i == 0 else "")
            if paths and cap:
                self.composer.clear()
            self.render()

        def send_one(self, path, caption):
            try:
                data = path.read_bytes()
            except OSError as e:
                QMessageBox.warning(self, "Cannot read file", str(e))
                return
            if file_kind(path.name) == "image":
                small = compress_image(str(path))
                if small is not None and len(small[0]) < 0.9 * len(data):
                    r = QMessageBox.question(self, "Send image",
                                             f"{path.name}: {small[3]}×{small[4]}, {human(len(data))}<br>"
                                             f"Send compressed ({small[1]}×{small[2]} JPEG, {human(len(small[0]))})?",
                                             QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
                    if r == QMessageBox.Cancel:
                        return
                    if r == QMessageBox.Yes:
                        self.core.send_bytes(self.peer, path.with_suffix(".jpg").name, small[0], caption,
                                             {"w": small[1], "h": small[2], "orig_size": len(data)})
                        return
            self.core.send_bytes(self.peer, path.name, data, caption)

        def dragEnterEvent(self, e):
            if e.mimeData().hasUrls():
                e.acceptProposedAction()

        def dropEvent(self, e):
            self.send_files([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()
                             and os.path.isfile(u.toLocalFile())])

        def _link(self, url):
            act, mid = url.scheme(), url.path()
            if self.peer is None:
                return
            m = next((x for x in self.core.chats[self.peer] if x.id == mid), None)
            if m is None:
                return
            if act == "retry":
                self.core.retry(m)
                self.render()
            elif act == "play" and m.extra.get("wav"):
                if self.st.voice.backend.ok:
                    try:
                        self.st.voice.play_clip(cdp_voice.read_wav(m.extra["wav"]))
                    except Exception:
                        QDesktopServices.openUrl(QUrl.fromLocalFile(m.extra["wav"]))
                else:
                    QDesktopServices.openUrl(QUrl.fromLocalFile(m.extra["wav"]))
            elif act == "open" and m.path:
                QDesktopServices.openUrl(QUrl.fromLocalFile(m.path))

        # ---- closing = close everything
        def closeEvent(self, e):
            if not self._closing:
                r = QMessageBox.question(self, "Close everything",
                                         "Stop the chat, the game, the flowgraph and GNU Radio Companion?")
                if r != QMessageBox.Yes:
                    e.ignore()
                    return
            self._closing = True
            self.timer.stop()
            if getattr(self, "_dash", None) is not None:
                self._dash.close()
            self.st.shutdown()
            e.accept()
            QApplication.quit()


def preview(m):
    if not m.path or not os.path.exists(m.path):
        return ""
    if m.kind == "image" and HAVE_QT:
        sz = QImageReader(m.path).size()
        if sz.width() <= 0:
            return ""
        src = html.escape(QUrl.fromLocalFile(m.path).toString(), quote=True)
        return f"<br><img src='{src}' width='{min(sz.width(), 360)}'>"
    if m.kind == "doc" and os.path.getsize(m.path) <= 200_000:
        try:
            head = open(m.path, "rb").read(3000).decode("utf-8", "replace")
        except OSError:
            return ""
        lines = head.splitlines()
        body = html.escape("\n".join(lines[:12]))
        return ("<br><table cellpadding='6' style='background:#F4F4F4; border:1px solid #DDD'><tr><td><pre "
                f"style='margin:0; font-size:12px'>{body}{'<br>…' if len(lines) > 12 else ''}</pre></td></tr></table>")
    return ""


def render_html(msgs, me, peer):
    out = ["<div style='font-family:sans-serif; font-size:14px; color:#1D1C1D'>"]
    if not msgs:
        out.append(f"<p style='color:#888; margin-top:24px'>New chat with <b>{node_name(peer)}</b>. Say hi!</p>")
    prev = None
    for m in msgs:
        who = node_name(me if m.mine else m.peer)
        dt = datetime.datetime.fromtimestamp(m.ts)
        if prev is None or prev.mine != m.mine or m.ts - prev.ts > 300:
            col = "#1D9BD1" if m.mine else "#E01E5A"
            out.append(f"<p style='margin:10px 0 0 0'><b style='color:{col}'>{who}</b>&nbsp;&nbsp;"
                       f"<span style='color:#888; font-size:12px'>{dt:%H:%M:%S}</span></p>")
        body = []
        if m.text:
            body.append(html.escape(m.text).replace("\n", "<br>"))
        if m.kind == "voice":
            dur = m.extra.get("dur")
            play = (f"<a href='play:{m.id}' style='color:#1264A3'>▶ play</a>" if m.extra.get("wav") else "")
            body.append(f"🎤 voice message{f' {dur} s' if dur else ''} {play} "
                        f"<span style='color:#888'>({human(m.size)})</span>")
        elif m.kind != "text":
            icon = {"image": "🖼", "doc": "📄"}.get(m.kind, "📎")
            extra = f" · compressed from {human(int(m.extra['orig_size']))}" if m.extra.get("orig_size") else ""
            name = html.escape(m.name or "file")
            link = (f"<a href='open:{m.id}' style='color:#1264A3'>{name}</a>" if m.path else name)
            body.append(f"{icon} {link} <span style='color:#888'>({human(m.size)}{extra})</span>{preview(m)}")
        st = ""
        if m.extra.get("enc"):
            st += "<span style='color:#888' title='end-to-end encrypted'> 🔒</span>"
        if m.extra.get("sys"):
            body = [f"<span style='color:#B3261E'>{b}</span>" for b in body]
        if m.mine:
            prog = m.extra.get("prog") if m.status == "sending" else None
            if prog and prog[1]:
                rate = m.size * prog[0] / prog[1] / max(prog[2], 1e-3) / 1e3
                sending = (f"<span style='color:#888'>  ⏳ sending… {100 * prog[0] // prog[1]} % "
                           f"({prog[0]}/{prog[1]} parts, {rate:.1f} kB/s)</span>")
            else:
                sending = "<span style='color:#888'>  ⏳ sending…</span>"
            st += {"key": "<span style='color:#888'>  🔑 waiting for the node's encryption key…</span>",
                   "sending": sending,
                  "sent": "<span style='color:#2BAC76'>  ✓ delivered</span>",
                  "failed": (f"<span style='color:#E01E5A'>  ✗ {html.escape(m.extra.get('why', 'not delivered'))} · "
                             f"<a href='retry:{m.id}' style='color:#E01E5A'>retry</a></span>")}.get(m.status, "")
        out.append(f"<p style='margin:2px 0 0 0'>{'<br>'.join(body)}{st}</p>")
        prev = m
    out.append("</div>")
    return "".join(out)


# ======================================================================================== application state
class AppState:
    def __init__(self, args):
        self.args = args
        self.session = Session()
        self.events = queue.Queue()
        self.procs = Procs(self.session)
        self.grc = find_grc(args.grc)
        self.game_cmd = find_game()
        self.link = None
        self.core = None
        self.me = None
        self.node_how = ""
        self._ctl_stop = threading.Event()
        self._ctl = None
        self.identity = None
        self.hub = None
        self.voice = None
        self.metrics = None
        self.mod = "bpsk"

    def last_login(self):
        try:
            return json.loads((HERE / ".cdp_last_login.json").read_text())
        except Exception:
            return {}

    def env(self):
        e = dict(os.environ)
        peers = self.core.opponents if self.core else []
        e.update({"CDP_MY_ID": str(self.me), "CDP_DEST": "255",          # GRC text box: to everyone
                  "CDP_PEERS": ",".join(map(str, peers)), "CDP_DEBUG_DIR": str(self.session.dir),
                  "CDP_HOME": str(HERE),                                 # where the blocks find cdp_fast.py
                  "CDP_ARQ_PORT": str(ARQ_PORT), "CDP_UDP_PORT": str(UDP_PORT), "CDP_UDP_OUT_PORT": str(UDP_OUT_PORT),
                  "CDP_CTL_PORT": str(CTL_PORT), "CDP_MOD": self.mod})
        return e

    def game_env(self):
        """The game's environment: its datagrams come from the daemon's UdpHub (the hub owns CDP_UDP_OUT_PORT and
        keeps the voice frames for itself)."""
        e = self.env()
        if self.hub is not None and self.hub.ok:
            e["CDP_UDP_OUT_PORT"] = str(GAME_UDP_PORT)
        return e

    def start(self, me, direct, how, mod="bpsk"):
        """Start as node `me` (from the login) with modulation `mod`; the other nodes are discovered on the link."""
        self.me = me
        self.node_how = how
        self.mod = "qpsk" if mod == "qpsk" else "bpsk"
        try:
            (HERE / ".cdp_last_login.json").write_text(json.dumps({"node": me, "direct": direct, "mod": self.mod}))
        except OSError:
            pass
        self.session.login(me, [], {"node_from": how, "discovery": f"presence every {PRESENCE_S:g} s", "mod": self.mod,
                                    "grc": str(self.grc) if self.grc else None, "direct": direct,
                                           "ports": {"arq": ARQ_PORT, "udp": UDP_PORT, "udp_out": UDP_OUT_PORT,
                                                     "ctl": CTL_PORT},
                                           "game_cmd": self.game_cmd})
        self._ctl = ctl_server(self.events, self._ctl_stop)
        if self._ctl is None:
            self.session.log(f"control port {CTL_PORT} busy: another daemon running? (--shutdown will not reach this one)")
        if port_busy(ARQ_PORT):
            self.session.log(f"WARNING: something already listens on {ARQ_PORT} - an old flowgraph may still run")
        try:
            self.identity = cdp_crypto.Identity()
            self.session.log(f"encryption key pair ready, fingerprint {self.identity.fp_text} "
                             f"({self.identity.path})")
        except Exception as e:
            self.identity = None
            self.session.log(f"no encryption: {e}")
        self.hub = cdp_voice.UdpHub(UDP_OUT_PORT, GAME_UDP_PORT, UDP_PORT,
                                    on_voice=lambda src, dst, p: self.voice and self.voice.on_datagram(src, dst, p))
        if not self.hub.ok:
            self.session.log(self.hub.error)
        self.prepare_fast()
        self.start_flowgraph(direct)
        self.link = Link(self.session)
        self.link.events = self.events
        self.link.start()
        self.core = ChatCore(self.session, self.link, me, identity=self.identity)
        self.voice = cdp_voice.VoiceEngine(me, self.hub.send if self.hub.ok else (lambda d, p: None),
                                           key_for=self.core.key_for, encrypt=lambda: self.core.encrypt_on,
                                           log=self.session.log)
        self.session.log(f"voice: {self.voice.backend.describe()}"
                         + ("" if self.hub.ok else " (live voice off: datagram port busy)"))
        self.metrics = cdp_metrics.Metrics(self.session.dir, extra=self.dash_extra, node=me)
        self.metrics.start_sampler(self.dash_counters)

    def dash_counters(self):
        """running byte totals of this app, sampled once a second by the dashboard"""
        v = self.voice.stats if self.voice is not None else {}
        lk = self.link
        return {"chat_tx": lk.tx_bytes if lk else 0, "chat_rx": lk.rx_bytes if lk else 0,
                "voice_tx": v.get("tx_bytes", 0), "voice_rx": v.get("rx_bytes", 0)}

    def dash_extra(self):
        core, now = self.core, time.time()
        nodes = []
        for p in core.opponents:
            lh = core.last_heard.get(p)
            pk = core.keys.get(p)
            nodes.append({"node": p, "host": core.peer_info.get(p, {}).get("host"), "online": core.online(p),
                          "last_heard_s": None if lh is None else round(now - lh, 1),
                          "fingerprint": pk.fp_text if pk else None})
        v = self.voice
        voice = None
        if v is not None:
            voice = dict(v.stats, audio=v.backend.describe() if v.backend.ok else f"off: {v.backend.why}",
                         live=bool(self.hub and self.hub.ok))
        return {"nodes": nodes, "crypto": core.crypto_info(), "voice": voice, "node": self.me,
                "link_connected": bool(self.link and self.link.connected)}

    def prepare_fast(self):
        """Build the flowgraph's C++ kernels (cdp_fast.cpp) now, in the background, so the flowgraph finds them
        compiled when it starts. Without them it runs its original Python code (slower, same results)."""
        def run():
            try:
                import cdp_fast
            except Exception as e:
                self.session.log(f"C++ kernels: cdp_fast.py not loadable ({e})")
                return
            t = time.time()
            ok = cdp_fast.lib() is not None
            self.session.log(f"C++ kernels for the flowgraph: ready ({time.time() - t:.1f} s)" if ok else
                             f"C++ kernels for the flowgraph: not available ({cdp_fast.why()}); the flowgraph uses "
                             f"its original Python code")
        if (HERE / "cdp_fast.py").is_file():
            threading.Thread(target=run, name="cdp-fast-build", daemon=True).start()

    def start_flowgraph(self, direct):
        if self.grc is None:
            self.session.log("no .grc found next to cdp_daemon.py (use --grc FILE): start the flowgraph yourself")
            return
        script = generated_script(self.grc)
        if direct and script is not None:
            self.procs.start("flowgraph", [sys.executable, "-u", str(script)], self.env(), cwd=self.grc.parent,
                             echo=ENV("CDP_FG_ECHO", "1") != "0")
            return
        cmd = shlex.split(ENV("CDP_GRC_CMD", "gnuradio-companion"))
        if shutil.which(cmd[0]) is None:
            self.session.log(f"'{cmd[0]}' not found on PATH (set CDP_GRC_CMD); start the flowgraph yourself with "
                             f"CDP_MY_ID={self.me} CDP_DEBUG_DIR={self.session.dir}")
            return
        self.procs.start("grc", cmd + [str(self.grc)], self.env(), cwd=self.grc.parent)

    def flowgraph_state(self):
        if self.procs.running("flowgraph"):
            return "flowgraph process running, waiting for its bridge"
        if self.procs.running("grc"):
            return "GNU Radio Companion is open: press ▶ Run (F6)"
        return "flowgraph not started from here - run it with this node's settings"

    def start_game(self):
        if self.game_cmd is None:
            return
        if self.procs.running("game"):
            self.session.log("game already running")
            return
        self.procs.start("game", self.game_cmd, self.game_env())

    def shutdown(self):
        self.session.log("closing everything")
        for x in (self.voice, self.hub, self.metrics):
            try:
                if x is not None:
                    x.close()
            except Exception:
                pass
        self.procs.stop_all()
        if self.link:
            self.link.stop()
        self._ctl_stop.set()
        if self._ctl:
            try:
                self._ctl.close()
            except OSError:
                pass
        self.session.close()


def pick_node(arg, last):
    """(node, how it was chosen) or (None, error)."""
    env = ENV("CDP_MY_ID", "").strip()
    if arg is not None:
        val, how = arg, "--node"
    elif env:
        if not env.isdigit():
            return None, f"CDP_MY_ID={env!r} is not a node number (0..254)"
        val, how = int(env), "CDP_MY_ID"
    elif isinstance(last.get("node"), int):
        val, how = last["node"], "used last time (.cdp_last_login.json)"
    else:
        import zlib
        val, how = zlib.crc32(socket.gethostname().encode()) % 200 + 1, "derived from the computer name"
    if not 0 <= val <= 254:
        return None, f"node number {val} ({how}) is outside 0..254"
    return val, how


def port_busy(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser(description="CDP link daemon: flowgraph, chat with every node heard, logs")
    ap.add_argument("--node", type=int, default=None, help="this PC's node number (default: $CDP_MY_ID, else the "
                                                           "one used last time, else derived from the computer name)")
    ap.add_argument("--grc", default=None, help="flowgraph file (default: v6.9*cdp_link*.grc next to this file)")
    ap.add_argument("--direct", action="store_true", help="run the generated flowgraph script without the GRC window "
                                                          "(needs one run from GRC first, which generates it)")
    ap.add_argument("--mod", choices=("bpsk", "qpsk"), default=None, help="modulation (default: $CDP_MOD, else the one "
                                                                         "used last time, else bpsk)")
    ap.add_argument("--shutdown", action="store_true", help="tell a running daemon to close everything, then exit")
    a = ap.parse_args()
    if a.shutdown:
        sys.exit(request_shutdown())
    if not HAVE_QT:
        sys.exit("PyQt5 not found. Linux: sudo apt install python3-pyqt5 ; macOS (radioconda): conda install pyqt")
    app = QApplication(sys.argv)
    app.setApplicationName("CDP Link")
    st = AppState(a)
    last = st.last_login()
    me, how = pick_node(a.node, last)
    if me is None:                                       # e.g. CDP_MY_ID=abc: let the login screen fix it
        st.session.log(how)
        me, how = pick_node(None, {})
    can_direct = st.grc is not None and generated_script(st.grc) is not None
    direct = bool(a.direct or ENV("CDP_DIRECT", "") == "1" or last.get("direct"))
    mod = (a.mod or ENV("CDP_MOD", "").strip().lower() or last.get("mod") or "bpsk")
    dlg = LoginDialog(me, how, st.grc, can_direct, direct, "qpsk" if mod == "qpsk" else "bpsk")
    if dlg.exec_() != QDialog.Accepted:
        st.session.log("login cancelled")
        st.session.close()
        return
    me2, direct, mod = dlg.values()
    if me2 != me:
        how = "chosen at login"
    st.start(me2, direct and can_direct, how, mod)
    w = ChatWindow(st)
    w.show()

    def on_signal(*_):
        st.events.put(("shutdown", "signal"))
    for sg in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sg, on_signal)
    tick = QTimer()
    tick.timeout.connect(lambda: None)                  # lets Python handle Ctrl-C while Qt runs
    tick.start(300)
    app.exec_()


if __name__ == "__main__":
    main()
