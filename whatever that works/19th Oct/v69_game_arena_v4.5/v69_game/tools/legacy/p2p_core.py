"""
game_core.py - Radio Arena v2: everything except drawing. Map, movement, shooting, the multi-player
session (lobby + arena), lag protection, links (v6.8 radio over UDP, or an in-process hub with bots).
No pygame/OpenGL here, so it runs headless (tests and the worst-case simulation use it directly).

Lag protection (why a player who was lagged no longer "dies all at once")
--------------------------------------------------------------------------
Every packet carries ts = the sender's clock in ms when it was built. Each receiver tracks, per player,
    lag = (min round trip / 2) + (how much later than usual this packet arrived)
("usual" = the smallest (my clock - ts) seen so far, so the two PCs' clocks never need to agree).

 1. Late packets are dropped:  lag > max_lag  ->  position, HP and hit reports in it are NOT applied.
    The player stays connected (the link is alive) and shows "lagging 850 ms" in the lobby/scoreboard.
 2. Stale targets can't be hit: if the newest accepted state of a player is older than
    max_lag + 1/their rate, they are drawn as a ghost and shots go through them (no hit report).
 3. The victim checks every hit: each hit names the seq of the victim's state the shooter aimed at
    (tseq). The victim knows when it sent that state, so it knows exactly how old the shooter's view was:
        now - sent_time[tseq]  >  2*max_lag + 1/my_rate + 1/shooter_rate + 0.15 s  ->  hit rejected.
    This uses only the victim's own clock, so it also catches hits that queued up somewhere and arrive
    in a burst after a stall (the old "dies immediately" case).
 4. Hit reports are repeated in every packet (up to 3 per packet) only while they could still be
    accepted, then dropped, so a backlog can't release a pile of old hits.
"""
from __future__ import annotations

import math
import random
import socket
from collections import deque
from typing import Dict, List, Optional, Tuple

from payload_v2 import (FLAG_DEAD, FLAG_FIRED, FLAG_IN_GAME, FLAG_LEAVING, FLAG_RELOADING, MAX_EVENTS,
                     MAX_HEARD, NONE_ID, PAYLOAD_LEN, Event, PlayerState, build_payload, parse_payload)

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

# connection parameters (kept from v6.5)
PEER_TIMEOUT = 3.5    # s without packets -> "link lost" (lobby sends 1/s, so allow a few lost ones)
LOBBY_RATE = 1.0      # packets/s in the lobby, and in the arena while no other player is heard
PRESENCE_PERIOD = 2.0 # ARQ mode, in the arena: one broadcast every 2 s so lobby players still see us

# defaults (main menu / flowgraph variables)
DEFAULT_RATE = 5.0
DEFAULT_MAX_PLAYERS = 4
DEFAULT_MAX_LAG = 0.4
MIN_RATE, MAX_RATE = 0.5, 10.0
MAX_PLAYERS_LIMIT = 8

BCAST = 255
HIT_MARGIN = 0.15     # s of slack in the victim's hit check (frame time on air, rounding)
OVERFLOW_GRACE = 1.5  # s the arena may be over the player cap before the newest player goes back
BASE_CREEP = 0.2      # ms per s: the arrival baseline creeps up (follows clock drift between PCs)
HIST = 4096           # sent-state history (seq -> time) for the hit check

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
SPAWN_CELLS = [(1, 1), (14, 14), (1, 14), (14, 1), (1, 7), (14, 8), (7, 1), (7, 14), (8, 6), (11, 10)]
for _r, _c in SPAWN_CELLS:
    assert MAP[_r][_c] == ".", (_r, _c)

INF = float("inf")


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


def seq_newer(a: int, b: int) -> bool:
    """True if u16 sequence number a is newer than b."""
    return 0 < ((a - b) & 0xFFFF) < 0x8000


# ------------------------------------------------------------------ ray casting (exact, grid DDA)
def wall_hit(ox, oy, oz, d, max_dist=80.0):
    """First wall or floor along the ray. Returns (kind, t) with kind 'wall'|'floor'|'none'."""
    dx, dy, dz = d
    t_floor = -oy / dy if dy < -1e-9 else INF
    limit = min(max_dist, t_floor)
    cx, cz = int(math.floor(ox / CELL)), int(math.floor(oz / CELL))
    sx = 1 if dx > 0 else -1
    sz = 1 if dz > 0 else -1
    tdx = CELL / abs(dx) if abs(dx) > 1e-12 else INF
    tdz = CELL / abs(dz) if abs(dz) > 1e-12 else INF
    tmx = (((cx + (dx > 0)) * CELL - ox) / dx) if abs(dx) > 1e-12 else INF
    tmz = (((cz + (dz > 0)) * CELL - oz) / dz) if abs(dz) > 1e-12 else INF
    t = 0.0
    while t < limit:
        if solid_cell(cz, cx):
            t_exit = min(tmx, tmz)
            if oy + dy * t < WALL_H:
                return "wall", t
            if dy < 0 and oy + dy * t_exit < WALL_H:
                return "wall", (WALL_H - oy) / dy
        if tmx < tmz:
            t, tmx, cx = tmx, tmx + tdx, cx + sx
        else:
            t, tmz, cz = tmz, tmz + tdz, cz + sz
    if t_floor <= max_dist:
        return "floor", t_floor
    return "none", max_dist


