#!/usr/bin/env python3
"""
cdp_chat_v66.py - Slack-style chat for the CDP radio link, v6.6 flowgraph (no separate daemon needed)

The app talks straight to the flowgraph's two transport blocks:
  ARQ block  (reliable)  TCP  CDP_HOST:CDP_ARQ_PORT            default 127.0.0.1:5100
  UDP block  (datagrams) send to CDP_HOST:CDP_UDP_PORT          default 5200
                         receive on CDP_UDP_BIND:CDP_UDP_OUT_PORT default 127.0.0.1:5201

    export CDP_MY_ID=2
    gnuradio-companion v6.6_csma_transport.grc &
    python3 cdp_chat_v66.py --name Methal

Conversations
  #everyone   broadcast through the UDP block (dest 255): every node gets it, no delivery confirmation, text only
              (up to about 1,800 characters).
  @node N     direct messages through the ARQ block: each one is confirmed (✓) or reported failed (✗, with retry);
              files up to about 4 MB (📎 button or drag & drop).
  Other nodes appear by themselves when they send something or when their presence beacon is heard (a short UDP
  broadcast every --beacon-s seconds); "+ Add node" adds one by its ID.

Received files go to --inbox (default ~/cdp_inbox). History is kept in --history
(default ~/.cdp_chat/history_node<ID>.jsonl), so it survives restarts.
If the flowgraph is not running, messages wait ("queued") and go out as soon as the app can reach it again.

Options:  --name NAME      your display name (default: env CDP_CHAT_NAME, else "node <ID>")
          --inbox DIR      where received files are saved
          --history FILE   chat history file
          --beacon-s S     presence beacon period, 0 = off (default 60)
Settings from the environment (same names as the flowgraph and cdp_sock.py):
          CDP_HOST, CDP_ARQ_PORT, CDP_UDP_PORT, CDP_UDP_OUT_PORT, CDP_UDP_BIND, CDP_CHAT_NAME

Needs PyQt5 (installed with GNU Radio's Qt GUI; else: sudo dnf install python3-qt5  /  sudo apt install python3-pyqt5)
"""
import argparse
import hashlib
import html
import json
import os
import random
import select
import socket
import struct
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

MAGIC = b"CDPC\x01"                 # chat envelope: MAGIC + header length (4 B) + JSON header + file bytes
FILE_MAGIC = b"CDPFILE\0"           # files sent with cdp_sock.py arq-send --file
BROADCAST = 255
UDP_MAX = 2046                      # UDP block: max_payload_len (2048) - 2
FAIL_REASONS = {1: "no ACK from the other node", 2: "bad destination", 3: "radio send queue full",
                4: "too large", 5: "empty message"}


def _env(name, default):
    return os.environ.get(name, default)


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def conv_key(dest):
    return "all" if dest in (None, BROADCAST, "all") else f"dm:{int(dest)}"


def conv_dest(conv):
    return BROADCAST if conv == "all" else int(conv.split(":")[1])


def pack_envelope(hdr, data=b""):
    h = json.dumps(hdr, separators=(",", ":")).encode()
    return MAGIC + len(h).to_bytes(4, "big") + h + data


def unpack_envelope(raw):
    """(header dict, file bytes) for a chat envelope, else None."""
    if not raw.startswith(MAGIC) or len(raw) < len(MAGIC) + 4:
        return None
    n = int.from_bytes(raw[len(MAGIC):len(MAGIC) + 4], "big")
    try:
        hdr = json.loads(raw[len(MAGIC) + 4:len(MAGIC) + 4 + n].decode())
    except (ValueError, UnicodeDecodeError):
        return None
    return hdr, raw[len(MAGIC) + 4 + n:]


# =====================================================================
#  Core: sockets, messages, history (no Qt here, so it can be tested alone)
# =====================================================================
class Msg:
    FIELDS = ("id", "ts", "author", "conv", "text", "file", "mine", "status", "err", "node")

    def __init__(self, **kw):
        self.text, self.file, self.err, self.node, self.status = "", None, "", None, ""
        for k, v in kw.items():
            setattr(self, k, v)

    def to_dict(self):
        return {k: getattr(self, k) for k in self.FIELDS}


