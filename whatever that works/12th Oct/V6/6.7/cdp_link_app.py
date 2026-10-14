#!/usr/bin/env python3
"""
cdp_link_app.py - CDP link v6.7: chat + diagnostics dashboard + flowgraph launcher in one app

    python3 cdp_link_app.py --node 2 --name Methal

The app starts the v6.7 flowgraph itself (compiled with grcc, window hidden), restarts it if it stops, and
connects to its ARQ (5100), UDP (5200/5201) and telemetry (5300) sockets. The node ID, name and every link
parameter are set in the Settings tab and saved in ~/.cdp_link/config.json.

Tabs
  Chat        the v6.6 chat (#everyone broadcast, @node direct messages and files with delivery confirmation)
  Dashboard   Live monitor  - the running system: radio, CPU, CSMA, receiver, ARQ, watchdog events
              Run analysis  - packet tests both ways, a reliable transfer, the peer's diagnostics -> report
  Settings    node ID, name, flowgraph file, auto start/restart, every parameter (live or at restart)
  Log         app and flowgraph console

Reports (HTML, Markdown, JSON) are saved in ~/.cdp_link/reports/; telemetry log and stack dumps in ~/.cdp_link/.
Needs PyQt5 (sudo dnf install python3-qt5) and GNU Radio 3.10 (grcc).
"""
import argparse
import collections
import hashlib
import html
import json
import math
import os
import queue
import random
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

MAGIC = b"CDPC\x01"                 # chat envelope: MAGIC + header length (4 B) + JSON header + file bytes
FILE_MAGIC = b"CDPFILE\0"           # files sent with cdp_sock.py arq-send --file
BROADCAST = 255
CONTROL_TYPES = ("ping", "pong", "diag_req", "diag", "bulk")
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
                 udp_out_port=None, udp_bind=None, beacon_s=60.0, log=None, node_id=None):
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
        if node_id is not None:                # set in the app (overrides CDP_MY_ID)
            self.env_node = int(node_id)
        self.on_control = None                # callback(src, dest, hdr, data, via) for test/diag packets
        self._ctl_inflight = {}               # ref -> callback(ok, why) for control packets sent by ARQ
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
        for cb in self._ctl_inflight.values():
            try:
                cb(False, "the connection to the radio was lost")
            except Exception:
                pass
        self._ctl_inflight.clear()
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
                self.log(f"WARNING: this app is set to node {self.env_node} but the flowgraph says node {node} "
                         f"(use Save & restart flowgraph in Settings)")
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
            cb = self._ctl_inflight.pop(ref, None)
            if cb is not None:
                reason = body[6] if len(body) > 6 else 0
                try:
                    cb(t == 0x82, "" if t == 0x82 else FAIL_REASONS.get(reason, f"error {reason}"))
                except Exception as e:
                    self.log(f"control callback error: {e!r}")
                return
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
            if hdr.get("t") in CONTROL_TYPES:
                if self.on_control is not None:
                    self.add_peer(src, None, now)
                    try:
                        self.on_control(src, dest, hdr, data, via)
                    except Exception as e:
                        self.log(f"control packet error: {e!r}")
                return
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

    # ---------------- control packets (link tests, diagnostics) ----------------
    def send_control_udp(self, dest, hdr, pad_to=0):
        """One radio frame through the UDP block (no confirmation). pad_to: total payload size in bytes."""
        payload = pack_envelope(hdr)
        if pad_to > len(payload):
            payload = pack_envelope(hdr, b"\0" * (min(pad_to, UDP_MAX) - len(payload)))
        try:
            self._udp_tx.sendto(bytes([int(dest)]) + payload[:UDP_MAX], (self.host, self.udp_port))
            return True
        except OSError:
            return False

    def send_control_arq(self, dest, hdr, data=b"", cb=None):
        """Reliable control message through the ARQ block; cb(ok, why) when confirmed or failed."""
        if self._arq is None or not self.arq_ok:
            return None
        self._ref = (self._ref + 1) & 0xFFFFFFFF
        if cb is not None:
            self._ctl_inflight[self._ref] = cb
        self._arq_frame(struct.pack(">BIB", 0x01, self._ref, int(dest)) + pack_envelope(hdr, data))
        self._flush()
        return self._ref

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
#  Configuration (~/.cdp_link/config.json)
# =====================================================================
APP_DIR = Path(os.path.expanduser("~/.cdp_link"))
# key, environment variable for the flowgraph, label, type, default, live target block, live attribute
PARAMS = [
    ("arq_mss", "CDP_ARQ_MSS", "ARQ segment size (bytes, max 2037)", int, 1000, "arq", "mss"),
    ("arq_window", "CDP_ARQ_WINDOW", "ARQ window (segments, 1-32)", int, 8, "arq", "window"),
    ("arq_retries", "CDP_ARQ_RETRIES", "ARQ tries per segment before FAIL", int, 8, "arq", "max_retries"),
    ("arq_init_rto", "CDP_ARQ_INIT_RTO", "ARQ first timeout (s)", float, 2.0, "arq", "init_rto_s"),
    ("arq_min_rto", "CDP_ARQ_MIN_RTO", "ARQ shortest timeout (s)", float, 1.0, "arq", "min_rto_s"),
    ("arq_max_rto", "CDP_ARQ_MAX_RTO", "ARQ longest timeout (s)", float, 8.0, "arq", "max_rto_s"),
    ("arq_ack_every", "CDP_ARQ_ACK_EVERY", "ACK at least every N segments", int, 4, "arq", "ack_every"),
    ("arq_ack_delay", "CDP_ARQ_ACK_DELAY", "ACK at the latest after (s)", float, 0.5, "arq", "ack_delay_s"),
    ("csma_slot", "CDP_CSMA_SLOT", "CSMA slot (s)", float, 0.015, "csma", "slot_s"),
    ("csma_difs", "CDP_CSMA_DIFS", "CSMA quiet time before slots (s)", float, 0.020, "csma", "difs_s"),
    ("csma_cwmin", "CDP_CSMA_CWMIN", "CSMA backoff window (slots)", int, 8, "csma", "cw_min"),
    ("csma_cwmax", "CDP_CSMA_CWMAX", "CSMA largest backoff window", int, 128, "csma", "cw_max"),
    ("csma_txdelay", "CDP_CSMA_TXDELAY", "Min. radio TX+RX delay (s)", float, 0.012, "csma", "tx_delay_s"),
    ("csma_guard", "CDP_CSMA_GUARD", "Receiver-off guard (s)", float, 0.005, "csma", "gate_guard_s"),
    ("cs_margin", "CDP_CS_MARGIN", "CSMA sense margin (dB)", float, 6.0, "csma", "cs_margin_db"),
    ("sq_margin", "CDP_SQ_MARGIN", "Squelch margin (dB)", float, 6.0, "noise", "margin_db"),
    ("tx_att", "CDP_TX_ATT", "TX attenuation while sending (dB)", float, 20.0, "csma", "tx_att_db"),
    ("tx_mute", "CDP_TX_MUTE", "Mute TX when idle (1 = on)", int, 1, "csma", "tx_mute"),
    ("debug", "CDP_DEBUG", "Deframer debug lines (1 = on)", int, 1, "deframer", "debug"),
]
PORTS = [("arq_port", "CDP_ARQ_PORT", 5100), ("udp_port", "CDP_UDP_PORT", 5200),
         ("udp_out_port", "CDP_UDP_OUT_PORT", 5201), ("telem_port", "CDP_TELEM_PORT", 5300)]


def default_config():
    here = Path(__file__).resolve().parent
    return {"node_id": 1, "name": "", "grc": str(here / "v6.7_cdp_link.grc"), "auto_start": True,
            "auto_restart": True, "show_flowgraph_gui": False, "bind": "127.0.0.1", "peer": None, "beacon_s": 60,
            "inbox": "~/cdp_inbox", "ports": {k: d for k, _, d in PORTS}, "params": {p[0]: p[4] for p in PARAMS}}


class Config:
    def __init__(self, path=None):
        self.path = Path(path) if path else APP_DIR / "config.json"
        self.data = default_config()
        self.load()

    def load(self):
        try:
            d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        for k, v in d.items():
            if isinstance(v, dict) and isinstance(self.data.get(k), dict):
                self.data[k].update(v)
            else:
                self.data[k] = v

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        os.replace(tmp, self.path)

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v

    def env(self):
        """Environment for the flowgraph: node ID, ports and every SET_* parameter."""
        e = {"CDP_MY_ID": str(int(self.data["node_id"])), "CDP_BIND": str(self.data["bind"]),
             "CDP_LOG_DIR": str(APP_DIR)}
        for k, env, _ in PORTS:
            e[env] = str(int(self.data["ports"][k]))
        for key, env, _, typ, dflt, _, _ in PARAMS:
            e[env] = str(typ(self.data["params"].get(key, dflt)))
        return e


