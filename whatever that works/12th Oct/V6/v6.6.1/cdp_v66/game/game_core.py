"""
game_core.py - everything except drawing: map, movement, shooting, hit events,
network links (UDP to GNU Radio, or an offline bot) and link statistics.
No pygame/OpenGL here, so it runs and tests headless.
"""
from __future__ import annotations

import math
import os
import random
import socket
import time
from collections import deque
from typing import List, Optional, Tuple

from payload import (FLAG_DEAD, FLAG_FIRED, FLAG_RELOADING, MAX_EVENTS, PAYLOAD_LEN, Event,
                     PlayerState, build_payload, parse_payload)

# ------------------------------------------------------------------ tuning
CELL = 2.0            # metres per map cell
WALL_H = 3.0
EYE_H = 1.6
PLAYER_R = 0.35
BODY_TOP = 1.45       # body cylinder height (hitbox)
HEAD_Y = 1.65         # head sphere centre
HEAD_R = 0.25
BODY_R = 0.40
WALK = 5.0            # m/s
SPRINT = 1.6          # multiplier with Shift
GRAVITY = 18.0
JUMP_V = 6.0
MAX_HP = 100
MAG = 30
FIRE_INTERVAL = 0.11  # s between shots (hold LMB = automatic)
RELOAD_TIME = 1.6
DMG_BODY = 20
DMG_HEAD = 50
RESPAWN_TIME = 3.0
EVENT_REPEAT = 6      # each hit report rides in this many consecutive packets
PEER_TIMEOUT = 2.0    # s without packets -> "link lost"

MAP = [
    "################",
    "#..............#",
    "#..##....##....#",
    "#..#......#..#.#",
    "#......##....#.#",
    "#.##...##..#...#",
    "#.#........#.###",
    "#....###.......#",
    "#.......###....#",
    "###.#........#.#",
    "#...#..##...##.#",
    "#.#....##......#",
    "#.#..#......#..#",
    "#....##....##..#",
    "#..............#",
    "################",
]
ROWS, COLS = len(MAP), len(MAP[0])
# spawn cells (row, col) for player 0 and 1 - opposite corners
SPAWN_CELLS = {0: (1, 1), 1: (14, 14)}


def solid_cell(r: int, c: int) -> bool:
    return not (0 <= r < ROWS and 0 <= c < COLS) or MAP[r][c] == "#"


def solid_at(x: float, z: float) -> bool:
    return solid_cell(int(math.floor(z / CELL)), int(math.floor(x / CELL)))


def cell_center(r: int, c: int) -> Tuple[float, float]:
    return (c + 0.5) * CELL, (r + 0.5) * CELL


def open_cells() -> List[Tuple[int, int]]:
    return [(r, c) for r in range(ROWS) for c in range(COLS) if MAP[r][c] != "#"]


def collides(x: float, z: float, r: float = PLAYER_R) -> bool:
    c0, c1 = int(math.floor((x - r) / CELL)), int(math.floor((x + r) / CELL))
    r0, r1 = int(math.floor((z - r) / CELL)), int(math.floor((z + r) / CELL))
    return any(solid_cell(rr, cc) for rr in range(r0, r1 + 1) for cc in range(c0, c1 + 1))


def look_dir(yaw: float, pitch: float) -> Tuple[float, float, float]:
    """yaw 0 looks toward -z, + yaw turns right; + pitch looks up."""
    cp = math.cos(pitch)
    return math.sin(yaw) * cp, math.sin(pitch), -math.cos(yaw) * cp


