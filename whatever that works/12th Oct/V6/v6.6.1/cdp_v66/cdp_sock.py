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

Launcher (pick an app; only one of them can use the UDP block's port 5201 at a time)
  python3 cdp_sock.py launch                     window (terminal menu without a display or with --text)
  python3 cdp_sock.py launch chat --name Methal  start the chat directly
  python3 cdp_sock.py launch game --peer 5       Radio Arena against node 5 (this node = $CDP_MY_ID)
  python3 cdp_sock.py launch game --bot          Radio Arena offline against a bot

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
import json
import os
import random
import select
import socket
import struct
import subprocess
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


# =====================================================================
#  Launcher: pick the chat app or the game (python3 cdp_sock.py launch)
# =====================================================================
HERE = os.path.dirname(os.path.abspath(__file__))
CHAT_APP = os.path.join(HERE, "cdp_chat_v66.py")
GAME_APP = os.path.join(HERE, "game", "fps_game.py")
SETTINGS = os.path.expanduser("~/.cdp_chat/launcher.json")
APP_TITLE = {"chat": "Chat", "game": "Radio Arena (game)", "bot": "Radio Arena vs bot (offline)"}


def env_node():
    v = _env("CDP_MY_ID", "").strip()
    return int(v) if v.isdigit() else None


def _udp_port_in_use(addr, port):
    """True if some program is bound to udp addr:port (probe bind, no SO_REUSEADDR, released at once)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((addr, int(port)))
        return False
    except OSError:
        return True
    finally:
        s.close()


def flowgraph_running():
    """True / False if the v6.6 flowgraph's UDP block is (not) listening on this PC, None if CDP_HOST is remote.
    Checked by a bind probe on the UDP port, NOT by connecting to the ARQ block: a connection would make the
    ARQ block hand its kept messages to the probe, and they would be lost for the chat."""
    host = _env("CDP_HOST", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost", "0.0.0.0"):
        return None
    return _udp_port_in_use(_env("CDP_BIND", "127.0.0.1"), _env("CDP_UDP_PORT", "5200"))


def rx_port_busy():
    """True if another program already receives on the UDP block's delivery port (chat, game, udp-listen)."""
    return _udp_port_in_use(_env("CDP_UDP_BIND", "127.0.0.1"), _env("CDP_UDP_OUT_PORT", "5201"))


def known_peers(node):
    """{node: name} from the chat history of this node (nodes the chat has seen or you added)."""
    peers = {}
    if node is None:
        return peers
    p = os.path.expanduser(f"~/.cdp_chat/history_node{node}.jsonl")
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"p"' not in line:
                    continue
                try:
                    n, name = json.loads(line)["p"]
                    if int(n) != node:
                        peers[int(n)] = name
                except (ValueError, KeyError, TypeError):
                    pass
    except OSError:
        pass
    return peers


def load_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(d):
    try:
        os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
        with open(SETTINGS, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
    except OSError:
        pass


def missing_modules(app):
    """Python modules the app needs that are not installed, with the Fedora install command."""
    import importlib.util
    need = {"chat": [("PyQt5", "python3-qt5")],
            "game": [("pygame", "python3-pygame"), ("OpenGL", "python3-pyopengl")]}[
        "game" if app in ("game", "bot") else "chat"]
    miss = [pkg for mod, pkg in need if importlib.util.find_spec(mod) is None]
    return miss


def app_command(app, name=None, peer=None, node=None, rate=None):
    if app == "chat":
        cmd = [sys.executable, CHAT_APP]
        if name:
            cmd += ["--name", name]
        return cmd
    cmd = [sys.executable, GAME_APP]
    if app == "bot":
        return cmd + ["--bot"]
    if peer is None:
        raise ValueError("the game needs the opponent's node ID (--peer)")
    cmd += ["--peer", str(int(peer))]
    if node is not None:
        cmd += ["--node", str(int(node))]
    if rate:
        cmd += ["--rate", f"{float(rate):g}"]
    return cmd


def preflight(app, node, peer=None):
    """List of problems that stop the app from starting (empty = OK) and warnings."""
    errors, warnings = [], []
    miss = missing_modules(app)
    if miss:
        errors.append(f"missing Python packages: sudo dnf install {' '.join(miss)}")
    if app in ("chat", "game") and rx_port_busy():
        errors.append(f"another program is receiving on udp {_env('CDP_UDP_BIND', '127.0.0.1')}:"
                      f"{_env('CDP_UDP_OUT_PORT', '5201')} (chat, game or cdp_sock.py udp-listen): close it first")
    if app == "game":
        if node is None:
            errors.append("this node's ID is unknown: export CDP_MY_ID=<id> (same value as the flowgraph)")
        if peer is None:
            errors.append("enter the opponent's node ID")
        elif node is not None and int(peer) == int(node):
            errors.append("the opponent's node ID is this node's own ID")
    if app in ("chat", "game") and flowgraph_running() is False:
        warnings.append("the flowgraph is not running on this PC (start v6.6_csma_transport.grc); "
                        "the app will wait for it")
    return errors, warnings


def status_line(node):
    fg = flowgraph_running()
    fg_s = {True: "flowgraph running", False: "flowgraph NOT running", None: f"flowgraph at {_env('CDP_HOST', '')}"}[fg]
    return f"node {node if node is not None else '?'} · {fg_s}"


# ---------------------------------------------------------------- terminal menu
def launch_text(args):
    st = load_settings()
    node = args.node if args.node is not None else env_node()
    while True:
        peers = known_peers(node)
        print(f"\n=== CDP Link launcher · {status_line(node)} ===")
        print("  1  Chat")
        print("  2  Radio Arena - play another node over the radio")
        print("  3  Radio Arena - practice offline against a bot")
        print("  q  quit")
        try:
            choice = input("choose: ").strip().lower()
        except EOFError:
            return 0
        if choice in ("q", "quit", "exit"):
            return 0
        app = {"1": "chat", "2": "game", "3": "bot"}.get(choice)
        if app is None:
            continue
        name = peer = rate = None
        try:
            if app == "chat":
                name = input(f"your name [{st.get('name') or 'node ' + str(node)}]: ").strip() or st.get("name")
            elif app == "game":
                if node is None:
                    v = input("this node's ID (CDP_MY_ID of the flowgraph): ").strip()
                    node = int(v) if v.isdigit() else None
                hint = ", ".join(f"{n} ({nm})" for n, nm in sorted(peers.items()))
                dflt = st.get("peer")
                v = input(f"opponent node ID{' - known: ' + hint if hint else ''}"
                          f"{f' [{dflt}]' if dflt is not None else ''}: ").strip()
                peer = int(v) if v.isdigit() else dflt
                v = input(f"packets per second [{float(st.get('rate', 5)):g}]: ").strip()
                rate = float(v) if v.replace(".", "", 1).isdigit() else st.get("rate", 5)
        except EOFError:
            return 0
        errors, warnings = preflight(app, node, peer)
        for w in warnings:
            print("note:", w)
        if errors:
            for e in errors:
                print("cannot start:", e)
            continue
        st.update({k: v for k, v in (("name", name), ("peer", peer), ("rate", rate)) if v is not None})
        save_settings(st)
        cmd = app_command(app, name=name, peer=peer, node=node, rate=rate)
        print(f"starting {APP_TITLE[app]} ... (close its window to come back here)", flush=True)
        try:
            code = subprocess.call(cmd)
        except KeyboardInterrupt:
            code = 0
        if code:
            print(f"{APP_TITLE[app]} exited with code {code}")


def launch_direct(args):
    node = args.node if args.node is not None else env_node()
    app = "bot" if args.app == "game" and args.bot else args.app
    errors, warnings = preflight(app, node, args.peer)
    for w in warnings:
        print("note:", w)
    if errors:
        for e in errors:
            print("cannot start:", e)
        return 1
    cmd = app_command(app, name=args.name, peer=args.peer, node=node, rate=args.rate)
    try:
        return subprocess.call(cmd)
    except KeyboardInterrupt:
        return 0


# ---------------------------------------------------------------- window
def launch_gui(args):
    try:
        from PyQt5.QtCore import Qt, QTimer
        from PyQt5.QtGui import QFont
        from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                                     QLineEdit, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QFrame, QMessageBox)
    except ImportError:
        print("PyQt5 not found - using the terminal menu (sudo dnf install python3-qt5 for the window)")
        return launch_text(args)

    import threading

    class Launcher(QWidget):
        def __init__(self):
            super().__init__()
            self.st = load_settings()
            self.proc = None
            self.proc_app = None
            self.stopping = False
            self.lines = []
            self.lock = threading.Lock()
            self.setWindowTitle("CDP Link launcher")
            self.resize(560, 560)
            self.setStyleSheet(
                "QWidget { font-size: 13px; }"
                "#title { font-size: 20px; font-weight: bold; }"
                "#card { border: 1px solid #CCC; border-radius: 8px; }"
                "#cardTitle { font-size: 16px; font-weight: bold; }"
                "#go { background: #007A5A; color: white; border-radius: 4px; padding: 6px 16px; font-weight: bold; }"
                "#go:disabled { background: #DDD; color: #999; }")
            lay = QVBoxLayout(self)
            lay.addWidget(QLabel("CDP Link", objectName="title"))
            self.status = QLabel("")
            lay.addWidget(self.status)

            row = QHBoxLayout()
            row.addWidget(QLabel("This node (CDP_MY_ID):"))
            self.node = QSpinBox()
            self.node.setRange(-1, 254)
            self.node.setSpecialValueText("?")
            n = args.node if args.node is not None else env_node()
            self.node.setValue(-1 if n is None else n)
            self.node.valueChanged.connect(self.refresh)
            row.addWidget(self.node)
            row.addStretch(1)
            lay.addLayout(row)

            # chat card
            card, cl = self._card("💬  Chat",
                                  "Messages to everyone or to one node, files up to 4 MB (ARQ + UDP blocks).")
            r = QHBoxLayout()
            r.addWidget(QLabel("Your name:"))
            self.name = QLineEdit(self.st.get("name") or _env("CDP_CHAT_NAME", ""))
            self.name.setPlaceholderText("node <ID>")
            r.addWidget(self.name, 1)
            self.chat_btn = QPushButton("Open chat", objectName="go")
            self.chat_btn.clicked.connect(lambda: self.start("chat"))
            r.addWidget(self.chat_btn)
            cl.addLayout(r)
            lay.addWidget(card)

            # game card
            card, gl = self._card("🎮  Radio Arena",
                                  "2-player 3D shooter. 31-byte state packets over the UDP block, one radio "
                                  "frame each, no ARQ. Both players pick the game and enter each other's node ID.")
            r = QHBoxLayout()
            r.addWidget(QLabel("Opponent node:"))
            self.peer = QSpinBox()
            self.peer.setRange(0, 254)
            self.peer.setValue(int(self.st.get("peer", 1)))
            self.peer.valueChanged.connect(self.refresh)
            r.addWidget(self.peer)
            r.addSpacing(12)
            r.addWidget(QLabel("Packets/s:"))
            self.rate = QDoubleSpinBox()
            self.rate.setRange(1.0, 20.0)
            self.rate.setSingleStep(1.0)
            self.rate.setDecimals(0)
            self.rate.setValue(float(self.st.get("rate", 5)))
            r.addWidget(self.rate)
            r.addStretch(1)
            gl.addLayout(r)
            self.known = QLabel("")
            self.known.setWordWrap(True)
            self.known.setStyleSheet("color:#666;")
            gl.addWidget(self.known)
            r = QHBoxLayout()
            self.bot_btn = QPushButton("Practice vs bot (offline)")
            self.bot_btn.clicked.connect(lambda: self.start("bot"))
            r.addWidget(self.bot_btn)
            r.addStretch(1)
            self.game_btn = QPushButton("Play over the radio", objectName="go")
            self.game_btn.clicked.connect(lambda: self.start("game"))
            r.addWidget(self.game_btn)
            gl.addLayout(r)
            lay.addWidget(card)

            # running app + output
            r = QHBoxLayout()
            self.running = QLabel("Nothing running")
            r.addWidget(self.running, 1)
            self.stop_btn = QPushButton("Stop")
            self.stop_btn.clicked.connect(self.stop)
            r.addWidget(self.stop_btn)
            lay.addLayout(r)
            self.out = QPlainTextEdit()
            self.out.setReadOnly(True)
            self.out.setMaximumBlockCount(2000)
            self.out.setFont(QFont("Monospace", 9))
            lay.addWidget(self.out, 1)

            self.timer = QTimer(self)
            self.timer.timeout.connect(self.tick)
            self.timer.start(300)
            self.slow = 0
            self.refresh()

        def _card(self, title, text):
            card = QFrame(objectName="card")
            cl = QVBoxLayout(card)
            cl.addWidget(QLabel(title, objectName="cardTitle"))
            t = QLabel(text)
            t.setWordWrap(True)
            t.setStyleSheet("color:#555;")
            cl.addWidget(t)
            return card, cl

        def node_id(self):
            v = self.node.value()
            return None if v < 0 else v

        def refresh(self, *_):
            node = self.node_id()
            fg = flowgraph_running()
            fg_s = {True: "<span style='color:#2BAC76'>● flowgraph running</span>",
                    False: "<span style='color:#E01E5A'>● flowgraph not running</span> - start "
                           "v6.6_csma_transport.grc (apps wait for it)",
                    None: f"flowgraph at {_env('CDP_HOST', '')}"}[fg]
            self.status.setText(fg_s)
            peers = known_peers(node)
            self.known.setText("Known nodes: " + ", ".join(f"{n} ({nm})" for n, nm in sorted(peers.items()))
                               if peers else "Known nodes: none yet (the chat learns them)")
            miss_g, miss_c = missing_modules("game"), missing_modules("chat")
            self.game_btn.setEnabled(not miss_g and node is not None and self.peer.value() != node)
            self.bot_btn.setEnabled(not miss_g)
            self.chat_btn.setEnabled(not miss_c)
            tip = f"sudo dnf install {' '.join(miss_g)}" if miss_g else (
                "set this node's ID first" if node is None else
                "that is this node's own ID" if self.peer.value() == node else "")
            self.game_btn.setToolTip(tip)

        def tick(self):
            with self.lock:
                lines, self.lines = self.lines, []
            for line in lines:
                self.out.appendPlainText(line)
            if self.proc is not None and self.proc.poll() is not None:
                code = self.proc.returncode
                how = "stopped" if self.stopping else "closed" if code == 0 else f"exited with code {code}"
                self.out.appendPlainText(f"--- {APP_TITLE[self.proc_app]} {how} ---")
                self.stopping = False
                self.proc, self.proc_app = None, None
            self.running.setText(f"Running: <b>{APP_TITLE[self.proc_app]}</b>" if self.proc else "Nothing running")
            self.stop_btn.setEnabled(self.proc is not None)
            self.slow += 1
            if self.slow >= 7:                     # ~2 s: re-check flowgraph / ports
                self.slow = 0
                self.refresh()

        def _reader(self, proc):
            for raw in iter(proc.stdout.readline, b""):
                with self.lock:
                    self.lines.append(raw.decode("utf-8", "replace").rstrip())

        def stop(self):
            if self.proc is None:
                return
            self.stopping = True
            self.proc.terminate()
            try:
                self.proc.wait(3.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(2.0)

        def start(self, app):
            if self.proc is not None:
                q = QMessageBox.question(self, "Switch app",
                                         f"{APP_TITLE[self.proc_app]} is running. Close it and start "
                                         f"{APP_TITLE[app]}?")
                if q != QMessageBox.Yes:
                    return
                self.stop()
                self.tick()
            node, peer = self.node_id(), self.peer.value()
            errors, warnings = preflight(app, node, peer if app == "game" else None)
            if errors:
                QMessageBox.warning(self, "Cannot start", "\n\n".join(errors))
                return
            name = self.name.text().strip() or None
            self.st.update({"name": name or "", "peer": peer, "rate": self.rate.value()})
            save_settings(self.st)
            cmd = app_command(app, name=name, peer=peer, node=node, rate=self.rate.value())
            for w in warnings:
                self.out.appendPlainText("note: " + w)
            self.out.appendPlainText("$ " + " ".join(cmd))
            env = dict(os.environ)
            if node is not None:
                env["CDP_MY_ID"] = str(node)
            env["PYTHONUNBUFFERED"] = "1"
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=HERE)
            self.proc_app = app
            threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
            self.tick()

        def closeEvent(self, e):
            if self.proc is not None:
                q = QMessageBox.question(self, "Quit launcher",
                                         f"{APP_TITLE[self.proc_app]} is still running. Close it too?")
                if q == QMessageBox.Yes:
                    self.stop()
            super().closeEvent(e)

    app = QApplication(sys.argv)
    w = Launcher()
    w.show()
    return app.exec_()


def launch(args):
    if args.app:
        return launch_direct(args)
    if args.text or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return launch_text(args)
    return launch_gui(args)


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
    s = sub.add_parser("launch", help="choose and start an app: the chat or the Radio Arena game")
    s.add_argument("app", nargs="?", choices=("chat", "game"), help="start this app directly (no menu)")
    s.add_argument("--text", action="store_true", help="terminal menu instead of the window")
    s.add_argument("--name", default=None, help="chat: your display name")
    s.add_argument("--peer", type=int, default=None, help="game: the opponent's node ID")
    s.add_argument("--rate", type=float, default=None, help="game: packets per second (default 5)")
    s.add_argument("--bot", action="store_true", help="game: offline against a bot")
    s.add_argument("--node", type=int, default=None, help="this node's ID (default: $CDP_MY_ID)")
    a = ap.parse_args()

    if a.cmd == "launch":
        sys.exit(launch(a))

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