def _hit_player(o, d, pos) -> Tuple[float, str]:
    """Distance along the ray to a player's hitbox (head sphere / body cylinder) and which part."""
    ox, oy, oz = o
    dx, dy, dz = d
    tx, ty, tz = pos
    best, part = INF, ""
    # head sphere
    cx, cy, cz = ox - tx, oy - (ty + HEAD_Y), oz - tz
    b = cx * dx + cy * dy + cz * dz
    c = cx * cx + cy * cy + cz * cz - HEAD_R * HEAD_R
    disc = b * b - c
    if disc >= 0:
        t = -b - math.sqrt(disc)
        if t < 0 <= -b + math.sqrt(disc):
            t = 0.0
        if t >= 0:
            best, part = t, "head"
    # body cylinder (vertical, ty..ty+BODY_TOP)
    a = dx * dx + dz * dz
    px, pz = ox - tx, oz - tz
    if a > 1e-12:
        b = px * dx + pz * dz
        c = px * px + pz * pz - BODY_R * BODY_R
        disc = b * b - a * c
        if disc >= 0:
            t = (-b - math.sqrt(disc)) / a
            if t >= 0 and ty <= oy + dy * t <= ty + BODY_TOP and t < best:
                best, part = t, "body"
    for yc in (ty + BODY_TOP, ty):                     # caps
        if abs(dy) > 1e-12:
            t = (yc - oy) / dy
            if 0 <= t < best:
                qx, qz = px + dx * t, pz + dz * t
                if qx * qx + qz * qz < BODY_R * BODY_R:
                    best, part = t, "body"
    return best, part


def raycast(ox, oy, oz, d, max_dist=80.0, targets=()):
    """targets: iterable of (key, (x, y_feet, z)). Returns (kind, dist, damage, key) with kind
    'head'|'body'|'wall'|'floor'|'none'; key is the hit target's key (or None)."""
    kind, dist = wall_hit(ox, oy, oz, d, max_dist)
    hit_key, dmg = None, 0
    for key, pos in targets:
        t, part = _hit_player((ox, oy, oz), d, pos)
        if t < dist:
            kind, dist, hit_key = part, t, key
            dmg = DMG_HEAD if part == "head" else DMG_BODY
    return kind, dist, dmg, hit_key


def line_of_sight(a, b) -> bool:
    dist = math.dist(a, b)
    if dist < 1e-6:
        return True
    d = tuple((b[i] - a[i]) / dist for i in range(3))
    kind, t = wall_hit(a[0], a[1], a[2], d, dist)
    return kind == "none" or t >= dist - 0.1


# ------------------------------------------------------------------ the local fighter
class Fighter:
    def __init__(self, pid: int, name: str = ""):
        self.pid = pid
        self.name = name
        self.kills = 0
        self.deaths = 0
        self.killer = NONE_ID
        self.eid = 0
        self.pending: List[dict] = []                 # hit reports still being sent
        self.fired_since_send = False
        self.x = self.z = self.y = self.vy = 0.0
        self.yaw = self.pitch = 0.0
        self.hp = 0
        self.ammo = MAG
        self.reload_t = self.fire_cd = self.dead_t = 0.0
        self.spawned = False

    def respawn(self, cell):
        self.x, self.z = cell_center(*cell)
        self.y = self.vy = 0.0
        cx, cz = COLS * CELL / 2, ROWS * CELL / 2           # face the middle
        self.yaw = math.atan2(cx - self.x, -(cz - self.z)) % (2 * math.pi)
        self.pitch = 0.0
        self.hp = MAX_HP
        self.ammo = MAG
        self.reload_t = self.fire_cd = self.dead_t = 0.0
        self.spawned = True

    @property
    def alive(self) -> bool:
        return self.hp > 0

    @property
    def reloading(self) -> bool:
        return self.reload_t > 0

    def eye(self):
        return self.x, self.y + EYE_H, self.z

    def update(self, dt, fwd=0.0, strafe=0.0, sprint=False, jump=False) -> bool:
        """fwd/strafe in -1..1 relative to view yaw. Returns True when the respawn timer ran out."""
        if not self.alive:
            self.dead_t -= dt
            return self.dead_t <= 0
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
        return False

    def look(self, dyaw, dpitch):
        self.yaw = (self.yaw + dyaw) % (2 * math.pi)
        self.pitch = max(-1.55, min(1.55, self.pitch + dpitch))

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

    def take_damage(self, dmg: int, shooter: int) -> bool:
        """Returns True if this hit killed us."""
        if not self.alive:
            return False
        self.hp = max(0, self.hp - dmg)
        if self.hp == 0:
            self.deaths = (self.deaths + 1) & 0xFF
            self.killer = shooter
            self.dead_t = RESPAWN_TIME
            self.reload_t = 0.0
            return True
        return False


