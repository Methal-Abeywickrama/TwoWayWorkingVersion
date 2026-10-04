#!/usr/bin/env python3
"""
cdp_chat.py - a very small Slack-style chat for the CDP transceiver link.

Put it in the same folder as folder_sync_daemon.py. It never talks to the radio
itself: it writes messages as small JSON files into outbox/ and reads what the
daemon drops into inbox/. Delivery status comes from where the daemon moves
your files (outbox/ = sending, outbox/sent/ = delivered, outbox/failed/ = failed).

    python3 cdp_chat.py --daemon          # also starts folder_sync_daemon.py for you
    python3 cdp_chat.py                   # if you run the daemon in another terminal

Where messages go: ONE destination for everything (all channels) - the
transport address/port in the sidebar's "Link" section. Channels are only labels
carried inside each message. Change the destination with "Change destination…"
(writes link.json, which the daemon re-reads before every send).

Options:  --root DIR   folder holding outbox/ and inbox/ (default: this folder)
          --name NAME  your display name (default: "node <TL_LOCAL_ADDR>")
          --daemon     start folder_sync_daemon.py as a child process (log in the app)
          --peer N     peer address passed to the daemon (default: TL_PEER_ADDR)

Needs PyQt5 (already installed with GNU Radio's Qt GUI; else: sudo apt install python3-pyqt5).
"""
import argparse
import hashlib
import html
import json
import os
import shutil
import socket
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

KIND = "cdp-chat"
CHAT_PREFIX, CHAT_SUFFIX = "chat_", ".json"
MAX_TEXT = 4000
STATUS_RANK = {"sent": 0, "unconfirmed": 1, "queued": 2, "failed": 3}


# =====================================================================
#  Folder-backed message store (no Qt in here, so it can be tested alone)
# =====================================================================
class Msg:
    __slots__ = ("id", "ts", "author", "channel", "text", "attachment",
                 "mine", "status", "path", "is_file", "src", "dst")

    def __init__(self, **kw):
        self.attachment = None
        self.text = ""
        self.is_file = False
        self.src = None        # received: {"addr", "port"} as reported by the transport layer
        self.dst = None        # sent: {"addr", "port"} the message was addressed to
        for k, v in kw.items():
            setattr(self, k, v)


def _skip(p):
    n = p.name
    return (not p.is_file()) or n.startswith(".") or n.endswith((".part", "~", ".tmp"))


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