# =====================================================================
#  Flowgraph runner: compiles the .grc with grcc, runs it, restarts it, keeps its console
# =====================================================================
class FlowgraphRunner:
    PATTERNS = [("radio_link_lost", re.compile(r"Broken pipe|Unable to refill buffer|Unable to push buffer|"
                                                r"Failed to read overflow status")),
                ("crash", re.compile(r"terminate called|terminate reached|Traceback|Segmentation fault")),
                ("watchdog", re.compile(r"\[WATCHDOG\]")),
                ("samples_dropped_warning", re.compile(r"only [\d.]+ % of the samples arrive")),
                ("mute_off", re.compile(r"TX mute OFF")),
                ("python_error", re.compile(r"Error|error:")),]

    def __init__(self, cfg, log=None):
        self.cfg = cfg
        self.log = log or (lambda s: None)
        self.proc = None
        self.lines = collections.deque(maxlen=4000)
        self._q = queue.Queue()
        self.counts = collections.Counter()
        self.starts = 0
        self.exits = []                     # (time, exit code)
        self.want_running = False
        self._restart_at = None
        self.started_at = None
        self.status = "stopped"
        self.error = ""
        self.build_dir = APP_DIR / "build"
        self._logf = None
        self._t_ext = 0.0

    def script(self):
        grc = Path(os.path.expanduser(self.cfg["grc"]))
        fg_id = "cdp_link_v67"
        try:
            m = re.search(r"^options:\s*\n(?:\s+.*\n)*?\s+id:\s*(\S+)", grc.read_text(), re.M)
            if m:
                fg_id = m.group(1).strip("'\"")
        except OSError:
            pass
        return grc, self.build_dir / f"{fg_id}.py"

    def compile(self):
        grc, script = self.script()
        if not grc.exists():
            raise RuntimeError(f"flowgraph file not found: {grc} (set it in Settings)")
        if script.exists() and script.stat().st_mtime >= grc.stat().st_mtime:
            return script
        grcc = shutil.which("grcc")
        if not grcc:
            raise RuntimeError("grcc (GNU Radio's flowgraph compiler) not found in PATH")
        self.build_dir.mkdir(parents=True, exist_ok=True)
        self.log(f"compiling {grc.name} with grcc ...")
        r = subprocess.run([grcc, "-o", str(self.build_dir), str(grc)], capture_output=True, text=True, timeout=300)
        if r.returncode != 0 or not script.exists():
            raise RuntimeError(f"grcc failed ({r.returncode}): {(r.stderr or r.stdout)[-600:]}")
        return script

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        self.want_running = True
        self._restart_at = None
        if not self.running():
            self._launch()

    def _external(self):
        """True if another flowgraph (e.g. started from GNU Radio Companion) already serves the ARQ port."""
        try:
            socket.create_connection(("127.0.0.1", int(self.cfg["ports"]["arq_port"])), timeout=0.3).close()
            return True
        except OSError:
            return False

    def _launch(self):
        if self._external():
            self.status = "external"
            self.error = "a flowgraph is already running (started outside the app): using it"
            self.log(self.error)
            return False
        try:
            script = self.compile()
        except Exception as e:
            self.status, self.error = "error", str(e)
            self.log(f"flowgraph not started: {e}")
            return False
        env = dict(os.environ)
        env.update(self.cfg.env())
        if not self.cfg["show_flowgraph_gui"]:
            env["QT_QPA_PLATFORM"] = "offscreen"          # no flowgraph window: less CPU for the radio
        APP_DIR.mkdir(parents=True, exist_ok=True)
        if self._logf is None:
            self._logf = open(APP_DIR / f"flowgraph_node{self.cfg['node_id']}.log", "a", encoding="utf-8")
        self._logf.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} start node {self.cfg['node_id']} "
                         f"({script.name})\n")
        self.proc = subprocess.Popen([sys.executable, "-u", str(script)], cwd=str(self.build_dir), env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     text=True, errors="replace", bufsize=1, start_new_session=True)
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        self.starts += 1
        self.started_at = time.time()
        self.status, self.error = "running", ""
        self.log(f"flowgraph started (pid {self.proc.pid}, node {self.cfg['node_id']}"
                 f"{', window hidden' if not self.cfg['show_flowgraph_gui'] else ''})")
        return True

    def _reader(self, proc):
        for line in proc.stdout:
            self._q.put(line.rstrip("\n"))
        self._q.put(None)

    def stop(self):
        self.want_running = False
        self._restart_at = None
        p = self.proc
        if p is None or p.poll() is not None:
            self.status = "stopped"
            return
        for sig, wait in ((signal.SIGINT, 6), (signal.SIGTERM, 3), (signal.SIGKILL, 2)):
            try:
                os.kill(p.pid, sig)
                p.wait(timeout=wait)
                break
            except subprocess.TimeoutExpired:
                continue
            except OSError:
                break
        self.status = "stopped"
        self.log("flowgraph stopped")

    def restart(self):
        self.stop()
        self.start()

    def dump_stacks(self):
        if self.running():
            os.kill(self.proc.pid, signal.SIGUSR1)
            return True
        return False

    def poll(self):
        new = []
        while True:
            try:
                line = self._q.get_nowait()
            except queue.Empty:
                break
            if line is None:
                continue
            stamped = f"{datetime.now():%H:%M:%S}  {line}"
            new.append(line)
            self.lines.append(stamped)
            if self._logf:
                self._logf.write(stamped + "\n")
            mo = re.match(r"^(O+)(?=\[|$)", line)
            if mo:
                self.counts["overflow"] += len(mo.group(1))
            mu = re.match(r"^(U+)(?=\[|$)", line)
            if mu:
                self.counts["underflow"] += len(mu.group(1))
            for key, rx in self.PATTERNS:
                if rx.search(line):
                    self.counts[key] += 1
        if self._logf:
            self._logf.flush()
        if self.proc is not None and self.proc.poll() is not None and self.status == "running":
            code = self.proc.returncode
            self.exits.append((time.time(), code))
            self.status = f"exited ({code})"
            self.log(f"flowgraph exited with code {code}")
            if self.want_running and self.cfg["auto_restart"]:
                recent = sum(1 for t, _ in self.exits if time.time() - t < 300)
                delay = min(30, [2, 5, 10, 20][min(recent - 1, 3)])
                self._restart_at = time.time() + delay
                self.log(f"restarting the flowgraph in {delay} s")
        if self.status == "external" and self.want_running and time.time() - self._t_ext > 5:
            self._t_ext = time.time()
            if not self._external():
                self.log("the outside flowgraph stopped: starting the app's own")
                self._launch()
        if self._restart_at and time.time() >= self._restart_at and not self.running():
            self._restart_at = None
            self._launch()
        return new

    def summary(self):
        return {"status": self.status, "pid": self.proc.pid if self.running() else None,
                "uptime_s": (time.time() - self.started_at) if self.running() and self.started_at else 0,
                "starts": self.starts, "exits": self.exits[-10:], "counts": dict(self.counts), "error": self.error}


# =====================================================================
#  Telemetry client (snapshots from the flowgraph's Telemetry block)
# =====================================================================
class TelemetryClient:
    def __init__(self, port, host="127.0.0.1"):
        self.host, self.port = host, int(port)
        self.sock = None
        self.buf = bytearray()
        self.latest = None
        self.history = collections.deque(maxlen=3600)
        self.events = collections.deque(maxlen=1000)
        self.replies = collections.deque(maxlen=50)
        self._t_try = 0.0
        self.t_last = 0.0

    @property
    def connected(self):
        return self.sock is not None

    def fresh(self, max_age=3.0):
        return self.latest is not None and time.time() - self.t_last < max_age

    def poll(self):
        if self.sock is None:
            if time.monotonic() - self._t_try < 2.0:
                return
            self._t_try = time.monotonic()
            try:
                s = socket.create_connection((self.host, self.port), timeout=0.3)
                s.setblocking(False)
                self.sock, self.buf = s, bytearray()
            except OSError:
                return
        while True:
            try:
                chunk = self.sock.recv(1 << 20)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                chunk = b""
            if not chunk:
                self.close()
                return
            self.buf += chunk
        while b"\n" in self.buf:
            line, _, rest = bytes(self.buf).partition(b"\n")
            self.buf = bytearray(rest)
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("type") == "snapshot":
                self.latest = obj
                self.t_last = time.time()
                self.history.append(obj)
                self.events.extend(obj.get("events_new") or [])
            else:
                self.replies.append(obj)

    def send(self, obj):
        if self.sock is None:
            return False
        try:
            self.sock.setblocking(True)
            self.sock.settimeout(1.0)
            self.sock.sendall((json.dumps(obj) + "\n").encode())
            self.sock.setblocking(False)
            return True
        except OSError:
            self.close()
            return False

    def close(self):
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass
        self.sock = None


# =====================================================================
#  Link test (Analysis mode) and the responder every app runs
# =====================================================================
PROFILES = {
    "quick (about 40 s)": [("ping", 32, 20, 0.5), ("ping", 1000, 12, 0.6), ("bulk", 20000), ("diag",)],
    "full (about 2 min)": [("ping", 32, 30, 0.4), ("ping", 200, 20, 0.5), ("ping", 1000, 20, 0.6),
                           ("ping", 1800, 20, 0.7), ("bulk", 100000), ("diag",)],
}