# ------------------------------------------------------------------ another player, as this node sees it
class RemotePlayer:
    def __init__(self, pid: int):
        self.pid = pid
        self.name = f"P{pid}"
        self.state: Optional[PlayerState] = None       # newest ACCEPTED state (position, hp, events)
        self.x = self.y = self.z = 0.0                 # drawn pose (smoothed + short prediction)
        self.yaw = self.pitch = 0.0
        self.vx = self.vy = self.vz = 0.0
        self.since_rx = 0.0
        self.first_rx: Optional[float] = None
        self.last_rx = -INF                           # any packet (link alive)
        self.accepted_t = -INF                        # newest accepted state: arrival time ...
        self.accepted_lag = 0.0                       # ... and its lag at arrival
        self.last_seq: Optional[int] = None           # newest seq seen (accepted or late)
        self.rx = 0
        self.lost = 0
        self.late = 0                                 # dropped because lag > max_lag
        self.rx_times: deque = deque(maxlen=60)
        # lag estimation
        self.base: Optional[float] = None             # arrival baseline: min of (my ms - ts), mod 65536
        self.base_t = 0.0
        self.lag = 0.0                                # newest packet's lag estimate (s)
        self.lag_avg: Optional[float] = None
        self.rtt: Optional[float] = None              # smoothed round trip (s), hold time removed
        self.rtt_min = INF
        self._echo_used: Optional[int] = None
        # what the player says about itself (from every in-order packet, late or not)
        self.in_game = False
        self.in_game_since: Optional[float] = None    # local time first seen in the arena (None = unknown)
        self.leaving = False
        self.rate = DEFAULT_RATE
        self.max_players = DEFAULT_MAX_PLAYERS
        self.kills = 0
        self.deaths = 0
        self.killer = NONE_ID
        self.heard_me_t = -INF
        self.not_heard_me_t = -INF
        self.seen_eids: deque = deque(maxlen=512)
        self.tracer_t = 0.0
        self.hit_flash = 0.0

    # ---------------------------------------------------------------- status
    def connected(self, now) -> bool:
        return not self.leaving and now - self.last_rx < PEER_TIMEOUT

    @property
    def alive(self) -> bool:
        return self.state is not None and not (self.state.flags & FLAG_DEAD)

    def state_age(self, now) -> float:
        """How old the pose we show is, in s (lag when it arrived + time since)."""
        return now - self.accepted_t + self.accepted_lag

    def fresh(self, now, max_lag) -> bool:
        return self.state_age(now) <= max_lag + 1.0 / max(self.rate, MIN_RATE) + 0.05

    def pkt_rate(self, now) -> float:
        return sum(1 for t in self.rx_times if now - t < 2.0) / 2.0

    def loss_pct(self) -> float:
        total = self.rx + self.lost
        return 100.0 * self.lost / total if total else 0.0

    # ---------------------------------------------------------------- lag
    def measure_lag(self, now: float, now_ms: int, ts: int) -> float:
        """Returns this packet's estimated one-way lag in s."""
        off = (now_ms - ts) & 0xFFFF
        if self.base is None:
            self.base, self.base_t = float(off), now
        else:
            self.base = (self.base + BASE_CREEP * (now - self.base_t)) % 65536.0
            self.base_t = now
            extra = ((off - self.base + 32768.0) % 65536.0) - 32768.0
            if extra < 0:
                self.base = float(off)
        extra = (((off - self.base + 32768.0) % 65536.0) - 32768.0) / 1000.0
        floor = self.rtt_min / 2.0 if self.rtt_min < INF else 0.0
        self.lag = floor + extra
        self.lag_avg = self.lag if self.lag_avg is None else 0.85 * self.lag_avg + 0.15 * self.lag
        return self.lag

    # ---------------------------------------------------------------- drawing pose
    def accept_pose(self, st: PlayerState, now: float):
        prev = self.state
        snap = prev is None or (prev.flags & FLAG_DEAD and not st.flags & FLAG_DEAD) or now - self.accepted_t > 1.0
        if snap:
            self.vx = self.vy = self.vz = 0.0
            self.x, self.y, self.z, self.yaw, self.pitch = st.x, st.y, st.z, st.yaw, st.pitch
        else:
            span = max(now - self.accepted_t, 0.02)
            lim = WALK * SPRINT * 1.2
            self.vx = max(-lim, min(lim, (st.x - prev.x) / span))
            self.vz = max(-lim, min(lim, (st.z - prev.z) / span))
            self.vy = max(-JUMP_V, min(JUMP_V, (st.y - prev.y) / span))
        self.state = st
        self.since_rx = 0.0
        if st.flags & FLAG_FIRED:
            self.tracer_t = 0.08

    def update(self, dt):
        """Smooth toward the newest accepted pose, moved on by the estimated velocity for up to 0.3 s."""
        if self.state is None:
            return
        self.since_rx += dt
        k = min(1.0, dt * 12.0)
        s = self.state
        ahead = 0.0 if s.flags & FLAG_DEAD else min(self.since_rx, 0.3)
        tx, tz = s.x + self.vx * ahead, s.z + self.vz * ahead
        if collides(tx, tz):
            tx, tz = s.x, s.z
        ty = max(0.0, s.y + self.vy * ahead)
        self.x += (tx - self.x) * k
        self.y += (ty - self.y) * k
        self.z += (tz - self.z) * k
        self.yaw += wrap_angle(s.yaw - self.yaw) * k
        self.pitch += (s.pitch - self.pitch) * k
        self.tracer_t = max(0.0, self.tracer_t - dt)
        self.hit_flash = max(0.0, self.hit_flash - dt)


