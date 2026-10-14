"""
Embedded Python Block: Game Link - Radio Arena over this flowgraph (no daemon, no transport block)

Wiring (message ports)
  Packet Deframer RX pdu_out -> from_radio        to_framer -> Packet Framer TX msg_in
  CSMA pdu_out               -> tx_done           (so this block knows when its game frame went on air)

What it does
  * launch_game = 1: starts the game window when the flowgraph starts and closes it when it stops. The game is
    game/fps_game.py next to this .grc. With launch_game = 0, start it yourself; the command is printed at start.
  * game -> radio: the game sends its 31-byte state to udp 127.0.0.1:game_in_port. Only the NEWEST state is kept
    here. It goes to the framer when the previous game frame has left the CSMA block (tx_done), and at most
    max_rate times per second. So the CSMA queue never holds more than one game frame and no position goes out
    seconds late; states overtaken by a newer one are dropped here ("replaced").
  * radio -> game: frames from the peer that carry a game packet go to udp 127.0.0.1:game_out_port.
  * Typed messages (QT edit box) keep working; they share the framer and CSMA with the game.

On air (framer payload): C5 10 | 31-byte game payload. C5 10 marks a game frame for the link layer only; the game
payload itself is unchanged. The framer and deframer of this flowgraph print no console line for binary payloads
that start with C5, so the console isn't flooded.

Peer and player
  peer_id = the other node's ID (MY_ID of the other PC). -1 = automatic: env CDP_PEER_ID, else 1 - src_id when
  src_id is 0 or 1. 255 = broadcast to and accept from any node.
  Player: the lower node ID plays P0 (blue), the higher P1 (red).
"""
import os
import socket
import subprocess
import sys
import threading
import time

import pmt
from gnuradio import gr

TAG = bytes([0xC5, 0x10])          # link-layer type: game frame
GAME_LEN = 31                       # payload.py PAYLOAD_LEN
APP = "game"                        # PDU metadata key "app" -> framer copies it -> CSMA pdu_out carries it