def wrap_angle(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


# ------------------------------------------------------------------ ray casting
def raycast(ox, oy, oz, d, max_dist=80.0, target=None, step=0.04):
    """March a ray. target = (x, y_feet, z) of a player hitbox or None.
    Returns (kind, dist, damage) with kind in 'head'|'body'|'wall'|'floor'|'none'."""
    dx, dy, dz = d
    for i in range(1, int(max_dist / step)):
        t = i * step
        px, py, pz = ox + dx * t, oy + dy * t, oz + dz * t
        if target is not None:
            tx, ty, tz = target
            hx, hy, hz = px - tx, py - (ty + HEAD_Y), pz - tz
            if hx * hx + hy * hy + hz * hz < HEAD_R * HEAD_R:
                return "head", t, DMG_HEAD
            if ty <= py <= ty + BODY_TOP and (px - tx) ** 2 + (pz - tz) ** 2 < BODY_R * BODY_R:
                return "body", t, DMG_BODY
        if py < 0:
            return "floor", t, 0
        if py < WALL_H and solid_at(px, pz):
            return "wall", t, 0
    return "none", max_dist, 0


def line_of_sight(a, b) -> bool:
    ax, ay, az = a
    bx, by, bz = b
    dist = math.dist(a, b)
    if dist < 1e-6:
        return True
    d = ((bx - ax) / dist, (by - ay) / dist, (bz - az) / dist)
    kind, t, _ = raycast(ax, ay, az, d, max_dist=dist, step=0.1)
    return kind == "none" or t >= dist - 0.1


# ------------------------------------------------------------------ a fighter (local player or bot)
class Fighter:
    def __init__(self, pid: int):
        self.pid = pid
        self.deaths = 0
        self.seq = 0
        self.eid = 0
        self.pending: List[List[int]] = []          # [eid, target, dmg, sends_left]
        self.seen = {}                               # sender pid -> deque of eids
        self.fired_since_send = False
        self.respawn()

    # --- life cycle
    def respawn(self):
        r, c = SPAWN_CELLS.get(self.pid, SPAWN_CELLS[0])
        self.x, self.z = cell_center(r, c)
        self.y = 0.0
        self.vy = 0.0
        cx, cz = COLS * CELL / 2, ROWS * CELL / 2           # face the middle
        self.yaw = math.atan2(cx - self.x, -(cz - self.z))
        self.pitch = 0.0
        self.hp = MAX_HP
        self.ammo = MAG
        self.reload_t = 0.0
        self.fire_cd = 0.0
        self.dead_t = 0.0

    @property
    def alive(self) -> bool:
        return self.hp > 0

    @property
    def reloading(self) -> bool:
        return self.reload_t > 0

    def eye(self):
        return self.x, self.y + EYE_H, self.z

    # --- per-frame
    def update(self, dt, fwd=0.0, strafe=0.0, sprint=False, jump=False):
        """fwd/strafe in -1..1 relative to view yaw."""
        if not self.alive:
            self.dead_t -= dt
            if self.dead_t <= 0:
                self.respawn()
            return
        self.fire_cd = max(0.0, self.fire_cd - dt)
        if self.reload_t > 0:
            self.reload_t -= dt
            if self.reload_t <= 0:
                self.reload_t = 0.0
                self.ammo = MAG

        mag = math.hypot(fwd, strafe)
        if mag > 1:
            fwd, strafe = fwd / mag, strafe / mag
        speed = WALK * (SPRINT if sprint and fwd > 0 else 1.0)
        fx, fz = math.sin(self.yaw), -math.cos(self.yaw)
        rx, rz = math.cos(self.yaw), math.sin(self.yaw)
        mx = (fx * fwd + rx * strafe) * speed * dt
        mz = (fz * fwd + rz * strafe) * speed * dt
        if not collides(self.x + mx, self.z):
            self.x += mx
        if not collides(self.x, self.z + mz):
            self.z += mz

        if jump and self.y <= 0.0:
            self.vy = JUMP_V
        self.vy -= GRAVITY * dt
        self.y += self.vy * dt
        if self.y <= 0:
            self.y, self.vy = 0.0, 0.0

    def look(self, dyaw, dpitch):
        self.yaw = (self.yaw + dyaw) % (2 * math.pi)
        self.pitch = max(-1.55, min(1.55, self.pitch + dpitch))

    # --- weapon
    def start_reload(self):
        if self.alive and not self.reloading and self.ammo < MAG:
            self.reload_t = RELOAD_TIME

    def try_fire(self) -> bool:
        if not self.alive or self.reloading or self.fire_cd > 0:
            return False
        if self.ammo <= 0:
            self.start_reload()
            return False
        self.ammo -= 1
        self.fire_cd = FIRE_INTERVAL
        self.fired_since_send = True
        if self.ammo == 0:
            self.start_reload()
        return True

    def shoot(self, target_pos=None):
        """Fire one round along the view. target_pos = opponent (x, y_feet, z) as you see it.
        Returns (fired, kind, dist)."""
        if not self.try_fire():
            return False, "none", 0.0
        ex, ey, ez = self.eye()
        kind, dist, dmg = raycast(ex, ey, ez, look_dir(self.yaw, self.pitch), target=target_pos)
        return True, kind, dist

    def report_hit(self, target_pid: int, dmg: int):
        self.eid = (self.eid + 1) & 0xFFFF
        self.pending.append([self.eid, target_pid, dmg, EVENT_REPEAT])

    # --- damage
    def take_damage(self, dmg: int) -> bool:
        """Returns True if this hit killed us."""
        if not self.alive:
            return False
        self.hp = max(0, self.hp - dmg)
        if self.hp == 0:
            self.deaths = (self.deaths + 1) & 0xFF
            self.dead_t = RESPAWN_TIME
            self.reload_t = 0.0
            return True
        return False

    def apply_events(self, st: PlayerState) -> Tuple[int, bool]:
        """Apply hit reports from an opponent packet. Returns (damage_taken, died)."""
        seen = self.seen.setdefault(st.pid, deque(maxlen=256))
        total, died = 0, False
        for e in st.events:
            if e.eid in seen:
                continue
            seen.append(e.eid)
            if e.target == self.pid and self.alive:
                total += e.damage
                died = self.take_damage(e.damage) or died
        return total, died

    # --- network
    def make_packet(self) -> bytes:
        flags = 0
        if self.fired_since_send:
            flags |= FLAG_FIRED
        if self.reloading:
            flags |= FLAG_RELOADING
        if not self.alive:
            flags |= FLAG_DEAD
        self.fired_since_send = False
        events = [Event(e[0], e[1], e[2]) for e in self.pending[:MAX_EVENTS]]
        for e in self.pending[:MAX_EVENTS]:
            e[3] -= 1
        # rotate so a burst of >3 hits all get airtime, drop the finished ones
        self.pending = [e for e in self.pending[MAX_EVENTS:] + self.pending[:MAX_EVENTS] if e[3] > 0]
        st = PlayerState(pid=self.pid, seq=self.seq, x=self.x, y=self.y, z=self.z,
                         yaw=self.yaw, pitch=self.pitch, hp=self.hp, ammo=self.ammo,
                         flags=flags, deaths=self.deaths, events=events)
        self.seq = (self.seq + 1) & 0xFFFF
        return build_payload(st)


# ------------------------------------------------------------------ what we know about the other player
class RemoteView:
    def __init__(self):
        self.state: Optional[PlayerState] = None
        self.x = self.y = self.z = 0.0
        self.yaw = self.pitch = 0.0
        self.last_rx = 0.0
        self.last_seq: Optional[int] = None
        self.rx = 0
        self.lost = 0
        self.bad = 0
        self.tracer_t = 0.0
        self._rx_times = deque(maxlen=50)
        self.vx = self.vy = self.vz = 0.0         # estimated velocity (m/s) from the last two packets
        self.since_rx = 0.0                       # s since the latest packet (for extrapolation)
        self._prev_t = None

    def connected(self, now) -> bool:
        return self.state is not None and now - self.last_rx < PEER_TIMEOUT

    @property
    def alive(self) -> bool:
        return self.state is not None and not (self.state.flags & FLAG_DEAD)

    def on_packet(self, st: PlayerState, now: float) -> bool:
        """Returns False for stale / out-of-order packets."""
        if self.last_seq is not None:
            gap = (st.seq - self.last_seq) & 0xFFFF
            if gap == 0 or gap > 0x8000:          # duplicate or older
                return False
            if gap < 1000:
                self.lost += gap - 1
        first = self.state is None or (self.state.flags & FLAG_DEAD and not st.flags & FLAG_DEAD)
        if first or self._prev_t is None or now - self._prev_t > 1.0:
            self.vx = self.vy = self.vz = 0.0
        else:
            span = max(now - self._prev_t, 0.02)
            lim = WALK * SPRINT * 1.2
            self.vx = max(-lim, min(lim, (st.x - self.state.x) / span))
            self.vz = max(-lim, min(lim, (st.z - self.state.z) / span))
            self.vy = max(-JUMP_V, min(JUMP_V, (st.y - self.state.y) / span))
        self._prev_t = now
        self.since_rx = 0.0
        self.last_seq = st.seq
        self.state = st
        self.last_rx = now
        self.rx += 1
        self._rx_times.append(now)
        if first:                                # snap on first packet / respawn
            self.x, self.y, self.z, self.yaw, self.pitch = st.x, st.y, st.z, st.yaw, st.pitch
        if st.flags & FLAG_FIRED:
            self.tracer_t = 0.08
        return True

    def update(self, dt):
        """Smooth toward the latest received pose, moved on by the estimated velocity for up to
        0.3 s, so the opponent keeps moving between packets on a slow link (e.g. 5 packets/s)."""
        if self.state is None:
            return
        self.since_rx += dt
        k = min(1.0, dt * 12.0)
        s = self.state
        ahead = 0.0 if s.flags & FLAG_DEAD else min(self.since_rx, 0.3)
        tx, tz = s.x + self.vx * ahead, s.z + self.vz * ahead
        if collides(tx, tz):                      # never predict into a wall
            tx, tz = s.x, s.z
        ty = max(0.0, s.y + self.vy * ahead)
        self.x += (tx - self.x) * k
        self.y += (ty - self.y) * k
        self.z += (tz - self.z) * k
        self.yaw += wrap_angle(s.yaw - self.yaw) * k
        self.pitch += (s.pitch - self.pitch) * k
        self.tracer_t = max(0.0, self.tracer_t - dt)

    def pkt_rate(self, now) -> float:
        t = [x for x in self._rx_times if now - x < 2.0]
        return len(t) / 2.0

    def loss_pct(self) -> float:
        total = self.rx + self.lost
        return 100.0 * self.lost / total if total else 0.0


# ------------------------------------------------------------------ links
class UdpLink:
    """Game <-> GNU Radio over UDP.
    TX: datagrams to tx_host:tx_port  (GNU Radio 'Socket PDU' UDP_SERVER on that port -> your TX chain)
    RX: listens on rx_port            (your RX chain -> 'Socket PDU' UDP_CLIENT 127.0.0.1:rx_port)"""

    def __init__(self, tx_host="127.0.0.1", tx_port=52001, rx_port=52002):
        self.tx = (tx_host, tx_port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", rx_port))
        self.sock.setblocking(False)
        self.desc = f"UDP tx {tx_host}:{tx_port}  rx :{rx_port}"

    def send(self, data: bytes):
        try:
            self.sock.sendto(data, self.tx)
        except OSError:
            pass            # flowgraph not running yet - just keep playing

    def poll(self, dt=0.0) -> List[bytes]:
        out = []
        while True:
            try:
                data, _ = self.sock.recvfrom(4096)
                out.append(data)
            except (BlockingIOError, InterruptedError):
                return out
            except OSError:          # e.g. Windows ICMP port-unreachable reset
                return out


class CdpLink:
    """Game <-> the CDP v6.6 flowgraph's UDP Transport block (one datagram = one radio frame, no ARQ).

    TX: to CDP_HOST:CDP_UDP_PORT (5200), datagram = dest node 1B | 31-byte game payload (unicast to the peer)
    RX: on CDP_UDP_BIND:CDP_UDP_OUT_PORT (5201), datagram = src 1B | dest 1B | data
        Only datagrams from the peer node whose data is exactly one game payload are passed on; chat
        beacons/broadcasts and anything else on the port are ignored (counted in .ignored).
    Only one program per PC can listen on 5201, so close the chat app before starting the game."""

    def __init__(self, my_node, peer_node, host=None, port=None, out_port=None, bind=None):
        env = os.environ.get
        self.my_node, self.peer = int(my_node), int(peer_node)
        if not (0 <= self.peer <= 254) or self.peer == self.my_node:
            raise ValueError(f"opponent node must be 0..254 and not this node ({self.my_node})")
        self.tx = (host or env("CDP_HOST", "127.0.0.1"), int(port or env("CDP_UDP_PORT", "5200")))
        self.rx_addr = (bind or env("CDP_UDP_BIND", "127.0.0.1"), int(out_port or env("CDP_UDP_OUT_PORT", "5201")))
        self.out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.bind(self.rx_addr)               # exclusive on purpose (no SO_REUSEADDR)
        except OSError as e:
            self.sock.close()
            raise OSError(f"cannot listen on udp {self.rx_addr[0]}:{self.rx_addr[1]} ({e}). "
                          f"Is the chat app (or another game) still running? Close it first.") from e
        self.sock.setblocking(False)
        self.ignored = 0
        self.send_errors = 0
        self.desc = f"CDP radio: node {self.my_node} <-> node {self.peer}"

    @staticmethod
    def game_pid(my_node, peer_node):
        """Player 0/1 from the two node IDs: the lower node ID plays P0 (blue), the higher P1 (red)."""
        return 0 if int(my_node) < int(peer_node) else 1

    def send(self, data: bytes):
        try:
            self.out.sendto(bytes([self.peer]) + data, self.tx)
        except OSError:
            self.send_errors += 1                      # flowgraph not running yet - keep playing

    def poll(self, dt=0.0) -> List[bytes]:
        out = []
        while True:
            try:
                d, _ = self.sock.recvfrom(65535)
            except (BlockingIOError, InterruptedError):
                return out
            except OSError:
                return out
            if len(d) == 2 + PAYLOAD_LEN and d[0] == self.peer and d[1] != 255:
                out.append(d[2:])
            else:
                self.ignored += 1


class BotLink:
    """Offline opponent. It only talks to you through build_payload/parse_payload,
    exactly like a radio peer, so it also exercises the payload code.
    loss = simulated packet loss probability, rate = bot packets per second."""

    def __init__(self, bot_pid: int, loss=0.0, rate=10.0, seed=None):
        self.bot = Fighter(bot_pid)
        self.loss = loss
        self.rate = rate
        self.rng = random.Random(seed)
        self.desc = f"offline bot (loss {loss:.0%})"
        self.opp: Optional[PlayerState] = None
        self.send_t = 0.0
        self.think_t = 0.0
        self.waypoint = cell_center(*self.rng.choice(open_cells()))
        self.stuck_t = 0.0
        self.last_dist = 1e9
        self.strafe_dir = 1.0
        self.out: List[bytes] = []

    def send(self, data: bytes):                # packet from the human player
        if self.rng.random() < self.loss:
            return
        if len(data) < PAYLOAD_LEN:
            return
        st = parse_payload(data)
        if st.pid == self.bot.pid:
            return
        self.opp = st
        self.bot.apply_events(st)

    def _ai(self, dt):
        b = self.bot
        fwd = strafe = 0.0
        target = None
        if self.opp is not None and not (self.opp.flags & FLAG_DEAD):
            target = (self.opp.x, self.opp.y, self.opp.z)
        sees = False
        if b.alive and target is not None:
            chest = (target[0], target[1] + 1.2, target[2])
            sees = math.dist(b.eye(), chest) < 30 and line_of_sight(b.eye(), chest)
        if not b.alive:
            return
        if sees:
            tx, ty, tz = target
            want_yaw = math.atan2(tx - b.x, -(tz - b.z))
            horiz = math.hypot(tx - b.x, tz - b.z)
            want_pitch = math.atan2(ty + 1.2 - (b.y + EYE_H), horiz)
            b.yaw += wrap_angle(want_yaw - b.yaw) * min(1, dt * 6)
            b.pitch += (want_pitch - b.pitch) * min(1, dt * 6)
            self.think_t -= dt
            if self.think_t <= 0:
                self.think_t = self.rng.uniform(0.6, 1.4)
                self.strafe_dir = self.rng.choice((-1.0, 1.0))
            strafe = self.strafe_dir
            fwd = 0.5 if horiz > 8 else (-0.3 if horiz < 3 else 0.0)
            aimed = abs(wrap_angle(want_yaw - b.yaw)) < 0.12
            if aimed and b.fire_cd <= 0 and b.ammo > 0 and not b.reloading:
                if self.rng.random() < dt * 5:                  # bursty trigger finger
                    if b.try_fire():
                        hit_p = max(0.12, 0.55 - horiz * 0.02)
                        if self.rng.random() < hit_p:
                            dmg = DMG_HEAD if self.rng.random() < 0.15 else DMG_BODY
                            b.report_hit(self.opp.pid, dmg)
        else:
            wx, wz = self.waypoint
            d = math.hypot(wx - b.x, wz - b.z)
            want_yaw = math.atan2(wx - b.x, -(wz - b.z))
            b.yaw += wrap_angle(want_yaw - b.yaw) * min(1, dt * 5)
            b.pitch *= 0.9
            fwd = 1.0 if abs(wrap_angle(want_yaw - b.yaw)) < 0.8 else 0.2
            self.stuck_t = self.stuck_t + dt if d > self.last_dist - 0.01 else 0.0
            self.last_dist = d
            if d < 0.8 or self.stuck_t > 1.0:
                self.waypoint = cell_center(*self.rng.choice(open_cells()))
                self.stuck_t, self.last_dist = 0.0, 1e9
            if b.ammo < MAG // 2:
                b.start_reload()
        b.update(dt, fwd=fwd, strafe=strafe)

    def poll(self, dt=0.0) -> List[bytes]:
        if dt > 0:
            self._ai(dt)
            if not self.bot.alive:
                self.bot.update(dt)
            self.send_t -= dt
            if self.send_t <= 0:
                self.send_t += 1.0 / self.rate
                pkt = self.bot.make_packet()
                if self.rng.random() >= self.loss:
                    self.out.append(pkt)
        out, self.out = self.out, []
        return out


# ------------------------------------------------------------------ the match (no drawing)
class Match:
    def __init__(self, my_pid: int, link, send_rate=10.0):
        self.me = Fighter(my_pid)
        self.remote = RemoteView()
        self.link = link
        self.send_rate = send_rate
        self.send_t = 0.0
        self.now = 0.0
        self.hitmarker_t = 0.0
        self.damage_t = 0.0
        self.tracers: List[Tuple[tuple, tuple, float]] = []   # (start, end, ttl)
        self.shots: deque = deque(maxlen=64)                    # (end, dir, kind) for impact effects
        self.messages: deque = deque(maxlen=4)                  # (text, ttl)
        self.kill_feed_last_deaths = 0

    def remote_target(self):
        if self.remote.connected(self.now) and self.remote.alive:
            return self.remote.x, self.remote.y, self.remote.z
        return None

    def msg(self, text, ttl=3.0):
        self.messages.append([text, ttl])

    def fire(self):
        fired, kind, dist = self.me.shoot(self.remote_target())
        if not fired:
            return
        ex, ey, ez = self.me.eye()
        d = look_dir(self.me.yaw, self.me.pitch)
        end = (ex + d[0] * dist, ey + d[1] * dist, ez + d[2] * dist)
        self.tracers.append([(ex, ey - 0.15, ez), end, 0.06])
        self.shots.append((end, d, kind))
        if kind in ("head", "body") and self.remote.state is not None:
            dmg = DMG_HEAD if kind == "head" else DMG_BODY
            self.me.report_hit(self.remote.state.pid, dmg)
            self.hitmarker_t = 0.15 if kind == "body" else 0.3

    def step(self, dt, fwd=0.0, strafe=0.0, sprint=False, jump=False, firing=False):
        self.now += dt
        was_alive = self.me.alive
        self.me.update(dt, fwd, strafe, sprint, jump)
        if was_alive is False and self.me.alive:
            self.msg("Respawned")
        if firing:
            self.fire()

        # receive
        for data in self.link.poll(dt):
            if len(data) < PAYLOAD_LEN:          # too short to unpack
                self.remote.bad += 1
                continue
            st = parse_payload(data)
            if st.pid == self.me.pid:          # our own echo (loopback flowgraph) - ignore
                continue
            if not self.remote.on_packet(st, self.now):
                continue
            prev_deaths = self.kill_feed_last_deaths
            dmg, died = self.me.apply_events(st)
            if dmg:
                self.damage_t = 0.4
            if died:
                self.msg(f"Eliminated by P{st.pid}  -  respawn in {RESPAWN_TIME:.0f}s", 4)
            if st.deaths != prev_deaths:
                if (st.deaths - prev_deaths) & 0xFF < 100 and self.remote.rx > 1:
                    self.msg(f"You eliminated P{st.pid}!")
                self.kill_feed_last_deaths = st.deaths
        self.remote.update(dt)

        # send
        self.send_t -= dt
        if self.send_t <= 0:
            self.send_t = max(self.send_t + 1.0 / self.send_rate, 0.0)
            self.link.send(self.me.make_packet())

        # timers
        self.hitmarker_t = max(0.0, self.hitmarker_t - dt)
        self.damage_t = max(0.0, self.damage_t - dt)
        for t in self.tracers:
            t[2] -= dt
        self.tracers = [t for t in self.tracers if t[2] > 0]
        for m in self.messages:
            m[1] -= dt
        while self.messages and self.messages[0][1] <= 0:
            self.messages.popleft()

    def score(self) -> Tuple[int, int]:
        """(my kills, my deaths). In a 2-player game my kills = opponent's deaths."""
        kills = self.remote.state.deaths if self.remote.state else 0
        return kills, self.me.deaths