# ------------------------------------------------------------------ links
class RadioLink:
    """Game <-> the v6.8 flowgraph's 'Game Link' block over UDP on this PC.

    TX to 127.0.0.1:tx_port (52001): datagram = dest node 1 B | 58-byte payload   (dest 255 = everyone)
    RX on 127.0.0.1:rx_port (52002): datagram = src node 1 B | dest node 1 B | payload
    The block adds the 2-byte C5 11 tag on air and removes it on receive."""

    def __init__(self, my_node: int, tx_port=52001, rx_port=52002, host="127.0.0.1"):
        self.my_node = int(my_node)
        self.tx = (host, int(tx_port))
        self.out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.bind((host, int(rx_port)))
        except OSError as e:
            self.sock.close()
            raise OSError(f"cannot listen on udp {host}:{rx_port} ({e}). Is another copy of the game running?") from e
        self.sock.setblocking(False)
        self.ignored = 0
        self.send_errors = 0
        self.desc = f"radio (v6.8 Game Link, node {self.my_node})"

    def send(self, data: bytes, dest: int):
        try:
            self.out.sendto(bytes([dest & 0xFF]) + data, self.tx)
        except OSError:
            self.send_errors += 1                      # flowgraph not running yet - keep going

    def poll(self) -> List[Tuple[int, bytes]]:
        out = []
        while True:
            try:
                d, _ = self.sock.recvfrom(65535)
            except (BlockingIOError, InterruptedError, OSError):
                return out
            if len(d) == 2 + PAYLOAD_LEN and d[2] == d[0] and d[0] != self.my_node:
                out.append((d[0], d[2:]))
            else:
                self.ignored += 1

    def close(self):
        for s in (self.sock, self.out):
            try:
                s.close()
            except OSError:
                pass


class LocalHub:
    """In-process 'radio' for practice and tests: every endpoint hears every frame after `delay` s,
    each frame lost with probability `loss`. Call hub.advance(dt) once per frame."""

    def __init__(self, delay=0.03, loss=0.0, seed=None):
        self.delay, self.loss = delay, loss
        self.rng = random.Random(seed)
        self.now = 0.0
        self.eps: Dict[int, "LocalHub.Endpoint"] = {}

    class Endpoint:
        def __init__(self, hub, node):
            self.hub, self.node = hub, node
            self.inbox: List[Tuple[float, int, bytes]] = []
            self.desc = "practice (local)"

        def send(self, data: bytes, dest: int):
            h = self.hub
            for n, ep in h.eps.items():
                if n == self.node or (dest != BCAST and dest != n):
                    continue
                if h.rng.random() >= h.loss:
                    ep.inbox.append((h.now + h.delay, self.node, bytes(data)))

        def poll(self):
            now = self.hub.now
            due = [(s, d) for t, s, d in self.inbox if t <= now]
            self.inbox = [m for m in self.inbox if m[0] > now]
            return due

        def close(self):
            self.hub.eps.pop(self.node, None)

    def endpoint(self, node: int) -> "LocalHub.Endpoint":
        ep = LocalHub.Endpoint(self, node)
        self.eps[node] = ep
        return ep

    def advance(self, dt):
        self.now += dt