class ChatStore:
    def __init__(self, root, author, node=0):
        self.root = Path(root)
        self.author, self.node = author, node
        self.outbox = self.root / "outbox"
        self.sent = self.outbox / "sent"
        self.failed = self.outbox / "failed"
        self.unconfirmed = self.outbox / "unconfirmed"
        self.inbox = self.root / "inbox"
        for d in (self.outbox, self.sent, self.failed, self.unconfirmed, self.inbox):
            d.mkdir(parents=True, exist_ok=True)
        self.chan_file = self.root / ".chat_channels.json"
        self.msgs = []                 # sorted by ts
        self.by_id = {}
        self._cache = {}               # path -> (mtime, parsed json or None)
        self._sig = None
        self.peer_name = "Peer"
        self.link_file = self.root / "link.json"
        self.status_file = self.root / ".daemon_status.json"
        self.rx_log_file = self.root / ".rx_log.jsonl"
        self.rx_src = {}               # received file name -> {"addr", "port"}
        self._rx_log_size = -1

    # ---------- link (destination) ----------
    def daemon_status(self):
        """The daemon's heartbeat file, or None if no daemon is running for this folder."""
        try:
            st = json.loads(self.status_file.read_text())
            return st if time.time() - float(st.get("ts", 0)) < 8 else None
        except Exception:
            return None

    def get_link(self):
        """(peer_addr, peer_port) messages are sent to, or (None, None) if unknown."""
        try:
            d = json.loads(self.link_file.read_text())
            return int(d["peer"]), int(d.get("port", 0))
        except Exception:
            pass
        st = self.daemon_status()
        if st:
            return int(st.get("peer", 0)), int(st.get("port", 0))
        return None, None

    def set_link(self, peer, port):
        peer, port = int(peer), int(port)
        if not (0 <= peer <= 255 and 0 <= port <= 255):
            raise ValueError("address and port must be 0-255")
        self._atomic_write(self.link_file, json.dumps({"peer": peer, "port": port}).encode())

    def _load_rx_log(self):
        try:
            size = self.rx_log_file.stat().st_size
        except OSError:
            return
        if size == self._rx_log_size:
            return
        self._rx_log_size = size
        src = {}
        for line in self.rx_log_file.read_text(errors="replace").splitlines():
            try:
                d = json.loads(line)
                if d.get("src_addr") is not None:
                    src[d["file"]] = {"addr": d["src_addr"], "port": d.get("src_port", 0)}
            except Exception:
                continue
        self.rx_src = src

    # ---------- channels ----------
    def saved_channels(self):
        try:
            return [c for c in json.loads(self.chan_file.read_text()) if isinstance(c, str)]
        except Exception:
            return []

    def add_channel(self, name):
        name = clean_channel(name)
        if not name:
            return None
        chans = self.saved_channels()
        if name not in chans:
            chans.append(name)
            self.chan_file.write_text(json.dumps(chans))
        return name

    def channels(self):
        out = ["general"]
        for c in self.saved_channels() + [m.channel for m in self.msgs]:
            if c not in out:
                out.append(c)
        return out

    # ---------- reading ----------
    def _parse(self, p):
        if not (p.name.startswith(CHAT_PREFIX) and p.name.endswith(CHAT_SUFFIX)):
            return None
        try:
            mt = p.stat().st_mtime
        except OSError:
            return None
        hit = self._cache.get(p)
        if hit and hit[0] == mt:
            return hit[1]
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if not (isinstance(d, dict) and d.get("kind") == KIND and "id" in d):
                d = None
        except Exception:
            d = None
        self._cache[p] = (mt, d)
        return d

    def scan(self):
        """Re-index the folders. Returns True if anything visible changed."""
        self._load_rx_log()
        chats, files = [], []
        for folder, status, mine in ((self.outbox, "queued", True), (self.sent, "sent", True),
                                     (self.failed, "failed", True), (self.unconfirmed, "unconfirmed", True),
                                     (self.inbox, "received", False)):
            try:
                entries = list(folder.iterdir())
            except OSError:
                continue
            for p in entries:
                if _skip(p):
                    continue
                d = self._parse(p)
                if d is not None:
                    chats.append((d, status, mine, p))
                else:
                    files.append((p, status, mine))

        msgs, by_id, attached = [], {}, {}
        for d, status, mine, p in chats:
            if d["id"] in by_id:             # duplicate delivery -> keep the first
                continue
            att = d.get("attachment") if isinstance(d.get("attachment"), dict) else None
            m = Msg(id=d["id"], ts=float(d.get("ts", 0)), author=str(d.get("author", "?")),
                    channel=clean_channel(d.get("channel", "general")) or "general",
                    text=str(d.get("text", ""))[:MAX_TEXT], attachment=att,
                    mine=mine, status=status, path=p)
            if mine:
                m.dst = d.get("dst") if isinstance(d.get("dst"), dict) else None
            else:
                m.src = self.rx_src.get(p.name)
                if m.src is None and d.get("node"):
                    m.src = {"addr": d["node"], "port": None}     # sender's own claim
            if att and att.get("file"):
                attached[(mine, att["file"])] = m
            if not mine:
                self.peer_name = m.author
            msgs.append(m)
            by_id[m.id] = m

        for p, status, mine in files:
            owner = attached.get((mine, p.name))
            if owner is not None:            # file belongs to a chat message
                if mine and STATUS_RANK.get(status, 0) > STATUS_RANK.get(owner.status, 0):
                    owner.status = status    # show the worse of message/attachment
                owner.attachment["path"] = str(p)
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            fid = "f" + hashlib.sha1(str(p).encode()).hexdigest()[:12]
            m = Msg(id=fid, ts=st.st_mtime, author=self.author if mine else self.peer_name,
                    channel="general", mine=mine, status=status, path=p, is_file=True,
                    attachment={"name": p.name, "file": p.name, "size": st.st_size, "path": str(p)})
            if not mine:
                m.src = self.rx_src.get(p.name)
            msgs.append(m)
            by_id[fid] = m

        msgs.sort(key=lambda m: (m.ts, m.id))
        sig = tuple((m.id, m.status, bool(m.attachment and m.attachment.get("path")), str(m.src))
                    for m in msgs)
        changed = sig != self._sig
        self._sig, self.msgs, self.by_id = sig, msgs, by_id
        return changed

    def stats(self):
        q = [m for m in self.msgs if m.mine and m.status == "queued"]
        f = [m for m in self.msgs if m.mine and m.status == "failed"]
        oldest = min((m.ts for m in q), default=None)
        self.n_unconfirmed = sum(1 for m in self.msgs if m.mine and m.status == "unconfirmed")
        return len(q), len(f), oldest

    # ---------- writing ----------
    @staticmethod
    def _atomic_write(dst, data):
        tmp = dst.with_name("." + dst.name + ".part")    # daemon ignores dot/.part files
        tmp.write_bytes(data)
        os.replace(tmp, dst)

    def send_text(self, channel, text, attachment=None):
        text = (text or "").strip()[:MAX_TEXT]
        if not text and not attachment:
            return None
        mid = uuid.uuid4().hex[:12]
        ts = time.time()
        peer, port = self.get_link()
        d = {"kind": KIND, "v": 1, "id": mid, "ts": ts, "author": self.author,
             "node": self.node, "channel": clean_channel(channel) or "general", "text": text,
             "attachment": attachment,
             "dst": None if peer is None else {"addr": peer, "port": port}}
        name = f"{CHAT_PREFIX}{int(ts * 1000)}_{mid}{CHAT_SUFFIX}"
        self._atomic_write(self.outbox / name, json.dumps(d, ensure_ascii=False).encode("utf-8"))
        return mid

    def send_file(self, src, channel, text=""):
        src = Path(src)
        tag = uuid.uuid4().hex[:4]
        sent_name = f"{src.stem}_{tag}{src.suffix}"          # unique name on the receiver
        dst = self.outbox / sent_name
        tmp = dst.with_name("." + dst.name + ".part")
        shutil.copyfile(src, tmp)                            # fresh mtime -> sent before the message
        os.replace(tmp, dst)
        att = {"name": src.name, "file": sent_name, "size": src.stat().st_size}
        return self.send_text(channel, text, attachment=att)

    def retry(self, mid):
        m = self.by_id.get(mid)
        if not m or not m.mine:
            return
        for p in [m.path] + ([Path(m.attachment["path"])] if m.attachment and m.attachment.get("path") else []):
            if p.parent in (self.failed, self.unconfirmed) and p.exists():
                dst = self.outbox / p.name
                os.replace(p, dst)
                os.utime(dst)          # fresh age, so the daemon's --max-age doesn't expire it again