class ChatCore:
    def __init__(self, name=None, inbox=None, history=None, host=None, arq_port=None, udp_port=None,
                 udp_out_port=None, udp_bind=None, beacon_s=60.0, log=None):
        self.name = name
        self.inbox = Path(os.path.expanduser(inbox or "~/cdp_inbox"))
        self.history_path = Path(os.path.expanduser(history)) if history else None
        self.host = host or _env("CDP_HOST", "127.0.0.1")
        self.arq_port = int(arq_port or _env("CDP_ARQ_PORT", "5100"))
        self.udp_port = int(udp_port or _env("CDP_UDP_PORT", "5200"))
        self.udp_out_port = int(udp_out_port or _env("CDP_UDP_OUT_PORT", "5201"))
        self.udp_bind = udp_bind or _env("CDP_UDP_BIND", "127.0.0.1")
        self.beacon_s = float(beacon_s)
        self._log_cb = log
        self._early_log = []
        env_id = _env("CDP_MY_ID", "")
        self.env_node = int(env_id) if env_id.strip().isdigit() else None
        self.node = None                      # this node's ID (from the ARQ block's HELLO)
        self.mss = self.window = None
        self.max_msg = 4 * 1024 * 1024
        self.msgs, self.by_id = [], {}
        self.peers = {}                       # node -> {"name": str, "seen": ts}
        self.changed = True
        self.arq_ok = False
        self.udp_rx_ok = None
        self.udp_rx_err = ""
        self._arq = None
        self._inb, self._outb = bytearray(), bytearray()
        self._ref = random.getrandbits(31)
        self._inflight = {}                   # ref -> msg id
        self._t_connect = 0.0
        self._t_beacon = 0.0
        self._hist = None
        self._hist_loaded = False
        self._hist_pending = []               # records written before the history file was known
        if self.name is None:
            self.name = f"node {self.env_node}" if self.env_node is not None else None
        self._udp_tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp_rx = None
        self._open_udp_rx()
        if self.history_path is not None or self.env_node is not None:
            self._load_history()

    # ---------------- logging ----------------
    def log(self, text):
        line = f"{datetime.now():%H:%M:%S}  {text}"
        if self._log_cb:
            self._log_cb(line)
        else:
            self._early_log.append(line)

    def set_log(self, cb):
        self._log_cb = cb
        for line in self._early_log:
            cb(line)
        self._early_log = []

    # ---------------- history ----------------
    def _history_file(self):
        if self.history_path is None:
            node = self.node if self.node is not None else self.env_node
            if node is None:
                return None
            self.history_path = Path(os.path.expanduser(f"~/.cdp_chat/history_node{node}.jsonl"))
        return self.history_path

    def _load_history(self):
        if self._hist_loaded:
            return
        p = self._history_file()
        if p is None:
            return
        self._hist_loaded = True
        n = 0
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines() if p.exists() else []
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if "m" in rec:
                m = Msg(**rec["m"])
                if m.id not in self.by_id:
                    self.msgs.append(m)
                    self.by_id[m.id] = m
                    n += 1
            elif "s" in rec and rec["s"][0] in self.by_id:
                m = self.by_id[rec["s"][0]]
                m.status, m.err = rec["s"][1], rec["s"][2]
            elif "p" in rec:
                node, pname = rec["p"]
                self.peers.setdefault(int(node), {"name": pname, "seen": 0})
        for m in self.msgs:
            if m.mine and m.status == "sending":              # handed to the radio, never confirmed
                m.status, m.err = "unknown", "the app was closed before the radio confirmed it"
        self.msgs.sort(key=lambda m: m.ts)
        if n:
            self.log(f"history: {n} messages from {p}")
        pending, self._hist_pending = self._hist_pending, []
        for rec in pending:
            self._append(rec)
        self.changed = True

    def _append(self, rec):
        p = self._history_file() if self._hist_loaded else None
        if p is None:
            self._hist_pending.append(rec)
            return
        try:
            if self._hist is None:
                p.parent.mkdir(parents=True, exist_ok=True)
                self._hist = open(p, "a", encoding="utf-8")
            self._hist.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._hist.flush()
        except OSError as e:
            self.log(f"cannot write history {p}: {e}")

    def _add(self, m):
        self.msgs.append(m)
        self.by_id[m.id] = m
        if self.msgs and len(self.msgs) > 1 and self.msgs[-2].ts > m.ts:
            self.msgs.sort(key=lambda x: x.ts)
        self._append({"m": m.to_dict()})
        self.changed = True

    def _status(self, m, status, err=""):
        m.status, m.err = status, err
        self._append({"s": [m.id, status, err]})
        self.changed = True

    # ---------------- peers ----------------
    def add_peer(self, node, name=None, seen=None):
        node = int(node)
        if node == self.node or not 0 <= node <= 254:
            return
        p = self.peers.get(node)
        new = p is None
        if new:
            p = self.peers[node] = {"name": name or f"node {node}", "seen": 0}
        if name and name != p["name"]:
            p["name"] = name
            new = True
        if seen:
            p["seen"] = seen
        if new:
            self._append({"p": [node, p["name"]]})
        self.changed = True

    def peer_name(self, node):
        p = self.peers.get(int(node))
        return p["name"] if p else f"node {node}"

    def conversations(self):
        convs = {f"dm:{n}" for n in self.peers}
        convs.update(m.conv for m in self.msgs)
        convs.discard("all")
        return ["all"] + sorted(convs, key=lambda c: int(c.split(":")[1]))

    # ---------------- sockets ----------------
    def _open_udp_rx(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind((self.udp_bind, self.udp_out_port))
            s.setblocking(False)
            self._udp_rx, self.udp_rx_ok, self.udp_rx_err = s, True, ""
        except OSError as e:
            s.close()
            self._udp_rx, self.udp_rx_ok, self.udp_rx_err = None, False, str(e)
            self.log(f"cannot receive broadcasts on udp {self.udp_bind}:{self.udp_out_port}: {e}")

    def _connect(self):
        now = time.monotonic()
        if self._arq is not None or now - self._t_connect < 2.0:
            return
        self._t_connect = now
        try:
            s = socket.create_connection((self.host, self.arq_port), timeout=0.5)
        except OSError:
            return
        s.setblocking(False)
        self._arq, self._inb, self._outb = s, bytearray(), bytearray()
        self.log(f"connected to the ARQ block at {self.host}:{self.arq_port}")

    def _disconnect(self, why):
        if self._arq is None:
            return
        try:
            self._arq.close()
        except OSError:
            pass
        self._arq = None
        self.arq_ok = False
        self.log(f"lost the ARQ block ({why}); retrying every 2 s")
        for ref, mid in list(self._inflight.items()):
            m = self.by_id.get(mid)
            if m is not None and m.status == "sending":
                self._status(m, "unknown", "the connection to the radio was lost before it was confirmed")
        self._inflight.clear()
        self.changed = True

    def _arq_frame(self, body):
        self._outb += len(body).to_bytes(4, "big") + body

    def _flush(self):
        if self._arq is None or not self._outb:
            return
        try:
            n = self._arq.send(self._outb)
            del self._outb[:n]
        except (BlockingIOError, InterruptedError):
            pass
        except OSError as e:
            self._disconnect(e)

    def _read_arq(self):
        if self._arq is None:
            return
        while True:
            try:
                chunk = self._arq.recv(262144)
            except (BlockingIOError, InterruptedError):
                break
            except OSError as e:
                return self._disconnect(e)
            if not chunk:
                return self._disconnect("closed by the flowgraph")
            self._inb += chunk
        while len(self._inb) >= 4:
            n = int.from_bytes(self._inb[:4], "big")
            if len(self._inb) < 4 + n:
                break
            body = bytes(self._inb[4:4 + n])
            del self._inb[:4 + n]
            self._on_arq(body)

    def _on_arq(self, body):
        t = body[0]
        if t == 0x80:                                         # HELLO
            _, node, self.mss, self.window, self.max_msg = struct.unpack(">BBHBI", body[:9])
            first = self.node is None
            if not first and node != self.node:
                self.log(f"WARNING: the flowgraph now says this node is {node} (was {self.node})")
            if self.env_node is not None and node != self.env_node:
                self.log(f"WARNING: CDP_MY_ID here is {self.env_node} but the flowgraph says node {node}")
            self.node, self.arq_ok = node, True
            if self.name is None:
                self.name = f"node {node}"
            self._load_history()
            self.log(f"radio ready: this is node {node}, {self.mss}-byte segments, window {self.window}")
            for m in list(self.msgs):                         # send what waited for the radio
                if m.mine and m.status == "queued":
                    self._send_broadcast(m) if m.conv == "all" else self._send_dm(m)
            self._t_beacon = 0.0
            self.changed = True
        elif t == 0x81:                                       # RECV
            self._on_message(body[1], self.node, body[2:], via="arq")
        elif t in (0x82, 0x83):                               # SENT / FAIL
            ref = int.from_bytes(body[1:5], "big")
            m = self.by_id.get(self._inflight.pop(ref, None))
            if m is None:
                return
            if t == 0x82:
                self._status(m, "sent")
            else:
                reason = body[6] if len(body) > 6 else 0
                self._status(m, "failed", FAIL_REASONS.get(reason, f"error {reason}"))
                self.log(f"message to {self.peer_name(conv_dest(m.conv))} FAILED: {m.err}")

    def _read_udp(self):
        if self._udp_rx is None:
            return
        while True:
            try:
                d, _ = self._udp_rx.recvfrom(65535)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            if len(d) >= 2:
                self._on_message(d[0], d[1], d[2:], via="udp")

    def _on_message(self, src, dest, raw, via):
        env = unpack_envelope(raw)
        now = time.time()
        conv = "all" if dest == BROADCAST else f"dm:{src}"
        if env is not None:
            hdr, data = env
            if hdr.get("t") == "hello":
                self.add_peer(src, hdr.get("from"), now)
                return
            self.add_peer(src, hdr.get("from"), now)
            mid = str(hdr.get("id") or uuid.uuid4().hex)
            if mid in self.by_id:
                return                                        # resent copy of something we already have
            conv = "all" if hdr.get("to") == "all" else f"dm:{src}"
            m = Msg(id=mid, ts=float(hdr.get("ts") or now), author=hdr.get("from") or f"node {src}", conv=conv,
                    text=str(hdr.get("text") or ""), mine=False, status="", node=src)
            if "file" in hdr:
                m.file = self._save_file(hdr["file"].get("name", "file"), data)
        else:
            self.add_peer(src, None, now)
            m = Msg(id=uuid.uuid4().hex, ts=now, author=self.peer_name(src), conv=conv, mine=False, status="",
                    node=src)
            if raw.startswith(FILE_MAGIC):
                name, _, data = raw[len(FILE_MAGIC):].partition(b"\0")
                m.file = self._save_file(name.decode("utf-8", "replace"), data)
            else:
                try:
                    m.text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    m.file = self._save_file(f"data_from_node{src}.bin", raw)
        self._add(m)
        self.log(f"received from {m.author} (node {src}, {via}): "
                 f"{'file ' + m.file['name'] if m.file else repr(m.text[:60])}")

    def _save_file(self, name, data):
        name = os.path.basename(str(name)) or "file"
        self.inbox.mkdir(parents=True, exist_ok=True)
        p = self.inbox / name
        stem, suf, i = p.stem, p.suffix, 1
        while p.exists():
            p = self.inbox / f"{stem} ({i}){suf}"
            i += 1
        p.write_bytes(data)
        return {"name": name, "size": len(data), "path": str(p)}

    # ---------------- sending ----------------
    def _envelope(self, m, data=b""):
        hdr = {"t": "msg", "id": m.id, "ts": m.ts, "from": self.name or "?", "to": "all" if m.conv == "all" else "dm",
               "text": m.text}
        if m.file:
            hdr["file"] = {"name": m.file["name"], "size": m.file["size"]}
        return pack_envelope(hdr, data)

    def _send_dm(self, m):
        data = b""
        if m.file:
            try:
                data = Path(m.file["path"]).read_bytes()
            except OSError as e:
                return self._status(m, "failed", f"cannot read {m.file['path']}: {e}")
        payload = self._envelope(m, data)
        if len(payload) > self.max_msg:
            return self._status(m, "failed", f"too large ({_human(len(payload))}, max {_human(self.max_msg)})")
        if self._arq is None or not self.arq_ok:
            return self._status(m, "queued", "waiting for the radio (is the flowgraph running?)")
        self._ref = (self._ref + 1) & 0xFFFFFFFF
        self._inflight[self._ref] = m.id
        self._arq_frame(struct.pack(">BIB", 0x01, self._ref, conv_dest(m.conv)) + payload)
        self._status(m, "sending")
        self._flush()

    def _send_broadcast(self, m):
        payload = self._envelope(m)
        if not self.arq_ok:
            return self._status(m, "queued", "waiting for the radio (is the flowgraph running?)")
        if len(payload) > UDP_MAX:
            return self._status(m, "failed", f"too long for a broadcast ({len(payload)} B > {UDP_MAX} B): "
                                             f"send it as a direct message")
        try:
            self._udp_tx.sendto(bytes([BROADCAST]) + payload, (self.host, self.udp_port))
            self._status(m, "broadcast")
        except OSError as e:
            self._status(m, "failed", f"UDP send failed: {e}")

    def send_text(self, conv, text):
        text = text.strip()
        if not text:
            return None
        m = Msg(id=uuid.uuid4().hex, ts=time.time(), author=self.name or "me", conv=conv, text=text, mine=True,
                status="queued", node=self.node)
        self._add(m)
        self._send_broadcast(m) if conv == "all" else self._send_dm(m)
        return m

    def send_file(self, conv, path, caption=""):
        if conv == "all":
            raise ValueError("files can only be sent as direct messages (broadcasts are single radio frames)")
        p = Path(path)
        size = p.stat().st_size
        m = Msg(id=uuid.uuid4().hex, ts=time.time(), author=self.name or "me", conv=conv, text=caption.strip(),
                file={"name": p.name, "size": size, "path": str(p.resolve())}, mine=True, status="queued",
                node=self.node)
        self._add(m)
        self._send_dm(m)
        return m

    def retry(self, mid):
        m = self.by_id.get(mid)
        if m is None or not m.mine:
            return
        self._send_broadcast(m) if m.conv == "all" else self._send_dm(m)

    def _beacon(self):
        if self.beacon_s <= 0 or self.node is None:
            return
        now = time.monotonic()
        if now - self._t_beacon < self.beacon_s:
            return
        self._t_beacon = now
        try:
            self._udp_tx.sendto(bytes([BROADCAST]) + pack_envelope({"t": "hello", "from": self.name}),
                                (self.host, self.udp_port))
        except OSError:
            pass

    # ---------------- main loop step ----------------
    def poll(self):
        self._connect()
        self._flush()
        self._read_arq()
        self._read_udp()
        self._beacon()

    def stats(self):
        mine = [m for m in self.msgs if m.mine]
        return (sum(m.status in ("queued", "sending") for m in mine),
                sum(m.status in ("failed", "unknown") for m in mine))

    def close(self):
        for s in (self._arq, self._udp_rx, self._udp_tx):
            try:
                if s is not None:
                    s.close()
            except OSError:
                pass
        if self._hist:
            self._hist.close()


# =====================================================================
#  HTML for the message view (plain function so it can be tested)
# =====================================================================
COLORS = ["#1D9BD1", "#E01E5A", "#2BAC76", "#ECB22E", "#9B59B6", "#E67E22"]


def _color(name):
    return COLORS[int(hashlib.md5(name.encode()).hexdigest(), 16) % len(COLORS)]


def render_html(msgs, title):
    out = ["<div style='font-family:sans-serif; font-size:14px; color:#1D1C1D'>"]
    if not msgs:
        out.append(f"<p style='color:#888; margin-top:24px'>This is the very beginning of "
                   f"<b>{html.escape(title)}</b>. Say hi!</p>")
    prev_day, prev = None, None
    for m in msgs:
        dt = datetime.fromtimestamp(m.ts)
        day = dt.date()
        if day != prev_day:
            label = "Today" if day == datetime.now().date() else dt.strftime("%A, %d %B")
            out.append(f"<p align='center' style='color:#888; font-size:12px; margin:14px 0 4px 0'>"
                       f"──────  {label}  ──────</p>")
            prev_day, prev = day, None
        grouped = prev is not None and prev.author == m.author and prev.mine == m.mine and m.ts - prev.ts < 300
        if not grouped:
            who = f"node {m.node}" if m.node is not None else ""
            out.append(f"<p style='margin:10px 0 0 0'><b style='color:{_color(m.author)}'>"
                       f"{html.escape(m.author)}</b>&nbsp;&nbsp;<span style='color:#888; font-size:12px'>"
                       f"{dt:%H:%M}{'&nbsp;&nbsp;·&nbsp;&nbsp;' + who if who else ''}</span></p>")
        body = []
        if m.text:
            body.append(html.escape(m.text).replace("\n", "<br>"))
        if m.file:
            f = m.file
            name = html.escape(str(f.get("name", "file")))
            size = _human(int(f.get("size", 0) or 0))
            body.append(f"📎 <a href='open:{m.id}' style='color:#1264A3'>{name}</a> <span style='color:#888'>"
                        f"({size}) · <a href='folder:{m.id}' style='color:#888'>show in folder</a></span>")
        status = ""
        if m.mine:
            err = html.escape(m.err or "")
            status = {
                "queued": f"<span style='color:#888'>  ⏳ waiting for the radio</span>",
                "sending": "<span style='color:#888'>  ⏳ sending…</span>",
                "sent": "<span style='color:#2BAC76'>  ✓</span>",
                "broadcast": "<span style='color:#888'>  ✓ sent to everyone (no confirmation for broadcasts)</span>",
                "unknown": (f"<span style='color:#ECB22E'>  ⚠ {err} · <a href='retry:{m.id}' "
                            f"style='color:#ECB22E'>resend</a></span>"),
                "failed": (f"<span style='color:#E01E5A'>  ✗ not delivered: {err} · <a href='retry:{m.id}' "
                           f"style='color:#E01E5A'>retry</a></span>"),
            }.get(m.status, "")
        grouped_time = (f"<span style='color:#BBB; font-size:11px'>{dt:%H:%M}&nbsp;&nbsp;</span>"
                        if grouped else "")
        out.append(f"<p style='margin:2px 0 0 0'>{grouped_time}{'<br>'.join(body)}{status}</p>")
        prev = m
    out.append("</div>")
    return "".join(out)


# =====================================================================
#  Qt user interface
# =====================================================================
try:
    from PyQt5.QtCore import Qt, QTimer, QUrl, pyqtSignal
    from PyQt5.QtGui import QDesktopServices, QFont, QBrush, QColor
    from PyQt5.QtWidgets import (
        QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QLabel,
        QListWidget, QListWidgetItem, QPushButton, QTextBrowser, QPlainTextEdit,
        QFileDialog, QInputDialog, QDockWidget, QMessageBox, QFrame)
    HAVE_QT = True
except ImportError:
    HAVE_QT = False

SIDEBAR_CSS = """
#sidebar { background: #3F0E40; }
#sidebar QLabel { color: #CFC3CF; }
#workspace { color: white; font-size: 16px; font-weight: bold; }
#sidebar QListWidget { background: transparent; border: none; color: #CFC3CF; font-size: 14px; outline: 0; }
#sidebar QListWidget::item { padding: 4px 8px; border-radius: 4px; }
#sidebar QListWidget::item:selected { background: #1164A3; color: white; }
#sidebar QListWidget::item:hover:!selected { background: #350D36; }
#sidebar QPushButton { color: #CFC3CF; background: transparent; border: none; text-align: left; padding: 4px 8px; }
#sidebar QPushButton:hover { color: white; }
#header { font-size: 15px; font-weight: bold; padding: 10px 16px; border-bottom: 1px solid #DDD; background: white; }
#view { border: none; background: white; padding: 4px 12px; }
#composerBox { border: 1px solid #BBB; border-radius: 8px; background: white; }
#composer { border: none; font-size: 14px; background: white; }
#send { background: #007A5A; color: white; border-radius: 4px; padding: 5px 14px; font-weight: bold; }
#send:disabled { background: #DDD; color: #999; }
#attach { border: none; font-size: 16px; padding: 4px 6px; background: transparent; }
#attach:disabled { color: #CCC; }
"""

if HAVE_QT:
    class Composer(QPlainTextEdit):
        submitted = pyqtSignal()

        def keyPressEvent(self, e):
            if e.key() in (Qt.Key_Return, Qt.Key_Enter) and not (e.modifiers() & Qt.ShiftModifier):
                self.submitted.emit()
                return
            super().keyPressEvent(e)

    class ChatWindow(QMainWindow):
        def __init__(self, core):
            super().__init__()
            self.core = core
            self.conv = "all"
            self.last_read = {}
            self.known_rx = set()
            self.first_render = True
            self._force_bottom = True
            self._sig = None

            self.setWindowTitle("CDP Link")
            self.resize(980, 680)
            self.setAcceptDrops(True)
            self.setStyleSheet(SIDEBAR_CSS)

            # ---- sidebar ----
            side = QWidget(objectName="sidebar")
            side.setFixedWidth(240)
            sl = QVBoxLayout(side)
            sl.setContentsMargins(12, 14, 12, 12)
            sl.addWidget(QLabel("CDP Link", objectName="workspace"))
            self.me_label = QLabel("")
            self.me_label.setStyleSheet("color:#2BAC76;")
            sl.addWidget(self.me_label)
            sl.addSpacing(14)
            sl.addWidget(QLabel("Conversations"))
            self.conv_list = QListWidget()
            self.conv_list.currentItemChanged.connect(self.on_conv_changed)
            sl.addWidget(self.conv_list, 1)
            add_btn = QPushButton("+  Add node")
            add_btn.clicked.connect(self.on_add_node)
            sl.addWidget(add_btn)
            line = QFrame()
            line.setFrameShape(QFrame.HLine)
            line.setStyleSheet("color:#5B2C5C;")
            sl.addWidget(line)
            self.queue_label = QLabel("")
            self.queue_label.setWordWrap(True)
            sl.addWidget(self.queue_label)
            sl.addSpacing(8)
            sl.addWidget(QLabel("<b>Radio</b>"))
            self.radio_label = QLabel("")
            self.radio_label.setWordWrap(True)
            sl.addWidget(self.radio_label)
            self.log_btn = QPushButton("Show log")
            self.log_btn.clicked.connect(self.toggle_log)
            sl.addWidget(self.log_btn)

            # ---- main pane ----
            main = QWidget()
            ml = QVBoxLayout(main)
            ml.setContentsMargins(0, 0, 0, 0)
            ml.setSpacing(0)
            self.header = QLabel("", objectName="header")
            ml.addWidget(self.header)
            self.view = QTextBrowser(objectName="view")
            self.view.setOpenLinks(False)
            self.view.anchorClicked.connect(self.on_link)
            ml.addWidget(self.view, 1)
            box = QWidget(objectName="composerBox")
            bl = QHBoxLayout(box)
            bl.setContentsMargins(6, 4, 6, 4)
            self.attach_btn = QPushButton("📎", objectName="attach")
            self.attach_btn.clicked.connect(self.on_attach)
            bl.addWidget(self.attach_btn, 0, Qt.AlignBottom)
            self.composer = Composer(objectName="composer")
            self.composer.setFixedHeight(64)
            self.composer.submitted.connect(self.on_send)
            self.composer.textChanged.connect(self.update_send_enabled)
            bl.addWidget(self.composer, 1)
            self.send_btn = QPushButton("Send", objectName="send")
            self.send_btn.clicked.connect(self.on_send)
            bl.addWidget(self.send_btn, 0, Qt.AlignBottom)
            wrap = QWidget()
            wl = QVBoxLayout(wrap)
            wl.setContentsMargins(16, 8, 16, 4)
            wl.addWidget(box)
            self.hint = QLabel("")
            self.hint.setStyleSheet("color:#999; font-size:11px;")
            wl.addWidget(self.hint)
            ml.addWidget(wrap)

            root = QWidget()
            rl = QHBoxLayout(root)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(0)
            rl.addWidget(side)
            rl.addWidget(main, 1)
            self.setCentralWidget(root)

            # ---- log dock ----
            self.log_view = QPlainTextEdit()
            self.log_view.setReadOnly(True)
            self.log_view.setMaximumBlockCount(3000)
            self.log_view.setFont(QFont("Monospace", 9))
            self.dock = QDockWidget("Log", self)
            self.dock.setWidget(self.log_view)
            self.addDockWidget(Qt.BottomDockWidgetArea, self.dock)
            self.dock.hide()
            self.dock.visibilityChanged.connect(lambda v: self.log_btn.setText("Hide log" if v else "Show log"))
            core.set_log(self.log_view.appendPlainText)

            self.timer = QTimer(self)
            self.timer.timeout.connect(self.tick)
            self.timer.start(50)
            self.tick()
            self.update_send_enabled()
            self.composer.setFocus()

        # ---------------- loop ----------------
        def tick(self):
            self.core.poll()
            if not self.core.changed:
                return
            self.core.changed = False
            if self.first_render:
                self.known_rx = {m.id for m in self.core.msgs if not m.mine}
                for c in self.core.conversations():
                    self.last_read[c] = max((m.ts for m in self.core.msgs if m.conv == c), default=0)
                self.first_render = False
            new_rx = [m for m in self.core.msgs if not m.mine and m.id not in self.known_rx]
            if new_rx:
                self.known_rx.update(m.id for m in new_rx)
                if not self.isActiveWindow():
                    QApplication.alert(self)
            self.refresh_sidebar()
            self.render()

        def conv_title(self, c):
            if c == "all":
                return "#everyone"
            n = conv_dest(c)
            return f"@{self.core.peer_name(n)} (node {n})"

        def refresh_sidebar(self):
            core = self.core
            me = f"node {core.node}" if core.node is not None else "node ?"
            self.me_label.setText(f"● {html.escape(core.name or 'you')} · {me}")
            self.setWindowTitle(f"CDP Link — {core.name or ''} ({me})")
            self.conv_list.blockSignals(True)
            self.conv_list.clear()
            for c in core.conversations():
                unread = 0
                if c != self.conv:
                    unread = sum(1 for m in core.msgs if m.conv == c and not m.mine
                                 and m.ts > self.last_read.get(c, 0))
                label = "#  everyone" if c == "all" else f"@  {core.peer_name(conv_dest(c))}  ·  {conv_dest(c)}"
                item = QListWidgetItem(label + (f"   ({unread})" if unread else ""))
                item.setData(Qt.UserRole, c)
                if unread:
                    f = item.font()
                    f.setBold(True)
                    item.setFont(f)
                    item.setForeground(QBrush(QColor("white")))
                self.conv_list.addItem(item)
                if c == self.conv:
                    self.conv_list.setCurrentItem(item)
            self.conv_list.blockSignals(False)
            sending, bad = core.stats()
            parts = []
            if sending:
                parts.append(f"{sending} sending")
            if bad:
                parts.append(f"<span style='color:#E01E5A'>{bad} not delivered</span>")
            self.queue_label.setText(" · ".join(parts) or "All messages delivered")
            if core.arq_ok:
                arq = (f"<span style='color:#2BAC76'>connected</span> · node {core.node} · "
                       f"{core.mss}-byte segments")
            else:
                arq = (f"<span style='color:#E01E5A'>not connected</span> to "
                       f"{core.host}:{core.arq_port} — is the flowgraph running? (retrying)")
            udp = (f"broadcasts in on udp {core.udp_bind}:{core.udp_out_port}" if core.udp_rx_ok else
                   f"<span style='color:#ECB22E'>cannot receive broadcasts ({html.escape(core.udp_rx_err)})</span>")
            self.radio_label.setText(f"ARQ: {arq}<br>UDP: {udp}")

        def render(self):
            sb = self.view.verticalScrollBar()
            at_bottom = sb.value() >= sb.maximum() - 4
            c = self.conv
            if c == "all":
                sub = "broadcast to every node · no delivery confirmation · text only"
            else:
                sub = f"direct · reliable (ARQ) · ✓ = confirmed by node {conv_dest(c)}"
            self.header.setText(f"{html.escape(self.conv_title(c))}<span style='color:#888; font-weight:normal; "
                                f"font-size:12px'>   ·  {sub}</span>")
            msgs = [m for m in self.core.msgs if m.conv == c]
            self.view.setHtml(render_html(msgs, self.conv_title(c)))
            if msgs:
                self.last_read[c] = max(self.last_read.get(c, 0), msgs[-1].ts)
            if at_bottom or self._force_bottom:
                QTimer.singleShot(0, lambda: sb.setValue(sb.maximum()))
            self._force_bottom = False
            dm = c != "all"
            self.attach_btn.setEnabled(dm)
            self.attach_btn.setToolTip("Send a file (or drag & drop onto the window)" if dm else
                                       "Files can only be sent in a direct conversation")
            self.hint.setText("Enter to send · Shift+Enter for a new line" +
                              (" · drop files here to send them" if dm else ""))

        # ---------------- actions ----------------
        def on_conv_changed(self, cur, _prev):
            if cur is None:
                return
            self.conv = cur.data(Qt.UserRole)
            self._force_bottom = True
            self.core.changed = True

        def on_add_node(self):
            node, ok = QInputDialog.getInt(self, "Add node", "Node ID (CDP_MY_ID of the other PC, 0-254):", 0, 0, 254)
            if not ok:
                return
            if node == self.core.node:
                QMessageBox.information(self, "Add node", "That is this node's own ID.")
                return
            name, ok = QInputDialog.getText(self, "Add node", "Name (optional):")
            self.core.add_peer(node, name.strip() if ok and name.strip() else None)
            self.conv = f"dm:{node}"
            self._force_bottom = True

        def toggle_log(self):
            self.dock.setVisible(not self.dock.isVisible())

        def update_send_enabled(self):
            self.send_btn.setEnabled(bool(self.composer.toPlainText().strip()))

        def on_send(self):
            text = self.composer.toPlainText().strip()
            if not text:
                return
            self.core.send_text(self.conv, text)
            self.composer.clear()
            self._force_bottom = True

        def on_attach(self):
            paths, _ = QFileDialog.getOpenFileNames(self, "Send file(s)")
            self.send_files(paths)

        def send_files(self, paths):
            if not paths:
                return
            if self.conv == "all":
                QMessageBox.information(self, "Files", "Files can only be sent in a direct conversation "
                                                       "(pick a node on the left).")
                return
            caption = self.composer.toPlainText().strip()
            for i, p in enumerate(paths):
                try:
                    self.core.send_file(self.conv, p, caption if i == 0 else "")
                except (OSError, ValueError) as e:
                    QMessageBox.warning(self, "Could not send file", f"{p}\n{e}")
            if caption:
                self.composer.clear()
            self._force_bottom = True

        def dragEnterEvent(self, e):
            if e.mimeData().hasUrls():
                e.acceptProposedAction()

        def dropEvent(self, e):
            paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
            self.send_files([p for p in paths if os.path.isfile(p)])

        def on_link(self, url):
            action, mid = url.scheme(), url.path()
            m = self.core.by_id.get(mid)
            if m is None:
                return
            if action == "retry":
                self.core.retry(mid)
            elif action in ("open", "folder"):
                p = m.file.get("path") if m.file else None
                if not p or not os.path.exists(p):
                    QMessageBox.information(self, "File", "That file is not here (moved or deleted).")
                    return
                target = p if action == "open" else str(Path(p).parent)
                QDesktopServices.openUrl(QUrl.fromLocalFile(target))

        def closeEvent(self, e):
            self.core.close()
            super().closeEvent(e)


# =====================================================================
def main():
    ap = argparse.ArgumentParser(description="Slack-style chat over the CDP radio link (v6.6 transport blocks)")
    ap.add_argument("--name", default=os.environ.get("CDP_CHAT_NAME"), help="your display name")
    ap.add_argument("--inbox", default="~/cdp_inbox", help="where received files are saved")
    ap.add_argument("--history", default=None, help="history file (default ~/.cdp_chat/history_node<ID>.jsonl)")
    ap.add_argument("--beacon-s", type=float, default=60.0, help="presence beacon period in s, 0 = off")
    args = ap.parse_args()
    if not HAVE_QT:
        sys.exit("PyQt5 not found: sudo dnf install python3-qt5  (Ubuntu: sudo apt install python3-pyqt5)")
    core = ChatCore(name=args.name, inbox=args.inbox, history=args.history, beacon_s=args.beacon_s)
    app = QApplication(sys.argv)
    app.setApplicationName("CDP Link")
    w = ChatWindow(core)
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