# ------------------------------------------------------------------ the session: lobby + arena
class Session:
    """One node's game. mode 'lobby' (waiting, 1 packet/s) or 'game' (in the arena).

    link: .send(payload, dest) and .poll() -> [(src, payload)]
    rate: packets/s in the arena. max_players: arena cap. max_lag: s, see the module docstring.
    arq:  True  = state goes unicast to each player in the arena (the v6.8 MAC sends it with SR-ARQ),
                  plus a broadcast every PRESENCE_PERIOD s for the lobby.
          False = everything broadcast (one frame reaches everyone, no ARQ, no Block ACK).
    protect: False switches the lag protection off (only for the comparison in the simulation)."""

    def __init__(self, node: int, link, name: str = "", rate=DEFAULT_RATE, max_players=DEFAULT_MAX_PLAYERS,
                 max_lag=DEFAULT_MAX_LAG, arq=True, clock_offset=0.0, seed=None, protect=True):
        self.node = int(node)
        self.link = link
        self.me = Fighter(self.node, (name or f"P{node}")[:8])
        self.rate = max(MIN_RATE, min(MAX_RATE, float(rate)))
        self.max_players = max(2, min(MAX_PLAYERS_LIMIT, int(max_players)))
        self.max_lag = float(max_lag)
        self.arq = bool(arq)
        self.protect = protect
        self.clock_offset = clock_offset
        self.rng = random.Random(seed if seed is not None else node)
        self.mode = "lobby"
        self.now = 0.0
        self.players: Dict[int, RemotePlayer] = {}
        self.seq = 0
        self.sent_hist: List[Optional[Tuple[int, float]]] = [None] * HIST
        self.respawn_seq = 0
        self.joined_t = 0.0
        self.send_t = 0.0
        self.presence_t = 0.0
        self.echo_rr = 0
        self.heard_rr = 0
        self.over_t = 0.0
        self.sent = 0
        self.frames = 0
        # effects / HUD
        self.hitmarker_t = 0.0
        self.damage_t = 0.0
        self.ghost_t = 0.0
        self.tracers: List[list] = []
        self.shots: deque = deque(maxlen=64)
        self.messages: deque = deque(maxlen=5)
        self.kill_feed: deque = deque(maxlen=5)          # [killer, victim, ttl]
        # statistics (HUD + simulation)
        self.stats = dict(hits_reported=0, ghost_shots=0, hits_taken=0, hits_rejected_stale=0,
                          hits_rejected_unknown=0, hits_rejected_dead=0, late_dropped=0, accepted=0,
                          out_of_order=0, events_expired=0)
        self.on_fire = None          # hook(session, eid, target_pid, origin, dir, view_pos, tseq) for the simulation
        self.on_hit_taken = None     # hook(session, shooter_pid, eid, dmg, accepted, reason)

    # ---------------------------------------------------------------- clock
    def now_ms(self) -> int:
        return int(math.floor((self.now + self.clock_offset) * 1000.0)) & 0xFFFF

    def msg(self, text, ttl=3.0):
        self.messages.append([text, ttl])

    # ---------------------------------------------------------------- arena membership
    def arena_peers(self) -> List[RemotePlayer]:
        return [p for p in self.players.values() if p.in_game and p.connected(self.now)]

    def cap(self) -> int:
        caps = [self.max_players] + [p.max_players for p in self.arena_peers() if p.max_players >= 2]
        return min(caps)

    def can_join(self) -> Tuple[bool, str]:
        n = len(self.arena_peers())
        if n + 1 > self.cap():
            return False, f"Arena full ({n}/{self.cap()})"
        return True, ""

    def join_game(self) -> Tuple[bool, str]:
        ok, why = self.can_join()
        if not ok:
            return False, why
        self.mode = "game"
        self.joined_t = self.now
        self.me.kills = 0
        self.me.deaths = 0
        self.me.killer = NONE_ID
        self.me.pending.clear()
        self._respawn()
        self.send_t = 0.0                          # announce right away
        self.over_t = 0.0
        return True, ""

    def leave_game(self, why: str = ""):
        if self.mode != "game":
            return
        self.mode = "lobby"
        self.me.hp = 0
        self.me.spawned = False
        self.me.pending.clear()
        self.send_t = 0.0
        if why:
            self.msg(why, 5)

    def shutdown(self):
        """Tell everyone we quit (LEAVING), so they drop us from their lists at once."""
        for _ in range(2):
            self._send_packets(leaving=True)

    def _rank_above_me(self, p: RemotePlayer) -> bool:
        """Is p ahead of me for an arena seat? Earlier arrivals first; near-ties by lower node ID."""
        if p.in_game_since is None:
            return p.pid < self.node
        if p.in_game_since < self.joined_t - 1.0:
            return True
        if p.in_game_since > self.joined_t + 1.0:
            return False
        return p.pid < self.node

    def _check_overflow(self, dt):
        peers = self.arena_peers()
        cap = self.cap()
        if len(peers) + 1 <= cap:
            self.over_t = 0.0
            return
        self.over_t += dt
        if self.over_t < OVERFLOW_GRACE:
            return
        ahead = sum(1 for p in peers if self._rank_above_me(p))
        if ahead >= cap:
            self.leave_game(f"Arena full ({cap} players) - back in the lobby")

    def _respawn(self):
        others = [(p.state.x, p.state.z) for p in self.arena_peers() if p.alive]
        best, best_d = None, -1.0
        cells = list(SPAWN_CELLS)
        self.rng.shuffle(cells)
        for cell in cells:
            x, z = cell_center(*cell)
            d = min((math.hypot(x - ox, z - oz) for ox, oz in others), default=99.0)
            if d > best_d:
                best, best_d = cell, d
        self.me.respawn(best)
        self.respawn_seq = self.seq

    # ---------------------------------------------------------------- shooting
    def targets(self):
        """(live targets, ghosts): ghosts are players whose shown state is too old to be hit."""
        live, ghosts = [], []
        for p in self.arena_peers():
            if not p.alive:
                continue
            if not self.protect or p.fresh(self.now, self.max_lag):
                live.append((p.pid, (p.x, p.y, p.z)))
            else:
                ghosts.append((p.pid, (p.x, p.y, p.z)))
        return live, ghosts

    def fire(self):
        me = self.me
        if not me.try_fire():
            return
        ex, ey, ez = me.eye()
        d = look_dir(me.yaw, me.pitch)
        live, ghosts = self.targets()
        kind, dist, dmg, pid = raycast(ex, ey, ez, d, targets=live)
        if ghosts:
            gk, gdist, _, gpid = raycast(ex, ey, ez, d, max_dist=dist, targets=ghosts)
            if gpid is not None and gdist < dist:
                self.stats["ghost_shots"] += 1
                if self.ghost_t <= 0:
                    p = self.players[gpid]
                    self.msg(f"{p.name} is lagging ({p.state_age(self.now) * 1000:.0f} ms) - hit not counted", 2)
                self.ghost_t = 1.0
        end = (ex + d[0] * dist, ey + d[1] * dist, ez + d[2] * dist)
        self.tracers.append([(ex, ey - 0.15, ez), end, 0.06])
        self.shots.append((end, d, kind))
        if pid is not None:
            p = self.players[pid]
            me.eid = (me.eid + 1) & 0xFFFF
            tseq = p.state.seq
            me.pending.append(dict(eid=me.eid, target=pid, dmg=dmg, tseq=tseq, t=self.now, sends=0,
                                   left=6, window=self._hit_window(p.rate) - p.state_age(self.now)))
            self.stats["hits_reported"] += 1
            self.hitmarker_t = 0.15 if kind == "body" else 0.3
            p.hit_flash = 0.12
            if self.on_fire:
                self.on_fire(self, me.eid, pid, (ex, ey, ez), d, (p.x, p.y, p.z), tseq)

    def _hit_window(self, their_rate: float) -> float:
        """How old a state of the victim may be when its hit arrives (see the module docstring)."""
        return 2 * self.max_lag + 1.0 / max(self.rate, MIN_RATE) + 1.0 / max(their_rate, MIN_RATE) + HIT_MARGIN

    def _pick_events(self, dest: int) -> List[Event]:
        me = self.me
        if self.protect:
            keep = []
            for e in me.pending:
                if self.now - e["t"] > e["window"]:
                    self.stats["events_expired"] += 1
                else:
                    keep.append(e)
            me.pending = keep
            cand = [e for e in me.pending if dest == BCAST or e["target"] == dest]
            cand.sort(key=lambda e: (e["sends"], e["t"]))
            out = cand[:MAX_EVENTS]
            for e in out:
                e["sends"] += 1
        else:                                         # v6.5 behaviour: each report in 6 packets in a row
            out = [e for e in me.pending if dest == BCAST or e["target"] == dest][:MAX_EVENTS]
            for e in out:
                e["left"] -= 1
            me.pending = [e for e in me.pending if e["left"] > 0]
        return [Event(e["eid"], e["target"], e["dmg"], e["tseq"]) for e in out]

    # ---------------------------------------------------------------- receiving
    def _receive(self):
        now = self.now
        for src, data in self.link.poll():
            if len(data) < PAYLOAD_LEN:
                continue
            st = parse_payload(data)
            if st.pid == self.node or st.pid != src:
                continue
            p = self.players.get(st.pid)
            if p is None:
                p = self.players[st.pid] = RemotePlayer(st.pid)
            if p.leaving and not st.flags & FLAG_LEAVING:
                p.leaving = False
            self.frames += 1
            p.last_rx = now
            p.rx_times.append(now)
            if p.first_rx is None:
                p.first_rx = now
            lag = p.measure_lag(now, self.now_ms(), st.ts)
            self._on_echo(p, st)
            # newest seq only
            if p.last_seq is not None:
                gap = (st.seq - p.last_seq) & 0xFFFF
                if gap == 0 or gap >= 0x8000:
                    self.stats["out_of_order"] += 1
                    continue
                if gap < 1000:
                    p.lost += gap - 1
            p.last_seq = st.seq
            p.rx += 1
            # what the player says about itself: always taken (newest wins)
            self._take_meta(p, st)
            if st.flags & FLAG_LEAVING:
                p.leaving = True
                p.in_game = False
                self.msg(f"{p.name} left", 3)
                continue
            # gameplay state: only if it is not too late
            if self.protect and self.mode == "game" and lag > self.max_lag:
                p.late += 1
                self.stats["late_dropped"] += 1
                continue
            p.accept_pose(st, now)
            p.accepted_t, p.accepted_lag = now, (max(0.0, lag) if self.protect else 0.0)
            self.stats["accepted"] += 1
            if self.mode == "game" and p.in_game:
                self._apply_events(p, st)

    def _take_meta(self, p: RemotePlayer, st: PlayerState):
        now = self.now
        if st.name:
            p.name = st.name
        p.rate = max(MIN_RATE, st.rate) if st.rate > 0 else p.rate
        p.max_players = st.max_players or p.max_players
        was = p.in_game
        p.in_game = bool(st.flags & FLAG_IN_GAME)
        if p.in_game and not was:
            # first packet ever from a player already in the arena: we don't know when it joined
            p.in_game_since = None if p.rx <= 1 else now
            if self.mode == "game" and p.rx > 1:
                self.msg(f"{p.name} joined the arena", 3)
        elif was and not p.in_game:
            p.in_game_since = None
            if self.mode == "game":
                self.msg(f"{p.name} went back to the lobby", 3)
        # kills / deaths are counters owned by that player
        if p.rx > 1 and st.deaths != p.deaths and ((st.deaths - p.deaths) & 0xFF) < 100 and p.in_game:
            killer = st.killer
            if killer == self.node and self.mode == "game":
                self.me.kills = (self.me.kills + ((st.deaths - p.deaths) & 0xFF)) & 0xFF
                self.msg(f"You eliminated {p.name}!", 3)
            kname = "you" if killer == self.node else (self.players[killer].name if killer in self.players else f"P{killer}")
            self.kill_feed.append([kname, p.name, 5.0])
        p.kills, p.deaths, p.killer = st.kills, st.deaths, st.killer
        # does this player hear me?
        if self.node in st.heard:
            p.heard_me_t = now
        elif len(st.heard) < MAX_HEARD:
            p.not_heard_me_t = now

    def _on_echo(self, p: RemotePlayer, st: PlayerState):
        if st.echo_id != self.node:
            return
        if p._echo_used == st.echo_seq:
            return
        p._echo_used = st.echo_seq
        rec = self.sent_hist[st.echo_seq % HIST]
        if rec is None or rec[0] != st.echo_seq:
            return
        sample = self.now - rec[1] - st.echo_age / 1000.0
        if -0.05 <= sample < 30.0:
            sample = max(0.0, sample)
            p.rtt_min = min(p.rtt_min, sample)
            p.rtt = sample if p.rtt is None else 0.8 * p.rtt + 0.2 * sample

    def _apply_events(self, p: RemotePlayer, st: PlayerState):
        me = self.me
        for e in st.events:
            if e.target != self.node or e.eid in p.seen_eids:
                continue
            p.seen_eids.append(e.eid)
            ok, why = True, ""
            if self.protect:
                rec = self.sent_hist[e.tseq % HIST]
                if rec is None or rec[0] != e.tseq or not seq_newer(e.tseq + 1, self.respawn_seq):
                    ok, why = False, "unknown"           # aimed at a state from before my respawn / unknown
                elif self.now - rec[1] > 2 * self.max_lag + 1.0 / max(self.current_rate(), MIN_RATE) \
                        + 1.0 / max(p.rate, MIN_RATE) + HIT_MARGIN:
                    ok, why = False, "stale"            # the shooter was looking at an old version of me
            if ok and not me.alive:
                ok, why = False, "dead"
            if ok:
                self.stats["hits_taken"] += 1
                self.damage_t = 0.4
                if me.take_damage(e.damage, p.pid):
                    self.msg(f"Eliminated by {p.name} - respawn in {RESPAWN_TIME:.0f}s", 4)
                    self.kill_feed.append([p.name, "you", 5.0])
            else:
                self.stats["hits_rejected_" + why] += 1
            if self.on_hit_taken:
                self.on_hit_taken(self, p.pid, e.eid, e.damage, ok, why)

    # ---------------------------------------------------------------- sending
    def current_rate(self) -> float:
        """Arena rate while another arena player is heard, LOBBY_RATE otherwise."""
        if self.mode == "game" and self.arena_peers():
            return self.rate
        return min(self.rate, LOBBY_RATE)

    def _heard_list(self) -> List[int]:
        heard = sorted(p.pid for p in self.players.values() if p.connected(self.now))
        if len(heard) > MAX_HEARD:                    # more than 3: rotate so everyone gets mentioned
            self.heard_rr = (self.heard_rr + MAX_HEARD) % len(heard)
            heard = (heard[self.heard_rr:] + heard[:self.heard_rr])[:MAX_HEARD]
        return heard

    def _echo_for(self, dest: int) -> Tuple[int, int, int]:
        cands = [p for p in self.players.values() if p.connected(self.now) and p.last_seq is not None]
        if dest != BCAST:
            cands = [p for p in cands if p.pid == dest] or cands
        if not cands:
            return NONE_ID, 0, 0
        self.echo_rr = (self.echo_rr + 1) % len(cands)
        p = sorted(cands, key=lambda q: q.pid)[self.echo_rr]
        age = int(min(65.0, max(0.0, self.now - p.last_rx)) * 1000)
        return p.pid, p.last_seq, age

    def _send_packets(self, leaving=False):
        me = self.me
        seq = self.seq
        self.seq = (self.seq + 1) & 0xFFFF
        self.sent_hist[seq % HIST] = (seq, self.now)
        flags = 0
        in_game = self.mode == "game" and not leaving
        if in_game:
            flags |= FLAG_IN_GAME
            if me.fired_since_send:
                flags |= FLAG_FIRED
            if me.reloading:
                flags |= FLAG_RELOADING
            if not me.alive:
                flags |= FLAG_DEAD
        else:
            flags |= FLAG_DEAD
        if leaving:
            flags |= FLAG_LEAVING
        me.fired_since_send = False
        heard = self._heard_list()

        def build(dest):
            eid, eseq, eage = self._echo_for(dest)
            st = PlayerState(pid=self.node, seq=seq, ts=self.now_ms(), flags=flags,
                             x=me.x, y=me.y, z=me.z, yaw=me.yaw, pitch=me.pitch,
                             hp=me.hp if in_game else 0, ammo=me.ammo, kills=me.kills, deaths=me.deaths,
                             killer=me.killer, rate=self.rate, max_players=self.max_players,
                             echo_id=eid, echo_seq=eseq, echo_age=eage, heard=heard, name=me.name,
                             events=self._pick_events(dest) if in_game else [])
            return build_payload(st)

        peers = [p for p in self.arena_peers()] if in_game else []
        if self.arq and peers:
            for p in sorted(peers, key=lambda q: q.pid):
                self.link.send(build(p.pid), p.pid)
                self.sent += 1
            if self.now - self.presence_t >= PRESENCE_PERIOD or leaving:
                self.presence_t = self.now
                self.link.send(build(BCAST), BCAST)
                self.sent += 1
        else:
            self.link.send(build(BCAST), BCAST)
            self.sent += 1

    # ---------------------------------------------------------------- one frame
    def step(self, dt, fwd=0.0, strafe=0.0, sprint=False, jump=False, firing=False):
        self.now += dt
        me = self.me
        if self.mode == "game":
            was_alive = me.alive
            if me.update(dt, fwd, strafe, sprint, jump):
                self._respawn()
                self.msg("Respawned")
            if firing and was_alive:
                self.fire()

        self._receive()
        for p in list(self.players.values()):
            p.update(dt)
            if not p.connected(self.now) and self.now - p.last_rx > 30.0:
                del self.players[p.pid]                # forget players gone for 30 s
            elif p.in_game and not p.connected(self.now):
                p.in_game = False
                p.in_game_since = None
        if self.mode == "game":
            self._check_overflow(dt)

        self.send_t -= dt
        rate = self.current_rate()
        if self.send_t > 1.0 / rate:                  # just switched to the faster rate
            self.send_t = 1.0 / rate
        if self.send_t <= 0:
            self.send_t = max(self.send_t + 1.0 / rate, 0.0)
            self._send_packets()

        self.hitmarker_t = max(0.0, self.hitmarker_t - dt)
        self.damage_t = max(0.0, self.damage_t - dt)
        self.ghost_t = max(0.0, self.ghost_t - dt)
        for t in self.tracers:
            t[2] -= dt
        self.tracers = [t for t in self.tracers if t[2] > 0]
        for lst in (self.messages, self.kill_feed):
            for m in lst:
                m[-1] -= dt
            while lst and lst[0][-1] <= 0:
                lst.popleft()

    # ---------------------------------------------------------------- link state for the UI
    def link_state(self, p: RemotePlayer) -> str:
        """'lost', 'two-way', 'one-way' (they don't hear me) or 'linked' (not known yet)."""
        if not p.connected(self.now):
            return "lost"
        if p.not_heard_me_t > p.heard_me_t and self.now - p.not_heard_me_t < PEER_TIMEOUT:
            return "one-way"
        if self.now - p.heard_me_t < PEER_TIMEOUT:
            return "two-way"
        return "linked"

    def lagging(self, p: RemotePlayer) -> bool:
        return p.connected(self.now) and p.lag_avg is not None and p.lag_avg > self.max_lag

    def roster(self) -> List[RemotePlayer]:
        """Known players for the lobby list, arena players first."""
        ps = [p for p in self.players.values() if not p.leaving]
        return sorted(ps, key=lambda p: (not p.in_game, not p.connected(self.now), p.pid))