def clean_channel(name):
    name = str(name or "").strip().lstrip("#").lower().replace(" ", "-")
    return "".join(c for c in name if c.isalnum() or c in "-_")[:40]


# =====================================================================
#  Qt user interface
# =====================================================================
try:
    from PyQt5.QtCore import Qt, QTimer, QUrl, QProcess, pyqtSignal
    from PyQt5.QtGui import QDesktopServices, QFont, QBrush, QColor
    from PyQt5.QtWidgets import (
        QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QLabel,
        QListWidget, QListWidgetItem, QPushButton, QTextBrowser, QPlainTextEdit,
        QFileDialog, QInputDialog, QDockWidget, QMessageBox, QFrame,
        QDialog, QFormLayout, QSpinBox, QDialogButtonBox)
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
        def __init__(self, store, args):
            super().__init__()
            self.store, self.args = store, args
            self.channel = "general"
            self.last_read = {}
            self.first_scan = True
            self.known_rx = set()
            self.proc = None

            self.setWindowTitle(f"CDP Link — {store.author}")
            self.resize(980, 680)
            self.setAcceptDrops(True)
            self.setStyleSheet(SIDEBAR_CSS)

            # ---- sidebar ----
            side = QWidget(objectName="sidebar")
            side.setFixedWidth(230)
            sl = QVBoxLayout(side)
            sl.setContentsMargins(12, 14, 12, 12)
            sl.addWidget(QLabel("CDP Link", objectName="workspace"))
            self.me_label = QLabel(f"● {store.author}")
            self.me_label.setStyleSheet("color:#2BAC76;")
            sl.addWidget(self.me_label)
            sl.addSpacing(14)
            sl.addWidget(QLabel("Channels"))
            self.chan_list = QListWidget()
            self.chan_list.currentItemChanged.connect(self.on_channel_changed)
            sl.addWidget(self.chan_list, 1)
            add_btn = QPushButton("+  Add channel")
            add_btn.clicked.connect(self.on_add_channel)
            sl.addWidget(add_btn)
            line = QFrame()
            line.setFrameShape(QFrame.HLine)
            line.setStyleSheet("color:#5B2C5C;")
            sl.addWidget(line)
            self.queue_label = QLabel("")
            self.queue_label.setWordWrap(True)
            sl.addWidget(self.queue_label)
            sl.addSpacing(8)
            sl.addWidget(QLabel("<b>Link</b>"))
            self.link_label = QLabel("")
            self.link_label.setWordWrap(True)
            sl.addWidget(self.link_label)
            link_btn = QPushButton("⚙  Change destination…")
            link_btn.clicked.connect(self.on_link_settings)
            sl.addWidget(link_btn)
            self.daemon_label = QLabel("")
            self.daemon_label.setWordWrap(True)
            sl.addWidget(self.daemon_label)
            self.log_btn = QPushButton("Show daemon log")
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
            self.attach_btn.setToolTip("Send a file (or drag & drop onto the window)")
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
            hint = QLabel("Enter to send · Shift+Enter for a new line")
            hint.setStyleSheet("color:#999; font-size:11px;")
            wl.addWidget(hint)
            ml.addWidget(wrap)

            root = QWidget()
            rl = QHBoxLayout(root)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(0)
            rl.addWidget(side)
            rl.addWidget(main, 1)
            self.setCentralWidget(root)

            # ---- daemon log dock ----
            self.log_view = QPlainTextEdit()
            self.log_view.setReadOnly(True)
            self.log_view.setMaximumBlockCount(3000)
            self.log_view.setFont(QFont("Monospace", 9))
            self.dock = QDockWidget("Daemon log", self)
            self.dock.setWidget(self.log_view)
            self.addDockWidget(Qt.BottomDockWidgetArea, self.dock)
            self.dock.hide()
            self.dock.visibilityChanged.connect(
                lambda v: self.log_btn.setText("Hide daemon log" if v else "Show daemon log"))

            if args.daemon:
                self.start_daemon()
            else:
                self.log_btn.hide()

            self.timer = QTimer(self)
            self.timer.timeout.connect(self.poll)
            self.timer.start(700)
            self.poll()
            self.update_send_enabled()
            self.composer.setFocus()

        # ---------------- daemon process ----------------
        def start_daemon(self):
            script = Path(__file__).resolve().parent / "folder_sync_daemon.py"
            if not script.exists():
                self.daemon_label.setText("Daemon: folder_sync_daemon.py not found")
                return
            self.proc = QProcess(self)
            self.proc.setProcessChannelMode(QProcess.MergedChannels)
            self.proc.readyReadStandardOutput.connect(self.on_daemon_output)
            self.proc.finished.connect(self.on_daemon_finished)
            argv = ["-u", str(script), "--root", str(self.store.root)]
            if self.args.peer is not None:
                argv += ["--peer", str(self.args.peer)]
            self.proc.start(sys.executable, argv)
            self.daemon_label.setText("Daemon: <span style='color:#2BAC76'>running</span>")

        def on_daemon_output(self):
            text = bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")
            for line in text.splitlines():
                self.log_view.appendPlainText(line)

        def on_daemon_finished(self, code, _status=None):
            self.daemon_label.setText(
                f"Daemon: <span style='color:#E01E5A'>stopped (exit {code})</span> — see log")
            self.dock.show()

        def toggle_log(self):
            self.dock.setVisible(not self.dock.isVisible())

        def closeEvent(self, e):
            if self.proc and self.proc.state() != QProcess.NotRunning:
                self.proc.terminate()
                if not self.proc.waitForFinished(2000):
                    self.proc.kill()
            super().closeEvent(e)

        # ---------------- polling & rendering ----------------
        def poll(self):
            changed = self.store.scan()
            if self.first_scan:
                for c in self.store.channels():
                    self.last_read[c] = max((m.ts for m in self.store.msgs if m.channel == c), default=0)
                self.known_rx = {m.id for m in self.store.msgs if not m.mine}
                self.first_scan = False
                changed = True
            new_rx = [m for m in self.store.msgs if not m.mine and m.id not in self.known_rx]
            if new_rx:
                self.known_rx.update(m.id for m in new_rx)
                if not self.isActiveWindow():
                    QApplication.alert(self)
            if changed:
                self.refresh_channels()
                self.render()
            self.refresh_status()

        def refresh_channels(self):
            self.chan_list.blockSignals(True)
            self.chan_list.clear()
            for c in self.store.channels():
                unread = 0
                if c != self.channel:
                    unread = sum(1 for m in self.store.msgs
                                 if m.channel == c and not m.mine and m.ts > self.last_read.get(c, 0))
                item = QListWidgetItem(f"#  {c}" + (f"   ({unread})" if unread else ""))
                item.setData(Qt.UserRole, c)
                if unread:
                    f = item.font()
                    f.setBold(True)
                    item.setFont(f)
                    item.setForeground(QBrush(QColor("white")))
                self.chan_list.addItem(item)
                if c == self.channel:
                    self.chan_list.setCurrentItem(item)
            self.chan_list.blockSignals(False)

        def refresh_status(self):
            q, f, oldest = self.store.stats()
            parts = []
            if q:
                parts.append(f"{q} sending")
            if f:
                parts.append(f"<span style='color:#E01E5A'>{f} failed</span>")
            if self.store.n_unconfirmed:
                parts.append(f"<span style='color:#ECB22E'>{self.store.n_unconfirmed} unconfirmed"
                             f" (FSM gives no delivery reports)</span>")
            txt = " · ".join(parts) or "All messages delivered"
            if oldest and time.time() - oldest > 45:
                txt += ("<br><span style='color:#ECB22E'>Oldest has waited "
                        f"{int(time.time() - oldest)} s — is the flowgraph running?</span>")
            self.queue_label.setText(txt)
            self.refresh_link()

        def link_text(self):
            peer, port = self.store.get_link()
            if peer is None:
                return "<span style='color:#ECB22E'>unknown — set it with Change destination</span>"
            p = "any" if port == 0 else str(port)
            return f"addr {peer}{' (broadcast)' if peer == 0 else ''} · port {p}"

        def refresh_link(self):
            st = self.store.daemon_status()
            me = st.get("local_addr") if st else None
            me = me if me else (self.store.node or "?")
            mine = f"This node: addr {me}"
            if st and st.get("local_port"):
                mine += f" · port {st['local_port']}"
            self.link_label.setText(f"{mine}<br>Sending to: {self.link_text()}")
            if self.proc is not None and self.proc.state() == QProcess.NotRunning:
                return                                   # keep the 'stopped (exit N)' text
            if st:
                ok = "" if st.get("confirmations") else " (no delivery confirmations yet)"
                self.daemon_label.setText(f"Daemon: <span style='color:#2BAC76'>running</span>"
                                          f" · ZMQ {st.get('zmq_tx')}/{st.get('zmq_rx')}{ok}")
            else:
                self.daemon_label.setText("Daemon: <span style='color:#E01E5A'>not detected</span>"
                                          " — start folder_sync_daemon.py or use --daemon")

        def on_link_settings(self):
            peer, port = self.store.get_link()
            dlg = QDialog(self)
            dlg.setWindowTitle("Destination")
            form = QFormLayout(dlg)
            info = QLabel("All channels are sent to this one destination — channels are just "
                          "labels inside each message.<br><br>"
                          "<b>Address</b>: the other node's TL_LOCAL_ADDR (0 = broadcast).<br>"
                          "<b>Port</b>: 0 = any. The receiver only checks it if its flowgraph "
                          "has TL_LOCAL_PORT set; a mismatch is dropped silently.")
            info.setWordWrap(True)
            form.addRow(info)
            addr_box, port_box = QSpinBox(), QSpinBox()
            for b in (addr_box, port_box):
                b.setRange(0, 255)
            addr_box.setValue(peer if peer is not None else 0)
            port_box.setValue(port or 0)
            form.addRow("Peer address", addr_box)
            form.addRow("Destination port", port_box)
            btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            btns.accepted.connect(dlg.accept)
            btns.rejected.connect(dlg.reject)
            form.addRow(btns)
            if dlg.exec_() == QDialog.Accepted:
                try:
                    self.store.set_link(addr_box.value(), port_box.value())
                except (OSError, ValueError) as e:
                    QMessageBox.warning(self, "Could not save destination", str(e))
                self.refresh_link()
                self.render()

        def render(self):
            sb = self.view.verticalScrollBar()
            at_bottom = sb.value() >= sb.maximum() - 4
            st = self.store.daemon_status()
            me = (st.get("local_addr") if st else None) or self.store.node or "?"
            self.header.setText(f"#  {self.channel}"
                                f"<span style='color:#888; font-weight:normal; font-size:12px'>"
                                f"   ·  you (addr {me})  →  {self.link_text()}"
                                f"   ·  peer: {html.escape(self.store.peer_name)}</span>")
            msgs = [m for m in self.store.msgs if m.channel == self.channel]
            self.view.setHtml(render_html(msgs, self.channel))
            if msgs:
                self.last_read[self.channel] = max(self.last_read.get(self.channel, 0), msgs[-1].ts)
            if at_bottom or self._force_bottom:
                QTimer.singleShot(0, lambda: sb.setValue(sb.maximum()))
            self._force_bottom = False

        _force_bottom = True

        # ---------------- actions ----------------
        def on_channel_changed(self, cur, _prev):
            if cur is None:
                return
            self.channel = cur.data(Qt.UserRole)
            self._force_bottom = True
            self.render()
            self.refresh_channels()

        def on_add_channel(self):
            name, ok = QInputDialog.getText(self, "Add channel", "Channel name:")
            if ok:
                c = self.store.add_channel(name)
                if c:
                    self.channel = c
                    self._force_bottom = True
                    self.store.scan()
                    self.refresh_channels()
                    self.render()

        def update_send_enabled(self):
            self.send_btn.setEnabled(bool(self.composer.toPlainText().strip()))

        def on_send(self):
            text = self.composer.toPlainText().strip()
            if not text:
                return
            try:
                self.store.send_text(self.channel, text)
            except OSError as e:
                QMessageBox.warning(self, "Could not queue message", str(e))
                return
            self.composer.clear()
            self._force_bottom = True
            self.poll()

        def on_attach(self):
            paths, _ = QFileDialog.getOpenFileNames(self, "Send file(s)")
            self.send_files(paths)

        def send_files(self, paths):
            caption = self.composer.toPlainText().strip()
            for i, p in enumerate(paths):
                try:
                    self.store.send_file(p, self.channel, caption if i == 0 else "")
                except OSError as e:
                    QMessageBox.warning(self, "Could not queue file", f"{p}\n{e}")
            if paths and caption:
                self.composer.clear()
            self._force_bottom = True
            self.poll()

        def dragEnterEvent(self, e):
            if e.mimeData().hasUrls():
                e.acceptProposedAction()

        def dropEvent(self, e):
            paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
            self.send_files([p for p in paths if os.path.isfile(p)])

        def on_link(self, url):
            action, mid = url.scheme(), url.path()
            m = self.store.by_id.get(mid)
            if m is None:
                return
            if action == "retry":
                self.store.retry(mid)
                self.poll()
            elif action == "open":
                p = m.attachment.get("path") if m.attachment else None
                if p and os.path.exists(p):
                    QDesktopServices.openUrl(QUrl.fromLocalFile(p))
                else:
                    QMessageBox.information(self, "Not here yet",
                                            "That file has not arrived (or was moved).")
            elif action == "folder":
                p = m.attachment.get("path") if m.attachment else None
                if p:
                    QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(p).parent)))