class GameLink(gr.basic_block):
    """Game Link: Radio Arena <-> framer/deframer, newest state only, one game frame in the CSMA queue."""

    def __init__(self, src_id=0, peer_id=-1, launch_game=1, game_rate=5.0, max_rate=5.0, inflight_timeout_s=1.5,
                 game_in_port=52001, game_out_port=52002, stats_every_s=10.0, verbose=1):
        gr.basic_block.__init__(self, name="Game Link (Radio Arena)", in_sig=None, out_sig=None)
        self.src_id = int(src_id) & 0xFF
        peer = int(peer_id)
        if peer < 0:
            env = os.environ.get("CDP_PEER_ID", "").strip()
            if env.isdigit():
                peer = int(env)
            elif self.src_id in (0, 1):
                peer = 1 - self.src_id
            else:
                peer = 255
        self.peer = peer & 0xFF
        if self.peer == 255:
            self.pid = self.src_id & 1
        else:
            self.pid = 0 if self.src_id < self.peer else 1
        self.launch_game = int(launch_game)
        self.game_rate = max(0.5, float(game_rate))
        self.max_rate = max(0.2, float(max_rate))
        self.inflight_timeout_s = max(0.2, float(inflight_timeout_s))
        self.game_in_port = int(game_in_port)
        self.game_out_port = int(game_out_port)
        self.stats_every_s = float(stats_every_s)
        self.verbose = int(verbose)

        self._lock = threading.Lock()
        self._latest = None
        self._inflight = False
        self._t_sent = 0.0
        self._run = False
        self._thr = None
        self._rx_sock = None
        self._tx_sock = None
        self._proc = None
        self._proc_reported = False
        self._waits = []
        self._st = dict(states=0, sent=0, on_air=0, replaced=0, timeouts=0, to_game=0, other=0, bad=0)
        self._st_last = dict(self._st)

        self.message_port_register_in(pmt.intern("from_radio"))
        self.message_port_register_in(pmt.intern("tx_done"))
        self.message_port_register_out(pmt.intern("to_framer"))
        self.set_msg_handler(pmt.intern("from_radio"), self._on_radio)
        self.set_msg_handler(pmt.intern("tx_done"), self._on_tx_done)

    # ------------------------------------------------------------------ helpers
    def _game_path(self):
        here = os.path.dirname(os.path.abspath(__file__))
        for base in (here, os.getcwd()):
            p = os.path.join(base, "game", "fps_game.py")
            if os.path.isfile(p):
                return p
        return os.path.join(here, "game", "fps_game.py")

    def _game_cmd(self):
        return [sys.executable, self._game_path(), "--id", str(self.pid), "--tx-port", str(self.game_in_port),
                "--rx-port", str(self.game_out_port), "--rate", f"{self.game_rate:g}"]

    # ------------------------------------------------------------------ start / stop
    def start(self):
        self._tx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._rx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._rx_sock.bind(("127.0.0.1", self.game_in_port))
            self._rx_sock.settimeout(0.005)
        except OSError as e:
            print(f"[GAME] cannot listen on udp 127.0.0.1:{self.game_in_port}: {e} (another flowgraph or program "
                  f"uses it). Game -> radio disabled.", flush=True)
            self._rx_sock.close()
            self._rx_sock = None
        peer = "any node (broadcast)" if self.peer == 255 else f"node {self.peer}"
        print(f"[GAME] node {self.src_id} plays P{self.pid} ({'blue' if self.pid == 0 else 'red'}) against {peer}; "
              f"newest state only, max {self.max_rate:g} frames/s, one game frame in the CSMA queue", flush=True)
        if self.peer == 255:
            print("[GAME] WARNING: peer unknown (255). Both PCs must get different players: set env CDP_PEER_ID "
                  "to the other node's ID, or use node IDs 0 and 1.", flush=True)
        if self.launch_game:
            path = self._game_path()
            if not os.path.isfile(path):
                print(f"[GAME] game not found at {path}: put the 'game' folder next to this .grc. "
                      f"Radio side runs anyway.", flush=True)
            else:
                try:
                    env = dict(os.environ, PYTHONUNBUFFERED="1")
                    self._proc = subprocess.Popen(self._game_cmd(), cwd=os.path.dirname(path), env=env)
                    print(f"[GAME] game window started (pid {self._proc.pid})", flush=True)
                except OSError as e:
                    print(f"[GAME] could not start the game: {e}", flush=True)
        else:
            print("[GAME] start the game yourself:  " + " ".join(self._game_cmd()), flush=True)
        self._run = True
        self._thr = threading.Thread(target=self._loop, name="game-link", daemon=True)
        self._thr.start()
        return True

    def stop(self):
        self._run = False
        if self._thr is not None:
            self._thr.join(timeout=1.0)
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(3.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        for s in (self._rx_sock, self._tx_sock):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        return True

    # ------------------------------------------------------------------ game -> radio
    def _loop(self):
        t_stats = time.monotonic()
        while self._run:
            if self._rx_sock is not None:
                try:
                    data, _ = self._rx_sock.recvfrom(4096)
                except socket.timeout:
                    data = None
                except OSError:
                    data = None
                    time.sleep(0.005)
                if data is not None:
                    if len(data) == GAME_LEN:
                        with self._lock:
                            if self._latest is not None:
                                self._st["replaced"] += 1
                            self._latest = data
                            self._st["states"] += 1
                    else:
                        self._st["bad"] += 1
            else:
                time.sleep(0.05)
            self._maybe_send()
            now = time.monotonic()
            if self._proc is not None and not self._proc_reported and self._proc.poll() is not None:
                self._proc_reported = True
                print(f"[GAME] game window closed (exit code {self._proc.returncode}); the radio side keeps "
                      f"running. Restart the flowgraph to open it again.", flush=True)
            if self.verbose and self.stats_every_s > 0 and now - t_stats >= self.stats_every_s:
                t_stats = now
                self._print_stats()

    def _maybe_send(self):
        now = time.monotonic()
        with self._lock:
            if self._latest is None or now - self._t_sent < 1.0 / self.max_rate:
                return
            if self._inflight:
                if now - self._t_sent < self.inflight_timeout_s:
                    return
                self._st["timeouts"] += 1            # never confirmed (dropped or very slow): send anyway
            data, self._latest = self._latest, None
            self._inflight = True
            self._t_sent = now
            self._st["sent"] += 1
        meta = pmt.make_dict()
        if self.peer != 255:
            meta = pmt.dict_add(meta, pmt.intern("dest_id"), pmt.from_long(self.peer))
        else:
            meta = pmt.dict_add(meta, pmt.intern("dest_id"), pmt.from_long(255))
        meta = pmt.dict_add(meta, pmt.intern("app"), pmt.intern(APP))
        raw = TAG + data
        self.message_port_pub(pmt.intern("to_framer"), pmt.cons(meta, pmt.init_u8vector(len(raw), list(raw))))

    def _on_tx_done(self, msg):
        try:
            meta = pmt.car(msg)
            if not pmt.is_dict(meta):
                return
            v = pmt.dict_ref(meta, pmt.intern("app"), pmt.PMT_NIL)
            if not (pmt.is_symbol(v) and pmt.symbol_to_string(v) == APP):
                return
        except Exception:
            return
        with self._lock:
            if self._inflight:
                self._waits.append(time.monotonic() - self._t_sent)
            self._inflight = False
            self._st["on_air"] += 1
        self._maybe_send()                            # a newer state may already be waiting

    # ------------------------------------------------------------------ radio -> game
    def _on_radio(self, msg):
        try:
            meta, vec = pmt.car(msg), pmt.cdr(msg)
            if not pmt.is_u8vector(vec):
                return
            raw = bytes(pmt.u8vector_elements(vec))
            if len(raw) != len(TAG) + GAME_LEN or raw[:2] != TAG:
                return                                # typed text etc.: not for the game
            src = pmt.to_long(pmt.dict_ref(meta, pmt.intern("src_id"), pmt.from_long(-1))) if pmt.is_dict(meta) else -1
        except Exception:
            return
        if self.peer != 255 and src != self.peer:
            self._st["other"] += 1
            return
        try:
            self._tx_sock.sendto(raw[2:], ("127.0.0.1", self.game_out_port))
            self._st["to_game"] += 1
        except (OSError, AttributeError):
            pass

    # ------------------------------------------------------------------ stats
    def _print_stats(self):
        d = {k: self._st[k] - self._st_last[k] for k in self._st}
        self._st_last = dict(self._st)
        if not any(d.values()):
            return
        with self._lock:
            w = sorted(self._waits)
            self._waits = []
        wait = f"{1e3 * w[len(w) // 2]:.0f} ms median, max {1e3 * w[-1]:.0f} ms" if w else "-"
        print(f"[GAME] since last line: game states in {d['states']} | sent {d['sent']} frames, on air {d['on_air']}, "
              f"replaced by newer {d['replaced']}, timeouts {d['timeouts']} | time to air {wait} | from radio to game "
              f"{d['to_game']} (other nodes {d['other']})", flush=True)