# ------------------------------------------------------------------ bots (practice mode and simulation)
class BotBrain:
    """Drives a Session like a player would: walks waypoints, aims at the nearest visible fresh player
    (with aim error), fires bursts, reloads. Uses only what its Session received over the link."""

    def __init__(self, session: Session, seed=None, skill=0.5, aggressive=True):
        self.s = session
        self.rng = random.Random(seed)
        self.skill = skill
        self.aggressive = aggressive
        self.waypoint = cell_center(*self.rng.choice(open_cells()))
        self.stuck_t = 0.0
        self.last_dist = 1e9
        self.think_t = 0.0
        self.strafe_dir = 1.0
        self.aim_err = (0.0, 0.0)

    def step(self, dt):
        s, b = self.s, self.s.me
        fwd = strafe = 0.0
        firing = False
        if s.mode == "game" and b.alive:
            target = None
            best = 1e9
            live, _ = s.targets()
            for pid, pos in live:
                chest = (pos[0], pos[1] + 1.2, pos[2])
                dd = math.dist(b.eye(), chest)
                if dd < 30 and dd < best and line_of_sight(b.eye(), chest):
                    target, best = pos, dd
            if target is not None and self.aggressive:
                tx, ty, tz = target
                want_yaw = math.atan2(tx - b.x, -(tz - b.z))
                horiz = math.hypot(tx - b.x, tz - b.z)
                want_pitch = math.atan2(ty + 1.1 - (b.y + EYE_H), horiz)
                self.think_t -= dt
                if self.think_t <= 0:
                    self.think_t = self.rng.uniform(0.5, 1.2)
                    self.strafe_dir = self.rng.choice((-1.0, 1.0))
                    spread = 0.10 * (1.2 - self.skill)
                    self.aim_err = (self.rng.gauss(0, spread), self.rng.gauss(0, spread * 0.6))
                b.yaw = (b.yaw + wrap_angle(want_yaw + self.aim_err[0] - b.yaw) * min(1, dt * 8)) % (2 * math.pi)
                b.pitch += (want_pitch + self.aim_err[1] - b.pitch) * min(1, dt * 8)
                strafe = self.strafe_dir
                fwd = 0.5 if horiz > 8 else (-0.3 if horiz < 3 else 0.0)
                firing = abs(wrap_angle(want_yaw - b.yaw)) < 0.25 and b.ammo > 0
            else:
                wx, wz = self.waypoint
                d = math.hypot(wx - b.x, wz - b.z)
                want_yaw = math.atan2(wx - b.x, -(wz - b.z))
                b.yaw = (b.yaw + wrap_angle(want_yaw - b.yaw) * min(1, dt * 5)) % (2 * math.pi)
                b.pitch *= 0.9
                fwd = 1.0 if abs(wrap_angle(want_yaw - b.yaw)) < 0.8 else 0.2
                self.stuck_t = self.stuck_t + dt if d > self.last_dist - 0.01 else 0.0
                self.last_dist = d
                if d < 0.8 or self.stuck_t > 1.0:
                    self.waypoint = cell_center(*self.rng.choice(open_cells()))
                    self.stuck_t, self.last_dist = 0.0, 1e9
                if b.ammo < MAG // 2:
                    b.start_reload()
        s.step(dt, fwd=fwd, strafe=strafe, firing=firing)