# =====================================================================
#  HTML for the message view (plain function so it can be tested)
# =====================================================================
COLORS = ["#1D9BD1", "#E01E5A", "#2BAC76", "#ECB22E", "#9B59B6", "#E67E22"]


def _color(name):
    return COLORS[int(hashlib.md5(name.encode()).hexdigest(), 16) % len(COLORS)]


def _route(m):
    """'from addr 2 · port 0' for received, 'to addr 2 · port any' for sent."""
    r = m.src if not m.mine else m.dst
    if not r or r.get("addr") is None:
        return ""
    port = r.get("port")
    if port is None:
        ptxt = ""
    elif m.mine:
        ptxt = f" · port {'any' if port == 0 else port}"
    else:
        ptxt = f" · port {port}"          # the sender's own TL_LOCAL_PORT
    return f"{'from' if not m.mine else 'to'} addr {r['addr']}{ptxt}"


def render_html(msgs, channel):
    out = ["<div style='font-family:sans-serif; font-size:14px; color:#1D1C1D'>"]
    if not msgs:
        out.append(f"<p style='color:#888; margin-top:24px'>This is the very beginning of "
                   f"<b>#{html.escape(channel)}</b>. Say hi!</p>")
    prev_day, prev = None, None
    for m in msgs:
        dt = datetime.fromtimestamp(m.ts)
        day = dt.date()
        if day != prev_day:
            label = "Today" if day == datetime.now().date() else dt.strftime("%A, %d %B")
            out.append(f"<p align='center' style='color:#888; font-size:12px; margin:14px 0 4px 0'>"
                       f"──────  {label}  ──────</p>")
            prev_day, prev = day, None
        route = _route(m)
        grouped = (prev is not None and prev.author == m.author and prev.mine == m.mine
                   and m.ts - prev.ts < 300 and _route(prev) == route)
        if not grouped:
            out.append(f"<p style='margin:10px 0 0 0'><b style='color:{_color(m.author)}'>"
                       f"{html.escape(m.author)}</b>&nbsp;&nbsp;"
                       f"<span style='color:#888; font-size:12px'>{dt:%H:%M}"
                       f"{'&nbsp;&nbsp;·&nbsp;&nbsp;' + route if route else ''}</span></p>")
        body = []
        if m.text:
            body.append(html.escape(m.text).replace("\n", "<br>"))
        if m.attachment:
            a = m.attachment
            name = html.escape(str(a.get("name", "file")))
            size = _human(int(a.get("size", 0) or 0))
            if a.get("path"):
                link = (f"<a href='open:{m.id}' style='color:#1264A3'>{name}</a> "
                        f"<span style='color:#888'>({size}) · <a href='folder:{m.id}' "
                        f"style='color:#888'>show in folder</a></span>")
            else:
                link = f"{name} <span style='color:#888'>({size}) · not received yet</span>"
            body.append(f"📎 {link}")
        status = ""
        if m.mine:
            status = {"queued": "<span style='color:#888'>  ⏳ sending…</span>",
                      "sent": "<span style='color:#2BAC76'>  ✓</span>",
                      "unconfirmed": (f"<span style='color:#ECB22E'>  ⚠ sent, delivery not confirmed · "
                                      f"<a href='retry:{m.id}' style='color:#ECB22E'>resend</a></span>"),
                      "failed": (f"<span style='color:#E01E5A'>  ✗ not delivered · "
                                 f"<a href='retry:{m.id}' style='color:#E01E5A'>retry</a></span>")
                      }.get(m.status, "")
        grouped_time = (f"<span style='color:#BBB; font-size:11px'>{dt:%H:%M}&nbsp;&nbsp;</span>"
                        if grouped else "")
        out.append(f"<p style='margin:2px 0 0 0'>{grouped_time}{'<br>'.join(body)}{status}</p>")
        prev = m
    out.append("</div>")
    return "".join(out)


# =====================================================================
def main():
    here = Path(__file__).resolve().parent
    addr = os.environ.get("TL_LOCAL_ADDR")
    ap = argparse.ArgumentParser(description="Slack-style chat over the CDP link")
    ap.add_argument("--root", type=Path, default=here)
    ap.add_argument("--name", default=f"node {addr}" if addr else socket.gethostname())
    ap.add_argument("--daemon", action="store_true", help="start folder_sync_daemon.py too")
    ap.add_argument("--peer", type=int, default=None)
    args = ap.parse_args()
    if not HAVE_QT:
        sys.exit("PyQt5 not found: sudo apt install python3-pyqt5")
    store = ChatStore(args.root.resolve(), args.name, int(addr or 0))
    app = QApplication(sys.argv)
    app.setApplicationName("CDP Link")
    w = ChatWindow(store, args)
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