def frame_bytes(payload):
    """Bytes on air for a framer payload (preamble 128, train+sync 8, Hamming(31,26) header+body, 128-B padding)."""
    bits = -(-56 // 26) * 31 + -(-(payload + 4) * 8 // 26) * 31
    raw = 136 + -(-bits // 8)
    return -(-(raw + 128) // 128) * 128


def expected_goodput(mss, mac_s=0.0875):
    """kB/s for a long ARQ transfer with no losses (1 ACK per 4 segments)."""
    air = frame_bytes(mss + 11) * 32 / 1.5e6
    ack = frame_bytes(12) * 32 / 1.5e6
    return 4 * mss / (4 * (air + mac_s) + (ack + mac_s)) / 1e3


class Responder:
    """Answers the peer's test packets (pings, diagnostic requests) and routes replies to a running test."""

    def __init__(self, core, summary_fn):
        self.core, self.summary_fn = core, summary_fn
        self.ping_rx = collections.Counter()
        self.tester = None
        self.bulk_rx = 0
        self._diag_done = set()
        core.on_control = self.on_control

    def on_control(self, src, dest, hdr, data, via):
        t = hdr.get("t")
        if t == "ping":
            key = (src, str(hdr.get("run")))
            self.ping_rx[key] += 1
            self.core.send_control_udp(src, {"t": "pong", "run": hdr.get("run"), "seq": hdr.get("seq"),
                                             "rx": self.ping_rx[key], "ts": hdr.get("ts"), "size": hdr.get("size")})
        elif t == "diag_req":
            run = str(hdr.get("run"))
            if (src, run) in self._diag_done:            # asked twice (ARQ and UDP): answer once
                return
            self._diag_done.add((src, run))
            try:
                summ = self.summary_fn()
            except Exception as e:
                summ = {"error": repr(e)}
            summ["ping_rx"] = {run: n for (s, run), n in self.ping_rx.items() if s == src}
            summ["bulk_rx"] = self.bulk_rx
            self.core.send_control_arq(src, {"t": "diag", "run": hdr.get("run"), "summary": summ})
        elif t == "bulk":
            self.bulk_rx += len(data)
        elif t in ("pong", "diag") and self.tester is not None:
            self.tester.on_control(src, hdr)


class LinkTester:
    def __init__(self, core, telem, peer, profile, log=None):
        self.core, self.telem, self.peer = core, telem, int(peer)
        self.profile = profile
        self.log = log or (lambda s: None)
        self.run = uuid.uuid4().hex[:8]
        self.phases = list(PROFILES[profile])
        self.idx = -1
        self.done = False
        self.error = None
        self.t0 = time.time()
        self.progress = "starting"
        self.res = {"run": self.run, "peer": peer, "profile": profile, "start": self.t0, "pings": [], "bulk": None,
                    "remote": None, "local_before": telem.latest if telem else None, "local_after": None}
        self._next()

    def _phase_run(self):
        return f"{self.run}-{self.idx}"

    def _next(self):
        self.idx += 1
        if self.idx >= len(self.phases):
            self.done = True
            self.res["end"] = time.time()
            self.res["local_after"] = self.telem.latest if self.telem else None
            self.progress = "finished"
            return
        ph = self.phases[self.idx]
        self.st = {"kind": ph[0], "t0": time.monotonic()}
        if ph[0] == "ping":
            _, size, count, interval = ph
            self.st.update(size=size, count=count, interval=interval, sent=0, t_send={}, rtt={}, rx_max=0,
                           next=time.monotonic(), last_send=None)
        elif ph[0] == "bulk":
            n = ph[1]
            self.st.update(n=n, result=None, arq0=self._arq_peer())
            data = os.urandom(n)
            ref = self.core.send_control_arq(self.peer, {"t": "bulk", "run": self.run, "n": n}, data,
                                             cb=lambda ok, why: self.st.update(result=(ok, why, time.monotonic())))
            if ref is None:
                self.st["result"] = (False, "radio not connected", time.monotonic())
        elif ph[0] == "diag":
            self.st.update(sent=False)
            self.core.send_control_arq(self.peer, {"t": "diag_req", "run": self.run})
            self.core.send_control_udp(self.peer, {"t": "diag_req", "run": self.run})

    def _arq_peer(self):
        snap = self.telem.latest if self.telem else None
        try:
            return dict(snap["blocks"]["arq"]["tx"].get(str(self.peer)) or {})
        except (TypeError, KeyError):
            return {}

    def stop(self):
        self.error = "stopped"
        self.done = True
        self.res["end"] = time.time()
        self.progress = "stopped"

    def on_control(self, src, hdr):
        if src != self.peer or self.done:
            return
        if hdr.get("t") == "pong" and hdr.get("run") == self._phase_run() and self.st["kind"] == "ping":
            seq = hdr.get("seq")
            t = self.st["t_send"].get(seq)
            if t is not None:
                self.st["rtt"][seq] = time.monotonic() - t
            self.st["rx_max"] = max(self.st["rx_max"], int(hdr.get("rx") or 0))
        elif hdr.get("t") == "diag" and hdr.get("run") == self.run:
            self.res["remote"] = hdr.get("summary")

    def poll(self):
        if self.done:
            return
        st, now = self.st, time.monotonic()
        k = st["kind"]
        if k == "ping":
            if st["sent"] < st["count"] and now >= st["next"]:
                seq = st["sent"]
                hdr = {"t": "ping", "run": self._phase_run(), "seq": seq, "ts": time.time(), "size": st["size"]}
                self.core.send_control_udp(self.peer, hdr, pad_to=st["size"])
                st["t_send"][seq] = now
                st["sent"] += 1
                st["next"] += st["interval"]
                st["last_send"] = now
            self.progress = (f"ping {st['size']} B: {st['sent']}/{st['count']} sent, "
                             f"{len(st['rtt'])} answers")
            if st["sent"] >= st["count"] and now - st["last_send"] > 3.0:
                rtts = sorted(st["rtt"].values())
                self.res["pings"].append({"size": st["size"], "sent": st["sent"], "pongs": len(rtts),
                                          "rx_reported": st["rx_max"], "run": self._phase_run(),
                                          "rtt_med_ms": 1e3 * rtts[len(rtts) // 2] if rtts else None,
                                          "rtt_p90_ms": 1e3 * rtts[int(0.9 * (len(rtts) - 1))] if rtts else None})
                self._next()
        elif k == "bulk":
            self.progress = f"reliable transfer of {st['n'] // 1000} kB to node {self.peer}: {now - st['t0']:.0f} s"
            if st["result"] is not None or now - st["t0"] > 300:
                ok, why, t_end = st["result"] or (False, "no answer within 300 s", now)
                dt = t_end - st["t0"]
                a1 = self._arq_peer()
                d = {k2: (a1.get(k2, 0) or 0) - (st["arq0"].get(k2, 0) or 0) for k2 in ("tx", "retx", "fast", "timeouts")}
                self.res["bulk"] = {"bytes": st["n"], "ok": ok, "why": why, "seconds": dt,
                                    "kBps": st["n"] / dt / 1e3 if ok and dt > 0 else 0.0, "arq": d,
                                    "rto_s": a1.get("rto_s"), "srtt_s": a1.get("srtt_s"), "cwnd": a1.get("cwnd")}
                self._next()
        elif k == "diag":
            self.progress = f"asking node {self.peer} for its diagnostics"
            if self.res["remote"] is not None or now - st["t0"] > 25:
                self._next()

    def results(self):
        """Pings with forward/reverse loss filled in (forward uses the peer's own count when available)."""
        remote_rx = ((self.res.get("remote") or {}).get("ping_rx") or {})
        out = []
        for p in self.res["pings"]:
            rx = max(p["rx_reported"], int(remote_rx.get(p["run"], 0) or 0))
            q = dict(p, rx=rx)
            q["fwd_loss"] = 1 - rx / p["sent"] if p["sent"] else None
            q["rev_loss"] = (1 - p["pongs"] / rx) if rx else None
            out.append(q)
        return out


# =====================================================================
#  Analysis: turn telemetry + test results into findings with fixes
# =====================================================================
def _num(x, d=None):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else d


def summarize_history(hist, seconds=600):
    """Statistics of this node's telemetry over the last `seconds`."""
    if not hist:
        return {}
    t_end = hist[-1]["t"]
    win = [h for h in hist if h["t"] >= t_end - seconds]
    rates = [_num(h["blocks"].get("csma", {}).get("rx_rate_pct")) for h in win]
    rates = [r for r in rates if r is not None]
    floors = [_num(h["blocks"].get("csma", {}).get("floor_db")) for h in win]
    floors = [f for f in floors if f is not None]
    busy = [_num(h["blocks"].get("csma", {}).get("busy_frac")) for h in win]
    busy = [b for b in busy if b is not None]
    cpu = [_num(h["proc"].get("cpu_pct")) for h in win]
    cpu = [c for c in cpu if c is not None]
    threads = collections.defaultdict(list)
    for h in win:
        for name, pct in h["proc"].get("top_threads") or []:
            threads[name].append(pct)
    top = sorted(((n, sum(v) / len(win)) for n, v in threads.items()), key=lambda x: -x[1])[:6]
    first, last = win[0]["blocks"], win[-1]["blocks"]

    def delta(block, key):
        a = (first.get(block) or {}).get("counters", {}).get(key, 0)
        b = (last.get(block) or {}).get("counters", {}).get(key, 0)
        return (b or 0) - (a or 0)
    dec = {k: delta("deframer", k) for k in ("train", "train_clean", "train_bad_sync", "train_garbage", "lock",
                                              "bad_len", "crc_fail", "crc_fail_cut", "ok", "delivered", "sq_eob")}
    cs0, cs1 = first.get("csma") or {}, last.get("csma") or {}
    return {"seconds": t_end - win[0]["t"], "snapshots": len(win),
            "rx_rate_min": min(rates) if rates else None, "rx_rate_mean": sum(rates) / len(rates) if rates else None,
            "rx_rate_bad_frac": sum(r < 99 for r in rates) / len(rates) if rates else None,
            "floor_med": sorted(floors)[len(floors) // 2] if floors else None,
            "floor_max": max(floors) if floors else None,
            "busy_mean": sum(busy) / len(busy) if busy else None, "cpu_mean": sum(cpu) / len(cpu) if cpu else None,
            "cpu_max": max(cpu) if cpu else None, "late_max_ms": max(h.get("late_ms_max", 0) for h in win),
            "top_threads": [[n, round(p, 1)] for n, p in top], "deframer": dec,
            "csma_tx": (cs1.get("tx", 0) or 0) - (cs0.get("tx", 0) or 0),
            "no_echo": (cs1.get("no_echo", 0) or 0) - (cs0.get("no_echo", 0) or 0),
            "late_unmute": (cs1.get("late_unmute", 0) or 0) - (cs0.get("late_unmute", 0) or 0),
            "latest": win[-1]}


def node_summary(cfg, telem, runner, core):
    """What this node reports about itself (also sent to the peer on a diagnostic request)."""
    hist = list(telem.history) if telem else []
    s = summarize_history(hist)
    latest = s.pop("latest", None)
    blocks = (latest or {}).get("blocks", {})
    return {"node": cfg["node_id"], "name": core.name if core else "", "time": time.time(),
            "params": dict(cfg["params"]), "history": s, "blocks": blocks,
            "proc": (latest or {}).get("proc"), "events": list(telem.events)[-40:] if telem else [],
            "runner": runner.summary() if runner else None, "telemetry_ok": bool(telem and telem.fresh())}


def analyze(local, remote=None, tests=None, bulk=None, cfg=None):
    """Rules: return findings [{severity, area, title, evidence, fix}] (severity: critical, warning, info, ok)."""
    F = []

    def add(sev, area, title, evidence, fix):
        F.append({"severity": sev, "area": area, "title": title, "evidence": evidence, "fix": fix})
    nodes = [("this node", local)] + ([("peer", remote)] if remote else [])
    for who, n in nodes:
        if not n:
            continue
        h, b = n.get("history") or {}, n.get("blocks") or {}
        cs, mu = b.get("csma") or {}, (b.get("csma") or {}).get("mute") or {}
        runner = n.get("runner") or {}
        cnt = runner.get("counts") or {}
        tag = f"{who} (node {n.get('node')})"
        if not n.get("telemetry_ok", True) and who == "this node":
            add("critical", "flowgraph", f"{tag}: no telemetry from the flowgraph",
                f"flowgraph status: {runner.get('status')} {runner.get('error') or ''}",
                "Start the flowgraph from the Settings tab (or check the Log tab for why it stopped). The v6.7 "
                "flowgraph must be used: older versions have no telemetry block.")
        r_min, bad = _num(h.get("rx_rate_min")), _num(h.get("rx_rate_bad_frac"), 0)
        if r_min is not None and r_min < 99:
            add("critical" if r_min < 97 else "warning", "CPU", f"{tag}: the PC loses radio samples",
                f"lowest {r_min:.1f} % of samples arrived; {100 * bad:.0f} % of the time below 99 %; "
                f"{cnt.get('overflow', 0)} overflow marks (O); CPU {h.get('cpu_mean') or 0:.0f} % average; "
                f"busiest threads: {h.get('top_threads')}",
                "Frames that overlap a gap are lost. Keep 'Show flowgraph window' off (Settings), set deframer "
                "debug to 0, keep the squelch margin at 6 dB, close other programs, use the performance power "
                "profile (powerprofilesctl set performance) and keep laptops on the charger.")
        if cnt.get("radio_link_lost") or any(e.get("key") == "rx_stream_stalled" for e in n.get("events") or []):
            add("critical", "radio", f"{tag}: the radio connection dropped",
                f"{cnt.get('radio_link_lost', 0)} 'Broken pipe / Unable to refill' lines, "
                f"{len(runner.get('exits') or [])} flowgraph exits, receive-stream stalls in the watchdog log",
                "The radio or its USB/network link went away (often power: the Pluto draws most at low TX "
                "attenuation). Use a powered USB hub or the Pluto's second USB port, a short good cable, and "
                "check 'dmesg | tail' right after it happens. The app restarts the flowgraph automatically.")
        if mu and mu.get("enabled") and (mu.get("failed") or not mu.get("ok")):
            add("critical", "radio", f"{tag}: TX mute is not working",
                f"mute state: {mu.get('reason')}",
                "Without it the idle transmitter leaks into the own receiver and that node hears weakly. Install "
                "libiio (sudo dnf install libiio python3-libiio) and restart the flowgraph.")
        if (mu.get("errors") or 0) > 0 or (_num(h.get("late_unmute"), 0) > 0):
            add("warning", "radio", f"{tag}: TX attenuation writes fail or come late",
                f"{mu.get('errors')} write errors, {h.get('late_unmute')} late unmutes, write max "
                f"{mu.get('write_max_ms')} ms", "Check the radio connection (USB/network); frames sent late go out "
                                               "at 89.75 dB and are lost.")
        echo, wmax, slot = _num(cs.get("echo_max_ms")), _num(mu.get("write_max_ms"), 0), _num(cs.get("slot_ms"))
        if echo is not None and slot is not None and echo + wmax >= slot:
            add("warning", "CSMA", f"{tag}: CSMA slot too short for this radio's delay",
                f"own echo max {echo:.1f} ms + attenuation write {wmax:.1f} ms >= slot {slot:.1f} ms",
                f"Set 'CSMA slot' to {math.ceil((echo + wmax + 3) / 5) * 5 / 1000:.3f} s (Settings, applies live) "
                f"on both nodes.")
        if _num(h.get("no_echo"), 0) > 0:
            add("warning", "radio", f"{tag}: own bursts not heard by own receiver",
                f"{h.get('no_echo')} bursts without echo in the window",
                "The transmitter may not be sending (TX cable/antenna, attenuation at 89.75, sink stalled).")
        busy = _num(h.get("busy_mean"))
        if busy is not None and busy > 0.3:
            add("warning", "channel", f"{tag}: channel busy {100 * busy:.0f} % of the time",
                f"noise floor {h.get('floor_med')} dB (max {h.get('floor_max')})",
                "Interference on the frequency or a raised noise floor: check the spectrum, try another FREQ, or "
                "raise the CSMA sense margin.")
        de = h.get("deframer") or {}
        if de.get("train"):
            cut = de.get("crc_fail_cut", 0)
            if de.get("crc_fail", 0) > 0.1 * max(de.get("lock", 1), 1):
                add("warning", "receiver", f"{tag}: many frames fail CRC",
                    f"{de.get('crc_fail')} CRC fails of {de.get('lock')} frames, {cut} with the squelch closing "
                    f"inside the frame", "Squelch cuts: lower the squelch margin (4 dB). Otherwise the signal is "
                                         "weak at this node: see the link tests below.")
            if de.get("train_bad_sync", 0) > 0.1 * de.get("train", 1):
                add("warning", "receiver", f"{tag}: bits go wrong right after the train word",
                    f"{de.get('train_bad_sync')} of {de.get('train')} train words had a broken sync word",
                    "Weak or distorted signal at this receiver (equalizer/Costas). Improve the link budget or "
                    "check for a DC/LO problem (TX mute on?).")
        for e in (n.get("events") or [])[-40:]:
            if e.get("severity") == "critical" and e.get("key") in ("csma_tx_phase_stuck", "tx_path_stalled",
                                                                   "arq_thread_stalled"):
                add("critical", "stall", f"{tag}: {e.get('key').replace('_', ' ')}",
                    f"{e.get('text')} (stack dump: {e.get('stacks', '-')})",
                    "This is the 'everything stops' case: the stack dump file shows where each Python thread was. "
                    "Send it to me with the report.")
            if e.get("key") == "deframer_waiting":
                add("warning", "stall", f"{tag}: frames held up in the deframer",
                    e.get("text"), "A frame's end is stuck until more signal arrives (the receiver only processes "
                                   "samples while the squelch is open). The ARQ resends, so messages arrive late.")
            if e.get("key") in ("python_stall", "hot_thread"):
                add("warning", "CPU", f"{tag}: {e.get('key').replace('_', ' ')}", e.get("text"),
                    "One Python block holds the CPU: see the busiest threads; reduce GUI sinks and debug output.")
            if (e.get("key") or "").startswith("arq_stuck_"):
                add("warning", "ARQ", f"{tag}: a message stayed open for a long time", e.get("text"),
                    "The link lost many frames in a row and the ARQ backed off (timeout doubled up to the max). "
                    "Lower 'ARQ longest timeout' (e.g. 4 s) for faster recovery, and fix the link losses.")
        for dest, p in ((b.get("arq") or {}).get("tx") or {}).items():
            tx, retx = _num(p.get("tx"), 0), _num(p.get("retx"), 0)
            if tx >= 20 and retx / tx > 0.2:
                add("warning", "ARQ", f"{tag}: {100 * retx / tx:.0f} % of ARQ segments to node {dest} were resent",
                    f"{tx} sent, {retx} resent, {p.get('timeouts')} timeouts, {p.get('failed')} failed messages, "
                    f"rto {p.get('rto_s')} s, cwnd {p.get('cwnd')}",
                    "Frames or ACKs are lost on this link: see the direction findings; smaller segments "
                    "(ARQ segment size 512) survive weak links better.")
    # ---- direction / size from the ping tests
    if tests:
        small = [t for t in tests if t["size"] <= 64]
        for t in small[:1]:
            fl, rl = t.get("fwd_loss"), t.get("rev_loss")
            if t["sent"] and not t["pongs"]:
                if remote is not None and fl == 1.0:
                    add("critical", "link", "this node -> peer is not getting through at all",
                        f"0 of {t['sent']} short packets arrived at the peer (its diagnostics came back by ARQ)",
                        "The peer hears this node only sometimes or not at all: check this node's TX attenuation, TX "
                        "antenna/cable and TX mute; compare the noise floors; do the radio swap test.")
                else:
                    add("critical", "link", "no answers from the peer",
                        f"0 of {t['sent']} short packets answered and no diagnostics from the peer",
                        "The peer's app/flowgraph is not running, the peer ID is wrong, or the link is down in at "
                        "least one direction. Check the peer's dashboard (Live monitor) while you run the test.")
                continue
            if fl is not None and rl is not None:
                if fl - rl > 0.15:
                    add("critical" if fl > 0.3 else "warning", "link", "this node -> peer is the weak direction",
                        f"{100 * fl:.0f} % of this node's packets lost at the peer, {100 * rl:.0f} % the other way",
                        "Either this transmitter is weaker (TX attenuation, antenna/cable on the TX port, radio unit) "
                        "or the peer hears worse (compare noise floors below; TX mute on the peer?). Swap the radios "
                        "between the PCs: if the weak direction moves with the radio, it's that radio/antenna.")
                elif rl - fl > 0.15:
                    add("critical" if rl > 0.3 else "warning", "link", "peer -> this node is the weak direction",
                        f"{100 * rl:.0f} % of the peer's answers lost here, {100 * fl:.0f} % the other way",
                        "This node hears worse or the peer transmits weaker: compare noise floors and TX "
                        "attenuation; swap test as above.")
                elif max(fl, rl) <= 0.05:
                    add("ok", "link", "short packets get through both ways",
                        f"loss {100 * fl:.0f} % forward, {100 * rl:.0f} % back", "-")
        if len(tests) >= 2:
            a, z = tests[0], tests[-1]
            la, lz = a.get("fwd_loss"), z.get("fwd_loss")
            if la is not None and lz is not None and lz - la > 0.15:
                good = [t for t in tests if t.get("fwd_loss") is not None and t["fwd_loss"] <= la + 0.05]
                best = max(good, key=lambda t: t["size"]) if good else a
                add("warning", "link", "loss grows with packet size (signal-to-noise limited)",
                    f"{100 * la:.0f} % at {a['size']} B, {100 * lz:.0f} % at {z['size']} B",
                    f"Use smaller ARQ segments (ARQ segment size about {max(256, best['size'] - 100)}), raise TX "
                    f"power (lower attenuation), improve antennas/height.")
    if remote and local:
        lf = _num((local.get("history") or {}).get("floor_med"))
        rf = _num((remote.get("history") or {}).get("floor_med"))
        if lf is not None and rf is not None and abs(lf - rf) > 6:
            hi = "this node" if lf > rf else "the peer"
            add("warning", "radio", f"{hi} has a {abs(lf - rf):.0f} dB higher noise floor",
                f"noise floor here {lf:.1f} dB, peer {rf:.1f} dB",
                f"{hi.capitalize()} hears weaker signals worse. Check its TX mute, antenna separation, nearby "
                f"interference; compare with the same radio swapped.")
    if bulk:
        a_ = bulk.get("arq") or {}
        segs = (f"{a_.get('tx', 0)} segments sent, {a_.get('retx', 0)} resent ({a_.get('fast', 0)} early), "
                f"{a_.get('timeouts', 0)} timeouts")
        mss = (cfg or {}).get("params", {}).get("arq_mss", 1000) if cfg else 1000
        exp = expected_goodput(int(mss))
        if not bulk.get("ok"):
            add("critical", "ARQ", "the reliable transfer test failed", f"{bulk.get('why')} after "
                f"{bulk.get('seconds', 0):.0f} s", "See the direction findings: the link is too lossy at the moment.")
        elif bulk.get("kBps", 0) < 0.5 * exp:
            add("warning", "ARQ", f"reliable transfer at {bulk['kBps']:.1f} kB/s (about {exp:.1f} kB/s possible)",
                segs,
                "Losses and timeouts slow it down: fix the weak direction; consider ARQ segment size 512.")
        else:
            add("ok", "ARQ", f"reliable transfer at {bulk['kBps']:.1f} kB/s (about {exp:.1f} kB/s possible)", segs, "-")
    order = {"critical": 0, "warning": 1, "info": 2, "ok": 3}
    seen, out = set(), []
    for f in sorted(F, key=lambda f: order[f["severity"]]):
        if (f["title"], f["evidence"][:40]) not in seen:
            seen.add((f["title"], f["evidence"][:40]))
            out.append(f)
    if not any(f["severity"] in ("critical", "warning") for f in out):
        out.append({"severity": "ok", "area": "all", "title": "no problems found",
                    "evidence": "all checks passed", "fix": "-"})
    return out


# =====================================================================
#  HTML for the dashboard and reports (plain functions, testable without Qt)
# =====================================================================
SEV_COLOR = {"critical": "#E01E5A", "warning": "#ECB22E", "info": "#1D9BD1", "ok": "#2BAC76"}


def _f(x, fmt="{:.1f}", none="-"):
    return fmt.format(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else none


def _dot(ok):
    c = {True: "#2BAC76", False: "#E01E5A", None: "#ECB22E"}[ok]
    return f"<span style='color:{c}'>●</span>"


def render_monitor_html(snap, telem, runner, core, cfg):
    css = ("<style>td,th{padding:2px 8px;text-align:left;font-size:13px} th{color:#666;font-weight:normal} "
           "h3{margin:12px 0 4px 0;font-size:14px}</style>")
    out = [css, "<div style='font-family:sans-serif'>"]
    rs = runner.summary() if runner else {}
    fresh = telem is not None and telem.fresh()
    out.append(f"<p>{_dot(rs.get('status') == 'running')} Flowgraph <b>{html.escape(str(rs.get('status')))}</b>"
               f" · pid {rs.get('pid') or '-'} · up {_f(rs.get('uptime_s'), '{:.0f}')} s · started "
               f"{rs.get('starts', 0)}× · {html.escape(rs.get('error') or '')}<br>"
               f"{_dot(fresh)} Telemetry {'live' if fresh else 'not received'} · "
               f"{_dot(bool(core and core.arq_ok))} Chat/ARQ socket {'connected' if core and core.arq_ok else 'not connected'}"
               f" · node {cfg['node_id']}</p>")
    if not snap:
        out.append("<p style='color:#888'>No telemetry yet. The v6.7 flowgraph sends it once it runs.</p></div>")
        return "".join(out)
    b, p = snap.get("blocks", {}), snap.get("proc", {})
    cs, mu, no, co = b.get("csma") or {}, (b.get("csma") or {}).get("mute") or {}, b.get("noise") or {}, b.get("corr") or {}
    de, ar, ud, tp = b.get("deframer") or {}, b.get("arq") or {}, b.get("udp") or {}, b.get("txprobe") or {}
    active = snap.get("active_problems") or []
    out.append("<h3>Problems now</h3>")
    out.append("<p>" + ("<br>".join(f"<span style='color:#E01E5A'>● {html.escape(a)}</span>" for a in active)
                        if active else "<span style='color:#2BAC76'>● none</span>") + "</p>")
    rate = cs.get("rx_rate_pct")
    out.append("<h3>Radio and PC</h3><table>")
    rows = [("Samples arriving", f"{_dot(rate is None or rate >= 99)} {_f(rate)} %"),
            ("Noise floor (CSMA / squelch)", f"{_f(cs.get('floor_db'))} dB / {_f(no.get('floor_db'))} dB "
                                             f"(squelch threshold {_f(no.get('threshold_db'))} dB)"),
            ("Channel now", f"{_f(cs.get('cs_db'))} dB, busy {_f(100 * (cs.get('busy_frac') or 0), '{:.0f}')} % "
                            f"of the last second"),
            ("TX mute", f"{_dot(bool(mu.get('ok')) if mu.get('enabled') else None)} "
                        f"{'on' if mu.get('enabled') else 'off'} · {html.escape(str(mu.get('reason')))} · write "
                        f"{_f(mu.get('write_med_ms'))} ms (max {_f(mu.get('write_max_ms'))}) · errors {mu.get('errors')} "
                        f"· sending at {_f(mu.get('tx_att_db'))} dB"),
            ("TX path", f"{tp.get('bursts', '-')} bursts, {_f(tp.get('airtime_s'))} s on air, last "
                        f"{_f(tp.get('last_age_s'))} s ago"),
            ("Process", f"CPU {_f(p.get('cpu_pct'), '{:.0f}')} % ({p.get('cores')} cores), {_f(p.get('rss_mb'), '{:.0f}')} MB,"
                        f" {p.get('threads')} threads, Python blocked up to {_f(snap.get('late_ms_max'), '{:.0f}')} ms"),
            ("Busiest threads", ", ".join(f"{html.escape(n)} {v:.0f} %" for n, v in (p.get("top_threads") or [])[:5]))]
    out += [f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows] + ["</table>"]
    out.append("<h3>CSMA</h3><table>")
    rows = [("Frames sent", f"{cs.get('tx')} · deferrals {cs.get('deferrals')} · dropped {cs.get('dropped')} · "
                            f"no echo {cs.get('no_echo')} · late unmute {cs.get('late_unmute')}"),
            ("Queue", f"{cs.get('queue')} frames, oldest {_f(cs.get('oldest_queued_s'))} s · cw {cs.get('cw')}"),
            ("Access delay", f"{_f(cs.get('access_med_ms'), '{:.0f}')} ms median, {_f(cs.get('access_p95_ms'), '{:.0f}')} ms p95"),
            ("Own echo / slot", f"{_f(cs.get('echo_med_ms'))} ms median, {_f(cs.get('echo_max_ms'))} ms max · slot "
                                f"{_f(cs.get('slot_ms'))} ms · RX-off {_f(cs.get('tx_delay_ms'))} + airtime + "
                                f"{_f(cs.get('guard_ms'))} ms"),
            ("State", f"{'sending (' + str(cs.get('phase')) + ', ' + _f(cs.get('phase_age_s'), '{:.2f}') + ' s)' if cs.get('self_tx') else 'listening'}"
                      f"{' · busy' if cs.get('busy') else ''}")]
    out += [f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows] + ["</table>"]
    c = de.get("counters") or {}
    out.append("<h3>Receiver</h3><table>")
    rows = [("Correlator", f"{co.get('found')} train words · correlation median {_f(co.get('rho_med'), '{:.2f}')}, "
                           f"min {_f(co.get('rho_min'), '{:.2f}')} (threshold {_f(co.get('thr'), '{:.2f}')})"),
            ("Deframer", f"{c.get('lock', 0)} frames · CRC ok {c.get('ok', 0)} · CRC fail {c.get('crc_fail', 0)} "
                         f"(squelch cut {c.get('crc_fail_cut', 0)}) · bad length {c.get('bad_len', 0)} · for other nodes "
                         f"{c.get('other', 0)}"),
            ("Waiting", f"{de.get('buffer_bits')} bits buffered, waiting {_f(de.get('waiting_s'))} s · last good frame "
                        f"{_f(de.get('last_ok_age_s'), '{:.0f}')} s ago")]
    out += [f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows] + ["</table>"]
    out.append("<h3>ARQ (reliable)</h3><table><tr><th>to node</th><th>msgs sent/failed/open</th><th>segments "
               "sent/resent/timeouts</th><th>in flight / waiting</th><th>cwnd</th><th>srtt / rto</th>"
               "<th>oldest open</th></tr>")
    for dest, q in (ar.get("tx") or {}).items():
        out.append(f"<tr><td>{dest}</td><td>{q.get('sent')}/{q.get('failed')}/{q.get('open_msgs')}</td><td>{q.get('tx')}/"
                   f"{q.get('retx')}/{q.get('timeouts')}</td><td>{q.get('inflight')} / {q.get('waiting')}</td><td>"
                   f"{q.get('cwnd')}</td><td>{_f(q.get('srtt_s'), '{:.2f}')} / {_f(q.get('rto_s'), '{:.2f}')} s</td><td>"
                   f"{_f(q.get('oldest_msg_s'), '{:.0f}')} s</td></tr>")
    if not ar.get("tx") and not ar.get("rx"):
        out.append("<tr><td colspan=7 style='color:#888'>no reliable traffic yet</td></tr>")
    for src, q in (ar.get("rx") or {}).items():
        out.append(f"<tr><td colspan=7>from node {src}: delivered {q.get('delivered')} msgs / {q.get('bytes')} B, "
                   f"duplicates {q.get('dup')}, ACKs {q.get('acks')}, skipped {q.get('skipped')}, out of order "
                   f"{q.get('out_of_order')}</td></tr>")
    out.append(f"</table><p style='font-size:12px;color:#666'>ARQ worker last ran {_f(ar.get('loop_age_s'), '{:.2f}')} s"
               f" ago · daemons {ar.get('clients')} · UDP {ud.get('counters')}</p>")
    evs = list(telem.events)[-15:] if telem else []
    out.append("<h3>Recent watchdog events</h3>")
    if evs:
        out.append("<table>" + "".join(
            f"<tr><td>{datetime.fromtimestamp(e['t']):%H:%M:%S}</td><td style='color:{SEV_COLOR.get(e['severity'], '#333')}'>"
            f"{e['severity']}</td><td>{html.escape(e['text'])}</td></tr>" for e in reversed(evs)) + "</table>")
    else:
        out.append("<p style='color:#888'>none</p>")
    out.append("</div>")
    return "".join(out)


def render_report(rep, fmt="html"):
    """rep: {"title", "time", "local", "remote", "findings", "pings", "bulk", "cfg"} -> HTML or Markdown text."""
    F = rep["findings"]
    md = fmt == "md"
    L = []
    if md:
        L.append(f"# {rep['title']}\n\n{datetime.fromtimestamp(rep['time']):%Y-%m-%d %H:%M:%S}\n")
        L.append("## Findings\n\n| Severity | Area | Finding | Evidence | What to do |\n|---|---|---|---|---|")
        for f in F:
            L.append(f"| {f['severity']} | {f['area']} | {f['title']} | {f['evidence']} | {f['fix']} |")
    else:
        L.append("<div style='font-family:sans-serif;font-size:13px'><style>td,th{padding:3px 8px;vertical-align:top;"
                 "text-align:left} th{background:#F4F4F4}</style>")
        L.append(f"<h2>{html.escape(rep['title'])}</h2><p style='color:#666'>"
                 f"{datetime.fromtimestamp(rep['time']):%Y-%m-%d %H:%M:%S}</p><h3>Findings</h3><table>"
                 "<tr><th></th><th>Finding</th><th>Evidence</th><th>What to do</th></tr>")
        for f in F:
            L.append(f"<tr><td style='color:{SEV_COLOR[f['severity']]}'><b>{f['severity']}</b><br>{f['area']}</td>"
                     f"<td>{html.escape(f['title'])}</td><td>{html.escape(str(f['evidence']))}</td>"
                     f"<td>{html.escape(f['fix'])}</td></tr>")
        L.append("</table>")
    if rep.get("pings"):
        hdr = ["Size", "Sent", "Arrived at peer", "Loss there", "Answers back", "Loss back", "RTT median", "RTT p90"]
        rows = [[f"{p['size']} B", p["sent"], p["rx"], _f(100 * p['fwd_loss'] if p['fwd_loss'] is not None else None, '{:.0f} %'),
                 p["pongs"], _f(100 * p['rev_loss'] if p['rev_loss'] is not None else None, '{:.0f} %'),
                 _f(p["rtt_med_ms"], "{:.0f} ms"), _f(p["rtt_p90_ms"], "{:.0f} ms")] for p in rep["pings"]]
        L += _table("Packet tests (UDP, each way)", hdr, rows, md)
    if rep.get("bulk"):
        bk = rep["bulk"]
        L += _table("Reliable transfer test (ARQ)", ["Bytes", "Result", "Time", "Goodput", "Segments sent/resent/early/timeouts",
                                                    "srtt / rto", "cwnd"],
                    [[bk["bytes"], "confirmed" if bk["ok"] else f"failed: {bk['why']}", _f(bk["seconds"], "{:.1f} s"),
                      _f(bk["kBps"], "{:.2f} kB/s"), "/".join(str(bk["arq"].get(k, 0)) for k in ("tx", "retx", "fast", "timeouts")),
                      f"{_f(bk.get('srtt_s'), '{:.2f}')} / {_f(bk.get('rto_s'), '{:.2f}')} s", bk.get("cwnd")]], md)
    cols = [("this node", rep.get("local"))] + ([("peer", rep.get("remote"))] if rep.get("remote") else [])
    if any(n for _, n in cols):
        keys = [("node", lambda n: n.get("node")), ("samples arriving (min / mean)", lambda n: f"{_f(n['history'].get('rx_rate_min'))} / {_f(n['history'].get('rx_rate_mean'))} %"),
                ("noise floor (median / max)", lambda n: f"{_f(n['history'].get('floor_med'))} / {_f(n['history'].get('floor_max'))} dB"),
                ("channel busy (mean)", lambda n: _f(100 * n['history']['busy_mean'], '{:.0f} %') if _num(n['history'].get('busy_mean')) is not None else '-'),
                ("CPU mean / max", lambda n: f"{_f(n['history'].get('cpu_mean'), '{:.0f}')} / {_f(n['history'].get('cpu_max'), '{:.0f}')} %"),
                ("busiest threads", lambda n: ", ".join(f"{a} {b:.0f} %" for a, b in (n['history'].get('top_threads') or [])[:4])),
                ("Python blocked (max)", lambda n: _f(n['history'].get('late_max_ms'), '{:.0f} ms')),
                ("TX mute", lambda n: str(((n.get('blocks') or {}).get('csma') or {}).get('mute', {}).get('reason'))),
                ("TX attenuation sending", lambda n: _f(((n.get('blocks') or {}).get('csma') or {}).get('mute', {}).get('tx_att_db'), '{:.2f} dB')),
                ("own echo max / slot", lambda n: f"{_f(((n.get('blocks') or {}).get('csma') or {}).get('echo_max_ms'))} / {_f(((n.get('blocks') or {}).get('csma') or {}).get('slot_ms'))} ms"),
                ("frames received / CRC fail / squelch cut", lambda n: "{} / {} / {}".format(*(n['history'].get('deframer', {}).get(k, 0) for k in ('lock', 'crc_fail', 'crc_fail_cut')))),
                ("flowgraph starts / exits", lambda n: f"{(n.get('runner') or {}).get('starts')} / {len((n.get('runner') or {}).get('exits') or [])}"),
                ("radio overflows (O) / link lost lines", lambda n: f"{((n.get('runner') or {}).get('counts') or {}).get('overflow', 0)} / {((n.get('runner') or {}).get('counts') or {}).get('radio_link_lost', 0)}")]
        rows = []
        for label, fn in keys:
            row = [label]
            for _, n in cols:
                try:
                    row.append(fn(n) if n and n.get("history") is not None else "-")
                except Exception:
                    row.append("-")
            rows.append(row)
        L += _table("Both nodes", ["", *[c for c, _ in cols]], rows, md)
    ev = []
    for who, n in cols:
        for e in (n or {}).get("events") or []:
            ev.append([datetime.fromtimestamp(e["t"]).strftime("%H:%M:%S"), who, e["severity"], e["text"]])
    if ev:
        L += _table("Watchdog events", ["Time", "Node", "Severity", "Event"], sorted(ev)[-40:], md)
    if rep.get("cfg"):
        L += _table("Settings in use (this node)", ["Setting", "Value"],
                    [[k, v] for k, v in rep["cfg"]["params"].items()], md)
    if not md:
        L.append("</div>")
    return "\n".join(L)


def _table(title, hdr, rows, md):
    if md:
        return [f"\n## {title}\n", "| " + " | ".join(map(str, hdr)) + " |", "|" + "---|" * len(hdr)] + \
               ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return [f"<h3>{html.escape(title)}</h3><table><tr>" + "".join(f"<th>{html.escape(str(h))}</th>" for h in hdr) + "</tr>"] + \
           ["<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>" for r in rows] + ["</table>"]


def save_report(rep):
    d = APP_DIR / "reports"
    d.mkdir(parents=True, exist_ok=True)
    stem = d / f"report_node{(rep.get('local') or {}).get('node', 'x')}_{datetime.fromtimestamp(rep['time']):%Y%m%d_%H%M%S}"
    (stem.with_suffix(".html")).write_text("<html><meta charset='utf-8'><body>" + render_report(rep, "html") + "</body></html>")
    (stem.with_suffix(".md")).write_text(render_report(rep, "md"))
    with open(stem.with_suffix(".json"), "w") as f:
        json.dump(rep, f, default=str, indent=1)
    return stem.with_suffix(".html")


# =====================================================================
#  Qt user interface: Chat | Dashboard | Settings | Log
# =====================================================================
try:
    from PyQt5.QtCore import Qt, QTimer, QUrl, pyqtSignal
    from PyQt5.QtGui import QDesktopServices, QFont, QBrush, QColor
    from PyQt5.QtWidgets import (
        QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QLabel, QListWidget, QListWidgetItem,
        QPushButton, QTextBrowser, QPlainTextEdit, QFileDialog, QInputDialog, QMessageBox, QFrame, QTabWidget,
        QFormLayout, QSpinBox, QDoubleSpinBox, QCheckBox, QLineEdit, QComboBox, QScrollArea)
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

    class ChatPanel(QWidget):
        def __init__(self, core):
            super().__init__()
            self.core = core
            self.conv = "all"
            self.last_read = {}
            self.known_rx = set()
            self.first_render = True
            self._force_bottom = True
            self.setAcceptDrops(True)
            self.setStyleSheet(SIDEBAR_CSS)
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
            sl.addWidget(line)
            self.queue_label = QLabel("")
            self.queue_label.setWordWrap(True)
            sl.addWidget(self.queue_label)
            self.radio_label = QLabel("")
            self.radio_label.setWordWrap(True)
            sl.addWidget(self.radio_label)
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
            rl = QHBoxLayout(self)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(0)
            rl.addWidget(side)
            rl.addWidget(main, 1)
            self.update_send_enabled()

        def set_core(self, core):
            self.core = core
            self.conv, self.first_render, self._force_bottom = "all", True, True
            core.changed = True

        def refresh(self):
            core = self.core
            if not core.changed:
                return
            core.changed = False
            if self.first_render:
                self.known_rx = {m.id for m in core.msgs if not m.mine}
                for c in core.conversations():
                    self.last_read[c] = max((m.ts for m in core.msgs if m.conv == c), default=0)
                self.first_render = False
            new_rx = [m for m in core.msgs if not m.mine and m.id not in self.known_rx]
            if new_rx:
                self.known_rx.update(m.id for m in new_rx)
                if not self.window().isActiveWindow():
                    QApplication.alert(self.window())
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
            self.conv_list.blockSignals(True)
            self.conv_list.clear()
            for c in core.conversations():
                unread = 0
                if c != self.conv:
                    unread = sum(1 for m in core.msgs if m.conv == c and not m.mine and m.ts > self.last_read.get(c, 0))
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
            parts = ([f"{sending} sending"] if sending else []) + \
                    ([f"<span style='color:#E01E5A'>{bad} not delivered</span>"] if bad else [])
            self.queue_label.setText(" · ".join(parts) or "All messages delivered")
            arq = (f"<span style='color:#2BAC76'>radio connected</span> · node {core.node}" if core.arq_ok else
                   "<span style='color:#E01E5A'>radio not connected</span> (flowgraph starting?)")
            self.radio_label.setText(arq)

        def render(self):
            sb = self.view.verticalScrollBar()
            at_bottom = sb.value() >= sb.maximum() - 4
            c = self.conv
            sub = ("broadcast to every node · no delivery confirmation · text only" if c == "all" else
                   f"direct · reliable (ARQ) · ✓ = confirmed by node {conv_dest(c)}")
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
            self.hint.setText("Enter to send · Shift+Enter for a new line" + (" · drop files here" if dm else ""))

        def on_conv_changed(self, cur, _prev):
            if cur is None:
                return
            self.conv = cur.data(Qt.UserRole)
            self._force_bottom = True
            self.core.changed = True

        def on_add_node(self):
            node, ok = QInputDialog.getInt(self, "Add node", "Node ID of the other PC (0-254):", 0, 0, 254)
            if not ok:
                return
            if node == self.core.node:
                QMessageBox.information(self, "Add node", "That is this node's own ID.")
                return
            name, ok = QInputDialog.getText(self, "Add node", "Name (optional):")
            self.core.add_peer(node, name.strip() if ok and name.strip() else None)
            self.conv = f"dm:{node}"
            self._force_bottom = True

        def update_send_enabled(self):
            self.send_btn.setEnabled(bool(self.composer.toPlainText().strip()))

        def on_send(self):
            text = self.composer.toPlainText().strip()
            if text:
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
                QMessageBox.information(self, "Files", "Files can only be sent in a direct conversation.")
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
                QDesktopServices.openUrl(QUrl.fromLocalFile(p if action == "open" else str(Path(p).parent)))

    class MainWindow(QMainWindow):
        def __init__(self, cfg):
            super().__init__()
            self.cfg = cfg
            self.setWindowTitle("CDP Link")
            self.resize(1100, 760)
            self.log_view = QPlainTextEdit()
            self.log_view.setReadOnly(True)
            self.log_view.setMaximumBlockCount(5000)
            self.log_view.setFont(QFont("Monospace", 9))
            self.runner = FlowgraphRunner(cfg, log=self.applog)
            self.telem = TelemetryClient(cfg["ports"]["telem_port"])
            self.core = self._make_core()
            self.responder = Responder(self.core, self.summary)
            self.tester = None
            self.report = None

            self.tabs = QTabWidget()
            self.chat = ChatPanel(self.core)
            self.tabs.addTab(self.chat, "Chat")
            self.tabs.addTab(self._build_dashboard(), "Dashboard")
            self.tabs.addTab(self._build_settings(), "Settings")
            self.tabs.addTab(self.log_view, "Log")
            self.setCentralWidget(self.tabs)
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.tick)
            self.timer.start(50)
            self._t_dash = 0.0
            if cfg["auto_start"]:
                self.runner.start()

        # ---------- plumbing ----------
        def applog(self, text):
            self.log_view.appendPlainText(text if text[:2].isdigit() else f"{datetime.now():%H:%M:%S}  {text}")

        def _make_core(self):
            c = self.cfg
            core = ChatCore(name=c["name"] or None, inbox=c["inbox"], arq_port=c["ports"]["arq_port"],
                            udp_port=c["ports"]["udp_port"], udp_out_port=c["ports"]["udp_out_port"],
                            beacon_s=c["beacon_s"], node_id=c["node_id"])
            core.set_log(self.applog)
            return core

        def summary(self):
            return node_summary(self.cfg, self.telem, self.runner, self.core)

        def tick(self):
            for line in self.runner.poll():
                self.log_view.appendPlainText(f"[fg] {line}")
            self.core.poll()
            self.telem.poll()
            if self.tester is not None and not self.tester.done:
                self.tester.poll()
                if self.tester.done:
                    self.finish_test()
            self.chat.refresh()
            now = time.monotonic()
            if now - self._t_dash >= 1.0:
                self._t_dash = now
                self.refresh_status()
                if self.tabs.currentIndex() == 1:
                    self.refresh_dashboard()

        def refresh_status(self):
            rs = self.runner.summary()
            probs = (self.telem.latest or {}).get("active_problems") or [] if self.telem.fresh() else []
            self.statusBar().showMessage(
                f"Node {self.cfg['node_id']} · flowgraph {rs['status']} · radio "
                f"{'connected' if self.core.arq_ok else 'not connected'} · telemetry "
                f"{'live' if self.telem.fresh() else '—'} · {len(probs)} active problem(s)")

        # ---------- dashboard ----------
        def _build_dashboard(self):
            w = QWidget()
            v = QVBoxLayout(w)
            self.dash_tabs = QTabWidget()
            mon = QWidget()
            mv = QVBoxLayout(mon)
            row = QHBoxLayout()
            for text, fn in (("Report (this node, last 10 min)", self.monitor_report), ("Dump stacks", self.dump_stacks),
                             ("Open logs folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(APP_DIR))))):
                b = QPushButton(text)
                b.clicked.connect(fn)
                row.addWidget(b)
            row.addStretch(1)
            mv.addLayout(row)
            self.monitor_view = QTextBrowser()
            mv.addWidget(self.monitor_view, 1)
            self.dash_tabs.addTab(mon, "Live monitor (while the apps run)")
            an = QWidget()
            av = QVBoxLayout(an)
            row = QHBoxLayout()
            row.addWidget(QLabel("Peer node"))
            self.peer_box = QSpinBox()
            self.peer_box.setRange(0, 254)
            self.peer_box.setValue(int(self.cfg["peer"] if self.cfg["peer"] is not None else 0))
            row.addWidget(self.peer_box)
            row.addWidget(QLabel("Test"))
            self.profile_box = QComboBox()
            self.profile_box.addItems(list(PROFILES))
            row.addWidget(self.profile_box)
            self.test_btn = QPushButton("Run analysis")
            self.test_btn.clicked.connect(self.start_test)
            row.addWidget(self.test_btn)
            self.stop_btn = QPushButton("Stop")
            self.stop_btn.clicked.connect(self.stop_test)
            row.addWidget(self.stop_btn)
            b = QPushButton("Open reports folder")
            b.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(APP_DIR / "reports"))))
            row.addWidget(b)
            row.addStretch(1)
            av.addLayout(row)
            self.test_label = QLabel("Runs packet tests both ways, a reliable transfer and asks the peer for its "
                                     "diagnostics. The peer only needs this app running.")
            self.test_label.setWordWrap(True)
            av.addWidget(self.test_label)
            self.report_view = QTextBrowser()
            av.addWidget(self.report_view, 1)
            self.dash_tabs.addTab(an, "Run analysis")
            v.addWidget(self.dash_tabs)
            return w

        def refresh_dashboard(self):
            if self.dash_tabs.currentIndex() == 0:
                sb = self.monitor_view.verticalScrollBar()
                pos = sb.value()
                self.monitor_view.setHtml(render_monitor_html(self.telem.latest if self.telem.fresh() else None,
                                                              self.telem, self.runner, self.core, self.cfg))
                sb.setValue(pos)
            elif self.tester is not None and not self.tester.done:
                self.test_label.setText(f"Running ({time.time() - self.tester.t0:.0f} s): {self.tester.progress}")

        def dump_stacks(self):
            ok = self.telem.send({"cmd": "stacks"})
            started = self.runner.started_at or 0
            if not ok and self.telem.t_last > started:
                # telemetry socket gone but the telemetry block ran in this flowgraph run: its SIGUSR1 handler is
                # installed (without it SIGUSR1 would terminate the flowgraph, so never send it blindly)
                ok = self.runner.dump_stacks()
            self.applog(f"stack dump requested ({'ok' if ok else 'flowgraph not running'}): "
                        f"{APP_DIR}/stacks_node{self.cfg['node_id']}.txt")

        def monitor_report(self):
            local = self.summary()
            rep = {"title": f"CDP link report, node {self.cfg['node_id']} (last 10 minutes)", "time": time.time(),
                   "local": local, "remote": None, "pings": [], "bulk": None, "cfg": self.cfg.data,
                   "findings": analyze(local, cfg=self.cfg.data)}
            path = save_report(rep)
            self.report_view.setHtml(render_report(rep))
            self.dash_tabs.setCurrentIndex(1)
            self.test_label.setText(f"Report saved: {path}")

        def start_test(self):
            if self.tester is not None and not self.tester.done:
                return
            peer = self.peer_box.value()
            if peer == self.cfg["node_id"]:
                QMessageBox.information(self, "Analysis", "Pick the other node's ID as peer.")
                return
            self.cfg["peer"] = peer
            self.cfg.save()
            self.tester = LinkTester(self.core, self.telem, peer, self.profile_box.currentText(), log=self.applog)
            self.responder.tester = self.tester
            self.report_view.setHtml("<p>Running…</p>")

        def stop_test(self):
            if self.tester is not None and not self.tester.done:
                self.tester.stop()
                self.finish_test()

        def finish_test(self):
            t = self.tester
            local = self.summary()
            pings = t.results()
            rep = {"title": f"CDP link analysis, node {self.cfg['node_id']} ↔ node {t.peer} ({t.profile})",
                   "time": time.time(), "local": local, "remote": t.res.get("remote"), "pings": pings,
                   "bulk": t.res.get("bulk"), "cfg": self.cfg.data,
                   "findings": analyze(local, t.res.get("remote"), pings, t.res.get("bulk"), cfg=self.cfg.data)}
            if t.res.get("remote") is None:
                rep["findings"].insert(0, {"severity": "warning", "area": "test", "title": "the peer sent no diagnostics",
                                           "evidence": "no answer to the diagnostic request",
                                           "fix": "Run this app on the peer too, and check that the link works at all."})
            path = save_report(rep)
            self.report = rep
            self.report_view.setHtml(render_report(rep))
            self.test_label.setText(f"{'Stopped' if t.error else 'Finished'} · report saved: {path}")

        # ---------- settings ----------
        def _build_settings(self):
            outer = QScrollArea()
            outer.setWidgetResizable(True)
            w = QWidget()
            form = QFormLayout(w)
            c = self.cfg
            self.node_box = QSpinBox()
            self.node_box.setRange(0, 254)
            self.node_box.setValue(int(c["node_id"]))
            form.addRow("This node's ID", self.node_box)
            self.name_box = QLineEdit(c["name"])
            form.addRow("Display name", self.name_box)
            self.grc_box = QLineEdit(c["grc"])
            form.addRow("Flowgraph (.grc)", self.grc_box)
            self.auto_box = QCheckBox("Start the flowgraph with the app")
            self.auto_box.setChecked(bool(c["auto_start"]))
            form.addRow(self.auto_box)
            self.restart_box = QCheckBox("Restart the flowgraph if it stops")
            self.restart_box.setChecked(bool(c["auto_restart"]))
            form.addRow(self.restart_box)
            self.gui_box = QCheckBox("Show the flowgraph window (costs CPU)")
            self.gui_box.setChecked(bool(c["show_flowgraph_gui"]))
            form.addRow(self.gui_box)
            self.param_boxes = {}
            form.addRow(QLabel("<b>Link parameters</b> (Apply now changes them in the running flowgraph; they are "
                               "also used at every start)"))
            for key, env, label, typ, dflt, _, _ in PARAMS:
                if typ is int:
                    box = QSpinBox()
                    box.setRange(0, 100000)
                else:
                    box = QDoubleSpinBox()
                    box.setDecimals(3)
                    box.setRange(0.0, 1000.0)
                    box.setSingleStep(0.001 if dflt < 0.1 else 0.5)
                box.setValue(typ(c["params"].get(key, dflt)))
                self.param_boxes[key] = box
                form.addRow(label, box)
            form.addRow(QLabel("<b>Ports</b> (need a flowgraph restart)"))
            self.port_boxes = {}
            for key, env, dflt in PORTS:
                box = QSpinBox()
                box.setRange(1024, 65535)
                box.setValue(int(c["ports"][key]))
                self.port_boxes[key] = box
                form.addRow(f"{key} ({env})", box)
            row = QHBoxLayout()
            for text, fn in (("Apply now", self.apply_live), ("Save && restart flowgraph", self.save_restart),
                             ("Start", self.runner.start), ("Stop", self.runner.stop), ("Defaults", self.defaults)):
                b = QPushButton(text)
                b.clicked.connect(fn)
                row.addWidget(b)
            form.addRow(row)
            outer.setWidget(w)
            return outer

        def _read_settings(self):
            c = self.cfg
            old = json.loads(json.dumps(c.data))
            c["node_id"] = self.node_box.value()
            c["name"] = self.name_box.text().strip()
            c["grc"] = self.grc_box.text().strip()
            c["auto_start"] = self.auto_box.isChecked()
            c["auto_restart"] = self.restart_box.isChecked()
            c["show_flowgraph_gui"] = self.gui_box.isChecked()
            for key, *_ in PARAMS:
                c["params"][key] = self.param_boxes[key].value()
            for key, *_ in PORTS:
                c["ports"][key] = self.port_boxes[key].value()
            c.save()
            return old

        def apply_live(self):
            old = self._read_settings()
            n = 0
            for key, env, label, typ, dflt, target, attr in PARAMS:
                if old["params"].get(key) != self.cfg["params"][key]:
                    if self.telem.send({"cmd": "set", "target": target, "key": attr,
                                        "value": typ(self.cfg["params"][key])}):
                        n += 1
            restart = any(old[k] != self.cfg[k] for k in ("node_id", "grc", "ports", "show_flowgraph_gui"))
            self.applog(f"settings saved; {n} parameter(s) changed in the running flowgraph" +
                        ("; node ID / ports / file changed: use 'Save & restart flowgraph'" if restart else ""))

        def save_restart(self):
            old = self._read_settings()
            if old["node_id"] != self.cfg["node_id"] or old["ports"] != self.cfg["ports"] or old["name"] != self.cfg["name"]:
                self.core.close()
                self.core = self._make_core()
                self.responder = Responder(self.core, self.summary)
                self.chat.set_core(self.core)
                self.telem.close()
                self.telem = TelemetryClient(self.cfg["ports"]["telem_port"])
            self.runner.restart()

        def defaults(self):
            for key, env, label, typ, dflt, _, _ in PARAMS:
                self.param_boxes[key].setValue(typ(dflt))

        def closeEvent(self, e):
            self.runner.stop()
            self.core.close()
            e.accept()


# =====================================================================
def main():
    ap = argparse.ArgumentParser(description="CDP link app: chat, dashboard and flowgraph launcher (v6.7)")
    ap.add_argument("--node", type=int, help="this node's ID (saved in ~/.cdp_link/config.json)")
    ap.add_argument("--name", help="display name")
    ap.add_argument("--grc", help="flowgraph file (default: v6.7_cdp_link.grc next to this app)")
    ap.add_argument("--no-flowgraph", action="store_true", help="don't start the flowgraph (it runs elsewhere)")
    ap.add_argument("--config", help="config file (default ~/.cdp_link/config.json)")
    ap.add_argument("--install-autostart", action="store_true",
                    help="start this app (and with it the flowgraph) automatically when you log in")
    ap.add_argument("--remove-autostart", action="store_true", help="undo --install-autostart")
    args = ap.parse_args()
    cfg = Config(args.config)
    if args.node is not None:
        cfg["node_id"] = args.node
    if args.name:
        cfg["name"] = args.name
    if args.grc:
        cfg["grc"] = os.path.abspath(args.grc)
    if args.no_flowgraph:
        cfg["auto_start"] = False
    cfg.save()
    desk = Path(os.path.expanduser("~/.config/autostart/cdp-link.desktop"))
    if args.install_autostart:
        desk.parent.mkdir(parents=True, exist_ok=True)
        extra = f" --config {os.path.abspath(args.config)}" if args.config else ""
        desk.write_text("[Desktop Entry]\nType=Application\nName=CDP Link\n"
                        f"Exec=sh -c 'sleep 10; exec {sys.executable} {os.path.abspath(__file__)}{extra}'\n"
                        "X-GNOME-Autostart-enabled=true\nTerminal=false\n")
        print(f"autostart installed: {desk} (node {cfg['node_id']}, settings in {cfg.path})")
        return
    if args.remove_autostart:
        desk.unlink(missing_ok=True)
        print("autostart removed")
        return
    if not HAVE_QT:
        sys.exit("PyQt5 not found: sudo dnf install python3-qt5  (Ubuntu: sudo apt install python3-pyqt5)")
    app = QApplication(sys.argv)
    app.setApplicationName("CDP Link")
    w = MainWindow(cfg)
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