class Practice:
    """Offline practice: you + bots on a LocalHub. Each bot is a full Session (same protocol)."""

    NAMES = ["Bot-A", "Bot-B", "Bot-C", "Bot-D", "Bot-E", "Bot-F", "Bot-G"]

    def __init__(self, my_node=0, n_bots=2, rate=DEFAULT_RATE, max_players=DEFAULT_MAX_PLAYERS,
                 max_lag=DEFAULT_MAX_LAG, name="You", loss=0.0, delay=0.03, seed=1):
        self.hub = LocalHub(delay=delay, loss=loss, seed=seed)
        self.session = Session(my_node, self.hub.endpoint(my_node), name=name, rate=rate,
                               max_players=max_players, max_lag=max_lag, arq=False)
        self.bots: List[BotBrain] = []
        ids = [i for i in range(1, 20) if i != my_node][:n_bots]
        for k, node in enumerate(ids):
            bs = Session(node, self.hub.endpoint(node), name=self.NAMES[k % len(self.NAMES)], rate=rate,
                         max_players=max_players, max_lag=max_lag, arq=False, clock_offset=7.3 * (k + 1))
            bs.join_game()
            self.bots.append(BotBrain(bs, seed=seed * 31 + node, skill=0.45))
        self.session.link.desc = f"practice vs {n_bots} bot{'s' if n_bots != 1 else ''}"

    def step(self, dt, *a, **kw):
        self.hub.advance(dt)
        for b in self.bots:
            b.step(dt)
        self.session.step(dt, *a, **kw)
