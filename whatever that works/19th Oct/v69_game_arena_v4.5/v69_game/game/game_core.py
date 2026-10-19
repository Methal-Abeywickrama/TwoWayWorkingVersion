"""
game_core.py - Radio Arena v3: everything except drawing. Map, movement, shooting and the
host-authoritative network game (lobby, host election, host world with lag compensation, client
prediction and reconciliation, host migration). No pygame/OpenGL here: it runs headless, and the
tests and the worst-case simulation (tools/worstcase_sim.py) use it directly.

How a game runs
---------------
Lobby      every node broadcasts a BEACON (1/s): name, settings, and the round-trip time to every node it
           hears (each beacon echoes the newest packet heard from each node, so RTTs come for free).
Election   "Enter arena": if a host already runs an arena, join it. Otherwise the nodes that want to play
           exchange beacons for ELECTION_WINDOW s, then every node computes the same winner from the shared
           RTT table: lowest average RTT to the others (ties -> lowest node ID), or a random pick seeded
           by the candidate list (menu option). If two hosts meet, the higher epoch wins, then the lower
           node ID; the other one closes its arena and its players move over.
Host       runs the only real game world. It keeps a circular buffer (HISTORY_S) of every player's
           position by host time. Each INPUT (timestamped position + angle + actions) is checked:
           move  -> reachable at running speed since the last input, not through a wall, player alive
                    and on the same life; else the host keeps its own position and flags CORRECTED.
           fire  -> the shot names the host time the shooter was looking at (snapshot seq + offset).
                    The host rewinds the other players to that time, ray-casts from the shooter's
                    position at the moment of the shot against walls and hitboxes, and applies the damage
                    now. Shots whose view is older than max_lag are rejected (too late), and so are shots
                    at players whose own inputs stopped for longer than max_lag (lagging = ghost).
           HP, deaths, kills and respawns exist only here. SNAPSHOTs (broadcast at the host's rate)
           carry them, plus hit / kill events, plus for each client the last input the host applied.
Client     predicts its own movement at once. Other players are drawn by interpolating snapshots a little
           behind the newest host time. When a snapshot says the host applied input N at a position that
           differs from where the client predicted it for input N (or flags CORRECTED), the client moves
           to the host position plus the movement it made since input N.
Migration  no snapshot for PEER_TIMEOUT s, or the host closes: the remaining arena players elect a new
           host among themselves; it continues from its newest snapshot (positions, HP, scores).
"""
from __future__ import annotations

import bisect
import collections
import math
import random
import socket
import zlib
from collections import deque
from typing import Dict, List, Optional, Tuple

from payload import (A_FIRE, A_HEAL, A_RELOAD, B_ARQ, B_ELECT_RANDOM, B_HOSTING, B_IN_ARENA, B_LEAVING, B_READY,
                     FRAME_BUDGET, FRAME_BUDGET_V69, I_ALIVE, I_IN, I_LEAVING, I_RELOADING, MAX_ACTIONS, MAX_EVENTS, MAX_PEERS,
                     NONE_ID, P_CORRECTED, P_DEAD, P_FIRED, P_HOST, P_RELOADING, S_CLOSING, S_FULL, T_BEACON,
                     T_INPUT, T_SNAPSHOT, Action, Beacon, Input, PeerInfo, Snapshot, SnapEvent, SnapPlayer,
                     build_payload, parse_payload)
from payload import G_HEALING, GAME_TYPES, MAX_DROPS, W_KNIFE, W_RIFLE, W_SNIPER, Drop

# ------------------------------------------------------------------ tuning
CELL = 2.0            # metres per map cell
WALL_H = 4.0          # walls (rays pass over them; players can't climb them)
STEP_H = 0.6          # highest edge a player walks up without jumping (stairs, 0.5 m blocks)
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
JUMP_APEX = JUMP_V * JUMP_V / (2 * GRAVITY)        # 1.0 m
MAG = 30              # rifle (kept for older code / tests)
FIRE_INTERVAL = 0.11  # s between rifle shots (hold LMB = automatic)
RELOAD_TIME = 1.6
DMG_BODY = 20
DMG_HEAD = 50
RESPAWN_TIME = 3.0
# weapons: 1 rifle, 2 sniper (right mouse = scope), 3 knife
WEAPONS = {
    W_RIFLE: dict(name="Rifle", mag=MAG, interval=FIRE_INTERVAL, reload=RELOAD_TIME, body=DMG_BODY, head=DMG_HEAD,
                  range=80.0, auto=True, speed=1.0),
    W_SNIPER: dict(name="Sniper", mag=5, interval=1.25, reload=2.4, body=80, head=150, range=150.0, auto=False,
                   speed=0.9),
    W_KNIFE: dict(name="Knife", mag=0, interval=0.55, reload=0.0, body=55, head=55, range=2.3, auto=True,
                  speed=1.12),
}
SWITCH_TIME = 0.4     # s after switching weapons before it fires
SNIPER_SPREAD = 0.06  # rad: sniper fired without the scope
SCOPE_SPEED = 0.55    # walking speed while scoped
KNIFE_CONE = 0.5      # rad: the knife also hits a body within this angle of the aim (melee is forgiving)
# healing: medkits lie at fixed spots (back PICKUP_RESPAWN s after taken) and drop where a player is eliminated
MEDKIT_MAX = 3        # carried at most
MEDKIT_START = 1      # carried after each respawn
HEAL_AMOUNT = 50      # HP per medkit ...
HEAL_TIME = 3.0       # ... given over this many seconds (H; firing stops it)
HEAL_COOLDOWN = 8.0   # s from one heal to the next
HEAL_SPEED = 0.6      # walking speed while healing
PICKUP_R = 1.0        # m
PICKUP_RESPAWN = 20.0 # s
DROP_TTL = 25.0       # s a dropped medkit stays

# connection parameters (kept from v6.5)
PEER_TIMEOUT = 3.5    # s without packets -> link lost (also: host lost -> migration)
LOBBY_RATE = 1.0      # beacons/s in the lobby; packets/s while nobody else is heard
ARENA_BEACON_S = 3.0  # in the arena a beacon only every 3 s (names + RTT table for elections)
ELECTION_WINDOW = 2.5 # s the players who want to play exchange beacons before the host is chosen
MIGRATION_WINDOW = 1.5
HOST_WAIT = 4.0       # s to wait for the elected host to show up before electing again
FULL_WAIT = 3.0       # s a joining player waits for a seat before going back to the lobby

# defaults (main menu / flowgraph variables)
DEFAULT_RATE = 5.0
DEFAULT_MAX_PLAYERS = 4
DEFAULT_MAX_LAG = 1.0 # s: oldest view a shot may be judged at (max rewind) and the "lagging" limit
MIN_RATE, MAX_RATE = 0.5, 10.0
MAX_PLAYERS_LIMIT = 8

BCAST = 255
HISTORY_S = 3.0       # host: rewind buffer length
EXTRAP_MAX = 0.25     # client: predict others at most this far past the newest snapshot
CORRECT_EPS = 0.30    # m: client prediction error that triggers a correction
CC_QUEUE_S = 0.25     # congestion control: extra round-trip delay that counts as "the radio queue is filling"
CC_MIN_RATE = 1.0     # ... and the lowest packet rate it goes down to
ACTION_SENDS = 6      # each fire / reload action rides in up to 6 inputs (or until acknowledged)
SEQ_RING = 4096

MAP = [
    "################",
    "#..............#",
    "#.666<<....##..#",
    "#.666<<.....#..#",
    "#.666....2..#..#",
    "#........#.....#",
    "#.2..##..#..3..#",
    "#....##........#",
    "#.........2....#",
    "#..#........444#",
    "#..#...##...444#",
    "#..#...#....444#",
    "#.3....#....^^.#",
    "#...........^^.#",
    "#..2...........#",
    "################",
]
# '#' wall (WALL_H high, can't be climbed)      '.' ground
# '1'..'6' block / building, n x 0.5 m high: walk onto 0.5 m, jump onto up to 1.5 m, take the stairs to the rest
# '<' '>' '^' 'v' stairs going UP towards -x, +x, -z (the row above), +z. A run of stair cells climbs from the
#     cell before the run to the cell after it (the two buildings: 3.0 m west, 2.0 m south-east)
ROWS, COLS = len(MAP), len(MAP[0])
SPAWN_CELLS = [(1, 1), (14, 14), (1, 14), (14, 1), (1, 7), (14, 7), (7, 1), (7, 14), (8, 7), (5, 13)]
MEDKIT_SPOTS = [(3, 3), (10, 13), (1, 9), (8, 1), (14, 10), (7, 12)]     # fixed medkit spots (r, c)
STAIRS = {"<": (-1, 0), ">": (1, 0), "^": (0, -1), "v": (0, 1)}            # uphill (dc, dr)

INF = float("inf")
_WALL = ("wall",)


def _build_cells():
    cells = []
    for r in range(ROWS):
        row = []
        for c in range(COLS):
            ch = MAP[r][c]
            row.append(_WALL if ch == "#" else ("flat", 0.0) if ch == "." else
                       ("flat", int(ch) * 0.5) if ch.isdigit() else None)
        cells.append(row)
    for r in range(ROWS):
        for c in range(COLS):
            ch = MAP[r][c]
            if ch not in STAIRS:
                continue
            dc, dr = STAIRS[ch]
            r0, c0 = r, c                                    # downhill end of the run
            while MAP[r0 - dr][c0 - dc] == ch:
                r0, c0 = r0 - dr, c0 - dc
            r1, c1 = r, c                                    # uphill end
            while MAP[r1 + dr][c1 + dc] == ch:
                r1, c1 = r1 + dr, c1 + dc
            lo, hi = cells[r0 - dr][c0 - dc], cells[r1 + dr][c1 + dc]
            assert lo and hi and lo[0] == "flat" and hi[0] == "flat", f"stairs at {(r, c)} need flat cells at both ends"
            n = abs(r1 - r0) + abs(c1 - c0) + 1
            i = abs(r - r0) + abs(c - c0)
            h_dn = lo[1] + (hi[1] - lo[1]) * i / n          # height at this cell's downhill / uphill edge
            h_up = lo[1] + (hi[1] - lo[1]) * (i + 1) / n
            axis = "x" if dc else "z"
            up_pos = (dc or dr) > 0                          # uphill towards +x / +z
            cells[r][c] = ("ramp", axis, h_dn, h_up) if up_pos else ("ramp", axis, h_up, h_dn)
    return cells


CELLS = _build_cells()
for _r, _c in SPAWN_CELLS + MEDKIT_SPOTS:
    assert CELLS[_r][_c][0] == "flat", (_r, _c)


def cell_info(r: int, c: int):
    """('wall',) | ('flat', h) | ('ramp', axis 'x'|'z', h at the cell's low-coordinate edge, h at the high edge)"""
    return CELLS[r][c] if 0 <= r < ROWS and 0 <= c < COLS else _WALL


def solid_cell(r: int, c: int) -> bool:
    """A wall (the climbable blocks and stairs are not 'solid' here)."""
    return cell_info(r, c)[0] == "wall"


def solid_at(x: float, z: float) -> bool:
    return solid_cell(int(math.floor(z / CELL)), int(math.floor(x / CELL)))


def cell_center(r: int, c: int) -> Tuple[float, float]:
    return (c + 0.5) * CELL, (r + 0.5) * CELL


def open_cells() -> List[Tuple[int, int]]:
    return [(r, c) for r in range(ROWS) for c in range(COLS) if not solid_cell(r, c)]


def _surface(info, r, c, x, z) -> float:
    """Height of a cell's top at (x, z), the point clamped into the cell. Walls: INF."""
    if info[0] == "flat":
        return info[1]
    if info[0] == "ramp":
        if info[1] == "x":
            f = (min(max(x, c * CELL), (c + 1) * CELL) - c * CELL) / CELL
        else:
            f = (min(max(z, r * CELL), (r + 1) * CELL) - r * CELL) / CELL
        return info[2] + (info[3] - info[2]) * f
    return INF


def floor_at(x: float, z: float) -> float:
    """Ground height at a point (INF inside a wall)."""
    r, c = int(math.floor(z / CELL)), int(math.floor(x / CELL))
    return _surface(cell_info(r, c), r, c, x, z)


def ray_top_at(x: float, z: float) -> float:
    """Top of whatever stands at (x, z): walls WALL_H, blocks / stairs their height, ground 0."""
    v = floor_at(x, z)
    return WALL_H if v == INF else v


def _footprint(x, z, rad):
    c0, c1 = int(math.floor((x - rad) / CELL)), int(math.floor((x + rad) / CELL))
    r0, r1 = int(math.floor((z - rad) / CELL)), int(math.floor((z + rad) / CELL))
    for rr in range(r0, r1 + 1):
        for cc in range(c0, c1 + 1):
            yield rr, cc


def _top_in(info, r, c, x, z, rad) -> float:
    """Highest point of a cell inside the square (x +- rad, z +- rad)."""
    if info[0] != "ramp":
        return _surface(info, r, c, x, z)
    if info[1] == "x":
        px = x + rad if info[3] > info[2] else x - rad
        return _surface(info, r, c, px, z)
    pz = z + rad if info[3] > info[2] else z - rad
    return _surface(info, r, c, x, pz)


def blocked(x: float, z: float, y: float, rad: float = PLAYER_R) -> bool:
    """Can't stand at (x, z) with the feet at height y: a wall, or a block / stair edge more than STEP_H higher."""
    for r, c in _footprint(x, z, rad):
        info = cell_info(r, c)
        if info[0] == "wall" or _top_in(info, r, c, x, z, rad) > y + STEP_H:
            return True
    return False


def ground_at(x: float, z: float, y: float, rad: float = PLAYER_R * 0.6) -> float:
    """What a player at (x, z) with the feet at y stands on: the highest top under its footprint it can step on."""
    best = -INF
    for r, c in _footprint(x, z, rad):
        info = cell_info(r, c)
        if info[0] == "wall":
            continue
        top = _top_in(info, r, c, x, z, rad)
        if top <= y + STEP_H and top > best:
            best = top
    return 0.0 if best == -INF else best


def collides(x: float, z: float, r: float = PLAYER_R, y: Optional[float] = None) -> bool:
    """Walls only (y None), or walls and everything more than a step above feet height y."""
    if y is not None:
        return blocked(x, z, y, r)
    c0, c1 = int(math.floor((x - r) / CELL)), int(math.floor((x + r) / CELL))
    r0, r1 = int(math.floor((z - r) / CELL)), int(math.floor((z + r) / CELL))
    return any(solid_cell(rr, cc) for rr in range(r0, r1 + 1) for cc in range(c0, c1 + 1))


def _segment_clear(x0, z0, x1, z1, r, y=None) -> bool:
    d = math.hypot(x1 - x0, z1 - z0)
    n = max(1, int(d / 0.2))
    return all(not collides(x0 + (x1 - x0) * k / n, z0 + (z1 - z0) * k / n, r, y) for k in range(n + 1))


def path_clear(x0, z0, x1, z1, r=PLAYER_R * 0.6, y=None) -> bool:
    """Could a player get from (x0, z0) to (x1, z1) without passing through a wall (or, with feet height y,
    a block it can't climb)? Players slide along walls (x and z move separately), so a straight line or either
    L-shaped path counts."""
    return (_segment_clear(x0, z0, x1, z1, r, y) or
            (_segment_clear(x0, z0, x1, z0, r, y) and _segment_clear(x1, z0, x1, z1, r, y)) or
            (_segment_clear(x0, z0, x0, z1, r, y) and _segment_clear(x0, z1, x1, z1, r, y)))


def look_dir(yaw: float, pitch: float) -> Tuple[float, float, float]:
    """yaw 0 looks toward -z, + yaw turns right; + pitch looks up."""
    cp = math.cos(pitch)
    return math.sin(yaw) * cp, math.sin(pitch), -math.cos(yaw) * cp


def dir_angles(d) -> Tuple[float, float]:
    """look_dir inverse: (yaw, pitch) of a unit vector."""
    return math.atan2(d[0], -d[2]) % (2 * math.pi), math.asin(max(-1.0, min(1.0, d[1])))


def wrap_angle(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


def seq_newer(a: int, b: int) -> bool:
    """True if u16 sequence number a is newer than b."""
    return 0 < ((a - b) & 0xFFFF) < 0x8000


# ------------------------------------------------------------------ ray casting (exact, grid DDA through the heights)
def _cell_hit(info, r, c, ox, oy, oz, dx, dy, dz, t0, t1):
    """Where the ray hits a wall / block / stairs inside one cell between t0 and t1, or None."""
    if info[0] == "ramp":
        h0, h1 = info[2], info[3]
        if info[1] == "x":
            a0, oa, da = c * CELL, ox, dx
        else:
            a0, oa, da = r * CELL, oz, dz

        def f(t):
            return oy + dy * t - (h0 + (h1 - h0) * (oa + da * t - a0) / CELL)
        f0, f1 = f(t0), f(t1)
        if f0 < 0:
            return "wall", t0
        if f1 < 0:
            return "floor", t0 + f0 / (f0 - f1) * (t1 - t0)
        return None
    top = WALL_H if info[0] == "wall" else info[1]
    if top <= 0:
        return None
    if oy + dy * t0 < top:
        return "wall", t0
    if dy < 0 and oy + dy * t1 < top:
        return ("wall" if info[0] == "wall" else "floor"), (top - oy) / dy
    return None


def wall_hit(ox, oy, oz, d, max_dist=80.0):
    """First wall, block, stair or floor along the ray. Returns (kind, t) with kind 'wall'|'floor'|'none'."""
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
        t_exit = min(tmx, tmz, limit)
        info = cell_info(cz, cx)
        if not (info[0] == "flat" and info[1] <= 0):
            hit = _cell_hit(info, cz, cx, ox, oy, oz, dx, dy, dz, t, t_exit)
            if hit is not None:
                return hit
        if tmx < tmz:
            t, tmx, cx = tmx, tmx + tdx, cx + sx
        else:
            t, tmz, cz = tmz, tmz + tdz, cz + sz
    if t_floor <= max_dist:
        return "floor", t_floor
    return "none", max_dist


def hit_player(o, d, pos) -> Tuple[float, str]:
    """Distance along the ray to a player's hitbox (head sphere / body cylinder) and which part."""
    ox, oy, oz = o
    dx, dy, dz = d
    tx, ty, tz = pos
    best, part = INF, ""
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
    for yc in (ty + BODY_TOP, ty):
        if abs(dy) > 1e-12:
            t = (yc - oy) / dy
            if 0 <= t < best:
                qx, qz = px + dx * t, pz + dz * t
                if qx * qx + qz * qz < BODY_R * BODY_R:
                    best, part = t, "body"
    return best, part


def raycast(ox, oy, oz, d, max_dist=80.0, targets=(), weapon=W_RIFLE):
    """targets: iterable of (key, (x, y_feet, z)). Returns (kind, dist, damage, key):
    kind 'head'|'body'|'wall'|'floor'|'none'; key = the hit target's key or None.
    The weapon sets the damage and the range (the knife reaches 2.3 m and also hits within KNIFE_CONE)."""
    w = WEAPONS[weapon]
    max_dist = min(max_dist, w["range"])
    kind, dist = wall_hit(ox, oy, oz, d, max_dist)
    hit_key, dmg = None, 0
    for key, pos in targets:
        t, part = hit_player((ox, oy, oz), d, pos)
        if t < dist:
            kind, dist, hit_key = part, t, key
            dmg = w["head"] if part == "head" else w["body"]
    if hit_key is None and weapon == W_KNIFE:
        best = None
        for key, pos in targets:
            chest = (pos[0], pos[1] + 1.1, pos[2])
            v = (chest[0] - ox, chest[1] - oy, chest[2] - oz)
            L = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
            if L < 1e-6 or L > w["range"] + BODY_R:
                continue
            ang = math.acos(max(-1.0, min(1.0, (v[0] * d[0] + v[1] * d[1] + v[2] * d[2]) / L)))
            if ang <= KNIFE_CONE and line_of_sight((ox, oy, oz), chest) and (best is None or L < best[0]):
                best = (L, key)
        if best is not None:
            kind, dist, dmg, hit_key = "body", best[0], w["body"], best[1]
    return kind, dist, dmg, hit_key


def line_of_sight(a, b) -> bool:
    dist = math.dist(a, b)
    if dist < 1e-6:
        return True
    d = tuple((b[i] - a[i]) / dist for i in range(3))
    kind, t = wall_hit(a[0], a[1], a[2], d, dist)
    return kind == "none" or t >= dist - 0.1


def spawn_point(others, rng) -> Tuple[int, int]:
    """Spawn cell farthest from the given (x, z) positions."""
    best, best_d = SPAWN_CELLS[0], -1.0
    cells = list(SPAWN_CELLS)
    rng.shuffle(cells)
    for cell in cells:
        x, z = cell_center(*cell)
        d = min((math.hypot(x - ox, z - oz) for ox, oz in others), default=99.0)
        if d > best_d:
            best, best_d = cell, d
    return best


class Unwrap:
    """ms mod 65536 -> continuous seconds."""

    def __init__(self):
        self.last = None
        self.base = 0

    def __call__(self, ts: int) -> float:
        if self.last is not None:
            d = (ts - self.last) & 0xFFFF
            if d >= 0x8000:                          # an older packet: don't move the base
                return (self.base + self.last - (0x10000 - d)) / 1000.0
            if ts < self.last:
                self.base += 0x10000
        self.last = ts
        return (self.base + ts) / 1000.0


# ------------------------------------------------------------------ the local fighter (prediction)
class Fighter:
    def __init__(self, pid: int, name: str = ""):
        self.pid = pid
        self.name = name
        self.x = self.z = self.y = self.vy = 0.0
        self.on_ground = True
        self.yaw = self.pitch = 0.0
        self.hp = 0
        self.weapon = W_RIFLE
        self.mags = {w: v["mag"] for w, v in WEAPONS.items()}
        self.reload_t = self.fire_cd = self.dead_t = 0.0
        self.switch_t = 0.0
        self.scoped = False
        self.trigger = False              # fire button held last frame (sniper: one shot per click)
        self.medkits = MEDKIT_START
        self.heal_t = 0.0                 # s of healing left (shown on the HUD)
        self.heal_cd = 0.0                # s until H works again
        self.life = 0
        self.kills = self.deaths = 0

    # ammo of the weapon in hand (the knife has none: always 1)
    @property
    def ammo(self) -> int:
        return self.mags[self.weapon] if WEAPONS[self.weapon]["mag"] else 1

    @ammo.setter
    def ammo(self, v):
        if WEAPONS[self.weapon]["mag"]:
            self.mags[self.weapon] = v

    @property
    def mag_size(self) -> int:
        return WEAPONS[self.weapon]["mag"]

    def respawn_at(self, x, z, y=None):
        self.x, self.z, self.vy = x, z, 0.0
        self.y = floor_at(x, z) if y is None else y
        if self.y == INF:
            self.y = 0.0
        self.on_ground = True
        cx, cz = COLS * CELL / 2, ROWS * CELL / 2           # face the middle
        self.yaw = math.atan2(cx - self.x, -(cz - self.z)) % (2 * math.pi)
        self.pitch = 0.0
        self.hp = MAX_HP
        self.mags = {w: v["mag"] for w, v in WEAPONS.items()}
        self.reload_t = self.fire_cd = self.dead_t = self.switch_t = 0.0
        self.heal_t = self.heal_cd = 0.0
        self.medkits = MEDKIT_START
        self.scoped = False

    @property
    def alive(self) -> bool:
        return self.hp > 0

    @property
    def reloading(self) -> bool:
        return self.reload_t > 0

    @property
    def healing(self) -> bool:
        return self.heal_t > 0

    def eye(self):
        return self.x, self.y + EYE_H, self.z

    def update(self, dt, fwd=0.0, strafe=0.0, sprint=False, jump=False):
        if not self.alive:
            self.dead_t = max(0.0, self.dead_t - dt)
            self.heal_t = 0.0
            return
        self.fire_cd = max(0.0, self.fire_cd - dt)
        self.switch_t = max(0.0, self.switch_t - dt)
        self.heal_t = max(0.0, self.heal_t - dt)
        self.heal_cd = max(0.0, self.heal_cd - dt)
        if self.reload_t > 0:
            self.reload_t -= dt
            if self.reload_t <= 0:
                self.reload_t = 0.0
                self.ammo = self.mag_size
        if self.weapon != W_SNIPER or self.reloading or self.switch_t > 0:
            self.scoped = False
        mag = math.hypot(fwd, strafe)
        if mag > 1:
            fwd, strafe = fwd / mag, strafe / mag
        speed = WALK * (SPRINT if sprint and fwd > 0 and not self.scoped else 1.0) * WEAPONS[self.weapon]["speed"]
        if self.scoped:
            speed *= SCOPE_SPEED
        if self.healing:
            speed *= HEAL_SPEED
        fx, fz = math.sin(self.yaw), -math.cos(self.yaw)
        rx, rz = math.cos(self.yaw), math.sin(self.yaw)
        mx = (fx * fwd + rx * strafe) * speed * dt
        mz = (fz * fwd + rz * strafe) * speed * dt
        if not blocked(self.x + mx, self.z, self.y):
            self.x += mx
        if not blocked(self.x, self.z + mz, self.y):
            self.z += mz
        g = ground_at(self.x, self.z, self.y)
        if jump and self.on_ground:
            self.vy = JUMP_V
            self.on_ground = False
        self.vy -= GRAVITY * dt
        y = self.y + self.vy * dt
        if y <= g:                                    # landed, or walked up a step / the stairs
            y, self.vy, self.on_ground = g, 0.0, True
        elif self.on_ground and self.vy <= 0 and y - g < STEP_H:
            y, self.vy = g, 0.0                       # walking down stairs: stay on them
        else:
            self.on_ground = False
        self.y = y

    def look(self, dyaw, dpitch):
        self.yaw = (self.yaw + dyaw) % (2 * math.pi)
        self.pitch = max(-1.55, min(1.55, self.pitch + dpitch))

    def switch_weapon(self, w: int) -> bool:
        if w not in WEAPONS or w == self.weapon or not self.alive:
            return False
        self.weapon = w
        self.switch_t = SWITCH_TIME
        self.reload_t = 0.0
        self.scoped = False
        return True

    def start_reload(self) -> bool:
        if self.alive and self.mag_size and not self.reloading and self.ammo < self.mag_size and self.switch_t <= 0:
            self.reload_t = WEAPONS[self.weapon]["reload"]
            self.scoped = False
            return True
        return False

    def can_heal(self) -> bool:
        return self.alive and self.medkits > 0 and self.heal_cd <= 0 and not self.healing and self.hp < MAX_HP

    def start_heal(self) -> bool:
        if not self.can_heal():
            return False
        self.medkits -= 1
        self.heal_t = HEAL_TIME
        self.heal_cd = HEAL_COOLDOWN
        return True

    def try_fire(self, pressed: bool = True) -> bool:
        """pressed: the fire button went down this frame (the sniper needs one click per shot)."""
        w = WEAPONS[self.weapon]
        if not self.alive or self.reloading or self.fire_cd > 0 or self.switch_t > 0:
            return False
        if not w["auto"] and not pressed:
            return False
        if w["mag"]:
            if self.ammo <= 0:
                return False
            self.ammo -= 1
        self.fire_cd = w["interval"]
        self.heal_t = 0.0                              # firing stops healing
        return True


# ------------------------------------------------------------------ links
class RadioLink:
    """Game <-> the v6.8 flowgraph's 'Game Link v2' block over UDP on this PC.

    TX to 127.0.0.1:tx_port (52001): datagram = dest node 1 B | game payload   (dest 255 = everyone)
    RX on 127.0.0.1:rx_port (52002): datagram = src node 1 B | dest node 1 B | game payload
    The block adds the 2-byte C5 12 tag on air and removes it on receive."""

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
            if len(d) >= 2 + 6 and d[3] == d[0] and d[0] != self.my_node:
                out.append((d[0], d[2:]))
            else:
                self.ignored += 1

    def close(self):
        for s in (self.sock, self.out):
            try:
                s.close()
            except OSError:
                pass


class BridgeLink(RadioLink):
    """Game <-> the v6.9 cdp_link flowgraph's App Socket Bridge (the stack is used unchanged).

    Datagrams (default, CDP_GAME_ARQ=0): to 127.0.0.1:CDP_UDP_PORT (5200)      dest 1 B | game payload
                                         from 127.0.0.1:CDP_UDP_OUT_PORT (5201) src 1 B | dest 1 B | game payload
      On air the bridge adds its 11-byte 'U' header; unicast datagrams go without ACK, dest 255 = everyone.
    Reliable inputs (CDP_GAME_ARQ=1): TCP 127.0.0.1:CDP_ARQ_PORT (5100), the same socket protocol as cdp_sock.py:
      SEND 01 | ref 4 B | dest 1 B | payload  ->  bridge;  RECV 81 | src 1 B | payload  <-  bridge (to every client).
      The chat ignores these messages: a game payload starts with type byte 4..6, which is never a chat message.
      If the TCP port is not reachable the input goes as a datagram instead."""

    unicast_ok = True
    frame_budget = FRAME_BUDGET_V69          # the bridge header is 11 B instead of the v6.8 tag's 2 B

    def __init__(self, my_node: int, udp_port=5200, udp_out_port=5201, arq_port=5100, host="127.0.0.1"):
        super().__init__(my_node, udp_port, udp_out_port, host)
        self.arq_addr = (host, int(arq_port))
        self.tcp: Optional[socket.socket] = None
        self.tcp_buf = bytearray()
        self.tcp_retry_at = 0.0
        self.ref = random.getrandbits(30)
        self.rel = collections.Counter()      # sent / sent_ok / failed / recv / other / fallback / connects
        self.listen_reliable = False          # read the reliable port: set when this node or a peer has ARQ on
        self.desc = f"radio (v6.9 bridge, node {self.my_node}, udp {udp_port}/{udp_out_port})"

    # -------------------------------------------------- reliable path (only used when the game's ARQ is on)
    def _connect(self):
        import time as _t
        now = _t.monotonic()
        if self.tcp is not None or now < self.tcp_retry_at:
            return self.tcp is not None
        self.tcp_retry_at = now + 2.0
        try:
            s = socket.create_connection(self.arq_addr, timeout=0.3)
            s.setblocking(False)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            return False
        self.tcp, self.tcp_buf = s, bytearray()
        self.rel["connects"] += 1
        return True

    def _drop_tcp(self):
        if self.tcp is not None:
            try:
                self.tcp.close()
            except OSError:
                pass
        self.tcp = None

    def send_reliable(self, data: bytes, dest: int):
        self.listen_reliable = True
        if dest == BCAST or not self._connect():
            self.rel["fallback"] += 1
            return self.send(data, dest)
        self.ref = (self.ref + 1) & 0xFFFFFFFF
        body = bytes([0x01]) + self.ref.to_bytes(4, "big") + bytes([dest & 0xFF]) + bytes(data)
        try:
            self.tcp.sendall(len(body).to_bytes(4, "big") + body)
            self.rel["sent"] += 1
        except (BlockingIOError, InterruptedError):
            self.rel["fallback"] += 1                      # socket buffer full: do not block the game
            self.send(data, dest)
        except OSError:
            self._drop_tcp()
            self.rel["fallback"] += 1
            self.send(data, dest)

    def _poll_tcp(self, out):
        if self.tcp is None:
            return
        while True:
            try:
                chunk = self.tcp.recv(65536)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                self._drop_tcp()
                return
            if not chunk:
                self._drop_tcp()
                return
            self.tcp_buf += chunk
        b = self.tcp_buf
        while len(b) >= 4:
            n = int.from_bytes(b[:4], "big")
            if len(b) < 4 + n:
                break
            body = bytes(b[4:4 + n])
            del b[:4 + n]
            if not body:
                continue
            if body[0] == 0x81 and len(body) >= 2 + 6:
                src, data = body[1], body[2:]
                if data[0] in GAME_TYPES and data[1] == src and src != self.my_node:
                    out.append((src, data))
                    self.rel["recv"] += 1
                else:
                    self.rel["other"] += 1                 # chat or another app's message: not for the game
            elif body[0] == 0x82:
                self.rel["sent_ok"] += 1
            elif body[0] == 0x83:
                self.rel["failed"] += 1

    def poll(self) -> List[Tuple[int, bytes]]:
        out = super().poll()
        if self.listen_reliable:   # a host also listens when only a client has CDP_GAME_ARQ=1 (its beacon says so)
            self._connect()
            self._poll_tcp(out)
        return out

    def close(self):
        self._drop_tcp()
        super().close()


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


# ------------------------------------------------------------------ what this node knows about another node
class Peer:
    def __init__(self, pid: int):
        self.pid = pid
        self.name = f"P{pid}"
        self.last_rx = -INF
        self.last_seq: Optional[int] = None
        self.rx_times: deque = deque(maxlen=40)
        self.flags = 0
        self.host = NONE_ID
        self.epoch = 0
        self.rate = DEFAULT_RATE
        self.max_players = DEFAULT_MAX_PLAYERS
        self.max_lag = DEFAULT_MAX_LAG
        self.rtt: Optional[float] = None               # my RTT to this node (s, smoothed)
        self.reported: Dict[int, float] = {}           # its RTTs to other nodes, as in its last beacon
        self.hears_me_t = -INF
        self.not_hears_me_t = -INF
        self.beacon_t = -INF
        self.hosting_t = -INF                          # last time it acted as host (snapshot or beacon)
        self.snap_max_players = DEFAULT_MAX_PLAYERS
        self.snap_ids: List[int] = []
        self.snap_full = False
        self.leaving = False
        self.vote: Optional[int] = None                # its current host vote while electing
        self.vote_t = -INF

    def connected(self, now) -> bool:
        return not self.leaving and now - self.last_rx < PEER_TIMEOUT

    def pkt_rate(self, now) -> float:
        return sum(1 for t in self.rx_times if now - t < 2.0) / 2.0

    @property
    def ready(self):
        return bool(self.flags & B_READY)

    @property
    def in_arena(self):
        return bool(self.flags & B_IN_ARENA)


# ------------------------------------------------------------------ host side
class HostPlayer:
    def __init__(self, pid: int, name: str, now: float):
        self.pid = pid
        self.name = name
        self.x = self.y = self.z = 0.0
        self.yaw = self.pitch = 0.0
        self.hp = MAX_HP
        self.life = 0
        self.dead_t = 0.0
        self.death_t = -INF
        self.kills = self.deaths = 0
        self.weapon = W_RIFLE
        self.mags = {w: v["mag"] for w, v in WEAPONS.items()}
        self.reload_until = -INF
        self.last_fire_ct = -INF
        self.last_fire_w = {w: -INF for w in WEAPONS}       # client time of the last shot per weapon
        self.medkits = MEDKIT_START
        self.heal_until = -INF
        self.heal_left = 0
        self.heal_acc = 0.0
        self.heal_cd_until = -INF
        self.fired = False
        self.corrected = False
        self.hist: deque = deque(maxlen=int(HISTORY_S * 70))   # (host t, x, y, z, alive, life)
        self.track: deque = deque(maxlen=120)                  # (client t, x, y, z): shooter origins
        self.unwrap = Unwrap()
        self.off_c: Optional[float] = None                     # host time - client time (fastest input)
        self.off_t = now
        self.last_input_seq: Optional[int] = None
        self.last_input_rx = now
        self.last_ct: Optional[float] = None
        self.input_lag = 0.0
        self.seen_aids: deque = deque(maxlen=128)
        self.rtt: Optional[float] = None

    @property
    def alive(self):
        return self.hp > 0

    def healing(self, now) -> bool:
        return now < self.heal_until

    def pos_at(self, t: float):
        """(x, y, z, alive, life) at host time t from the rewind buffer."""
        h = self.hist
        if not h:
            return self.x, self.y, self.z, self.alive, self.life
        if t >= h[-1][0]:
            return h[-1][1:]
        if t <= h[0][0]:
            return h[0][1:]
        i = bisect.bisect_left([e[0] for e in h], t)
        a, b = h[i - 1], h[i]
        if a[4] != b[4] or a[5] != b[5]:                       # died / respawned in between: no lerp
            return a[1:]
        k = (t - a[0]) / max(1e-9, b[0] - a[0])
        return (a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k, a[3] + (b[3] - a[3]) * k, a[4], a[5])

    def origin_at(self, ct: float):
        """Shooter's position at client time ct (from its own accepted inputs)."""
        tr = list(self.track)
        if not tr:
            return self.x, self.y, self.z
        if ct >= tr[-1][0]:
            return tr[-1][1:]
        if ct <= tr[0][0]:
            return tr[0][1:]
        for a, b in zip(tr, tr[1:]):
            if a[0] <= ct <= b[0]:
                k = (ct - a[0]) / max(1e-9, b[0] - a[0])
                return tuple(a[i] + (b[i] - a[i]) * k for i in (1, 2, 3))
        return tr[-1][1:]


class HostWorld:
    """The only real game world (runs on the elected host)."""

    def __init__(self, node: "Node", epoch: int):
        self.node = node
        self.epoch = epoch
        self.players: Dict[int, HostPlayer] = {}
        self.events: deque = deque()                  # [SnapEvent, sends_left]
        self.eid = 0
        self.snap_times: Dict[int, float] = {}        # snapshot seq -> host time
        self.full_t = -INF
        self.spot_t = [-INF] * len(MEDKIT_SPOTS)         # a fixed spot holds a medkit while now >= its time
        self.drops: Dict[int, list] = {}                  # id -> [x, z, y, expires]
        self.drop_id = 0
        self.stats = dict(moves_ok=0, moves_corrected=0, shots=0, hits=0, kills=0, rej_late=0, rej_unknown_view=0,
                          rej_dead=0, rej_rate=0, rej_reloading=0, lagging_target=0, inputs=0, inputs_late=0,
                          pickups=0, heals=0, heals_refused=0)

    # ---------------------------------------------------------------- players
    def add(self, pid: int, name: str, at=None) -> HostPlayer:
        now = self.node.now
        p = HostPlayer(pid, name, now)
        self.players[pid] = p
        if at is None:
            self.respawn(p, count_life=False)
        else:
            p.x, p.y, p.z = at
        self.record(p, now)
        return p

    def remove(self, pid: int):
        self.players.pop(pid, None)

    def respawn(self, p: HostPlayer, count_life=True):
        others = [(q.x, q.z) for q in self.players.values() if q is not p and q.alive]
        cell = spawn_point(others, self.node.rng)
        p.x, p.z = cell_center(*cell)
        p.y = floor_at(p.x, p.z)
        cx, cz = COLS * CELL / 2, ROWS * CELL / 2
        p.yaw = math.atan2(cx - p.x, -(cz - p.z)) % (2 * math.pi)
        p.pitch = 0.0
        p.hp = MAX_HP
        p.mags = {w: v["mag"] for w, v in WEAPONS.items()}
        p.reload_until = -INF
        p.medkits = MEDKIT_START
        p.heal_until = p.heal_cd_until = -INF
        p.heal_left = 0
        p.dead_t = 0.0
        if count_life:
            p.life = (p.life + 1) & 3
        p.corrected = True
        p.track.clear()
        p.last_ct = None

    def record(self, p: HostPlayer, now: float):
        p.hist.append((now, p.x, p.y, p.z, p.alive, p.life))

    def lagging(self, p: HostPlayer, now: float) -> bool:
        return p.pid != self.node.node and now - p.last_input_rx > self.node.max_lag

    # ---------------------------------------------------------------- per frame
    def tick(self, dt: float, now: float):
        for p in list(self.players.values()):
            if not p.alive:
                p.dead_t -= dt
                p.heal_until = -INF
                if p.dead_t <= 0:
                    self.respawn(p)
            elif p.healing(now) and p.heal_left > 0:
                p.heal_acc += HEAL_AMOUNT / HEAL_TIME * dt
                n = min(int(p.heal_acc), p.heal_left, MAX_HP - p.hp)
                p.heal_acc -= int(p.heal_acc)
                p.hp += n
                p.heal_left -= n
                if p.heal_left <= 0 or p.hp >= MAX_HP:
                    p.heal_until = -INF
            self.record(p, now)
        self._pickups(now)

    # ---------------------------------------------------------------- medkits
    def spot_pos(self, k: int):
        x, z = cell_center(*MEDKIT_SPOTS[k])
        return x, floor_at(x, z), z

    def spots_mask(self, now: float) -> int:
        return sum(1 << k for k in range(len(MEDKIT_SPOTS)) if now >= self.spot_t[k])

    def _pickups(self, now: float):
        for k in [k for k, d in self.drops.items() if now > d[3]]:
            del self.drops[k]
        for p in self.players.values():
            if not p.alive or p.medkits >= MEDKIT_MAX:
                continue
            for k in range(len(MEDKIT_SPOTS)):
                if now >= self.spot_t[k]:
                    x, y, z = self.spot_pos(k)
                    if math.hypot(p.x - x, p.z - z) < PICKUP_R and abs(p.y - y) < 1.2:
                        self.spot_t[k] = now + PICKUP_RESPAWN
                        p.medkits += 1
                        self.stats["pickups"] += 1
                        if p.medkits >= MEDKIT_MAX:
                            break
            for k, d in list(self.drops.items()):
                if p.medkits < MEDKIT_MAX and math.hypot(p.x - d[0], p.z - d[1]) < PICKUP_R and abs(p.y - d[2]) < 1.2:
                    del self.drops[k]
                    p.medkits += 1
                    self.stats["pickups"] += 1

    def drop_medkit(self, x: float, z: float, y: float, now: float):
        if len(self.drops) >= MAX_DROPS:
            del self.drops[min(self.drops, key=lambda k: self.drops[k][3])]
        self.drop_id = (self.drop_id + 1) & 0xFF
        g = floor_at(x, z)
        self.drops[self.drop_id] = [x, z, g if g != INF else y, now + DROP_TTL]

    def start_heal(self, p: HostPlayer, now: float) -> bool:
        if not p.alive or p.medkits <= 0 or now < p.heal_cd_until - 0.3 or p.healing(now) or p.hp >= MAX_HP:
            self.stats["heals_refused"] += 1
            return False
        p.medkits -= 1
        p.heal_until = now + HEAL_TIME
        p.heal_left = HEAL_AMOUNT
        p.heal_acc = 0.0
        p.heal_cd_until = now + HEAL_COOLDOWN
        self.stats["heals"] += 1
        return True

    # ---------------------------------------------------------------- inputs from a client
    def on_input(self, inp: Input, now: float, peer: Optional[Peer]) -> None:
        node = self.node
        p = self.players.get(inp.node)
        if inp.flags & I_LEAVING or not inp.flags & I_IN:
            if p is not None:
                self.remove(inp.node)
                node.msg(f"{p.name} left the arena", 3)
            return
        if p is None:
            if len(self.players) >= node.max_players:
                self.full_t = now
                return
            p = self.add(inp.node, peer.name if peer else f"P{inp.node}")
            node.msg(f"{p.name} joined the arena", 3)
        self.stats["inputs"] += 1
        ct = p.unwrap(inp.ts)
        off = now - ct
        if p.off_c is None:
            p.off_c = off
        else:                                         # fastest input so far, creeping up 0.2 ms/s (clock drift)
            p.off_c = min(p.off_c + 0.0002 * max(0.0, now - p.off_t), off)
        p.off_t = now
        lag = now - (ct + p.off_c)                    # how much later than the fastest input this one came
        newer = p.last_input_seq is None or seq_newer(inp.seq, p.last_input_seq)
        if newer:
            p.last_input_seq = inp.seq
            p.last_input_rx = now
            p.input_lag = lag
            if lag > node.max_lag:
                self.stats["inputs_late"] += 1
            st = self.snap_times.get(inp.snap_ack)    # RTT host <-> client
            if st is not None:
                sample = now - st - inp.snap_hold / 1000.0
                if 0 <= sample < 10:
                    p.rtt = sample if p.rtt is None else 0.8 * p.rtt + 0.2 * sample
                    node._rtt_sample(p.pid, sample)
            self._move(p, inp, ct)
            if inp.weapon in WEAPONS:
                p.weapon = inp.weapon
        for a in inp.actions:                          # actions: deduplicated by aid, accepted in any order
            if a.aid in p.seen_aids:
                continue
            p.seen_aids.append(a.aid)
            w = WEAPONS.get(a.weapon)
            if a.kind == A_RELOAD:
                if p.alive and w and w["mag"] and p.mags[a.weapon] < w["mag"]:
                    t_host = ct + a.dt / 1000.0 + p.off_c
                    p.reload_until = t_host + w["reload"] * 0.9
                    p.mags[a.weapon] = w["mag"]
            elif a.kind == A_FIRE and w:
                self._client_fire(p, inp, a, ct, now)
            elif a.kind == A_HEAL:
                self.start_heal(p, now)

    def _move(self, p: HostPlayer, inp: Input, ct: float):
        if not p.alive or inp.life != p.life or not inp.flags & I_ALIVE:
            return                                   # dead, or the client has not seen its (re)spawn yet
        dt = ct - p.last_ct if p.last_ct is not None else 0.5
        p.last_ct = ct
        x, y, z = inp.x, max(0.0, min(5.0, inp.y)), inp.z
        allowed = WALK * SPRINT * 1.3 * max(0.0, dt) + 0.6
        d = math.hypot(x - p.x, z - p.z)
        g = ground_at(x, z, y + 0.1, PLAYER_R * 0.5)
        y_ok = g - 0.3 <= y <= max(g + JUMP_APEX + 0.4, p.y + 0.4)    # not in the floor, not flying higher
        ytop = max(p.y, y) + 0.1
        if d <= allowed and y_ok and not collides(x, z, PLAYER_R * 0.9, ytop) and \
                path_clear(p.x, p.z, x, z, y=ytop):
            p.x, p.y, p.z = x, y, z
            self.stats["moves_ok"] += 1
        else:
            p.corrected = True
            self.stats["moves_corrected"] += 1
        p.yaw, p.pitch = inp.yaw, inp.pitch
        p.track.append((ct, p.x, p.y, p.z))

    def _client_fire(self, p: HostPlayer, inp: Input, a: Action, ct: float, now: float):
        node = self.node
        t_act = ct + a.dt / 1000.0                    # client clock
        t_fire = t_act + p.off_c                      # ~ host clock
        # the shooter's screen: snapshot view_seq, or between it and view_seq + view_next
        ta = self.snap_times.get(a.view_seq)
        tb = self.snap_times.get((a.view_seq + a.view_next) & 0xFFFF) if a.view_next else None
        view = None
        if ta is not None and a.view_next and tb is not None and tb > ta:
            k = a.view_frac / 255.0
            view = (ta, tb, k)
            tv = ta + (tb - ta) * k
        elif ta is not None and not a.view_next:              # the client was extrapolating past ta
            tv = ta + a.view_frac * 0.004
            view = (ta, None, a.view_frac * 0.004)
        else:
            tv = None
        why = self._check_fire(p, t_act, t_fire, tv, now, inp.life, a.weapon)
        res = None
        if why is None:
            o = p.origin_at(t_act)
            if a.dx is not None and abs(inp.x - p.x) < 0.05 and abs(inp.z - p.z) < 0.05:
                ox, oz = inp.x + a.dx, inp.z + a.dz           # where the shooter really stood
                if math.hypot(ox - o[0], oz - o[2]) < 2.0 and not collides(ox, oz, PLAYER_R * 0.5):
                    o = (ox, o[1], oz)
            res = self.fire(p, (o[0], o[1] + EYE_H, o[2]), look_dir(a.yaw, a.pitch), tv, now, view, a.weapon)
        if node.on_shot:
            node.on_shot(node, p.pid, a.aid, why, res, tv, now)

    def _check_fire(self, p, t_act, t_fire, tv, now, life, weapon=W_RIFLE):
        st = self.stats
        st["shots"] += 1
        if tv is None:
            st["rej_unknown_view"] += 1
            return "unknown_view"
        if now - tv > self.node.max_lag:
            st["rej_late"] += 1
            return "late"
        if life != p.life or (not p.alive and p.death_t < t_fire - 0.05):
            st["rej_dead"] += 1
            return "dead"
        w = WEAPONS[weapon]
        if t_act - p.last_fire_w[weapon] < w["interval"] * 0.7:
            st["rej_rate"] += 1
            return "rate"
        if w["mag"] and t_fire < p.reload_until - 0.15:
            st["rej_reloading"] += 1
            return "reloading"
        p.last_fire_ct = p.last_fire_w[weapon] = t_act
        if w["mag"]:
            p.mags[weapon] -= 1
            if p.mags[weapon] <= 0:
                p.reload_until = t_fire + w["reload"] * 0.9
                p.mags[weapon] = w["mag"]
        p.heal_until = -INF                            # firing stops healing
        p.fired = True
        return None

    def fire(self, p: HostPlayer, origin, d, tv: float, now: float, view=None, weapon=W_RIFLE):
        """Rewind the others to what the shooter saw - host time tv, or exactly the interpolation between
        two snapshots view = (t_a, t_b, k) - ray-cast, and apply the result now.
        Returns (kind, victim pid or None, damage, {pid: rewound position})."""
        targets, rewound = [], {}
        for q in self.players.values():
            if q is p or not q.alive:
                continue
            if view is not None and view[1] is not None:      # between two snapshots, as the client drew it
                ta, tb, k = view
                xa, ya, za, aa, la = q.pos_at(ta)
                xb, yb, zb, ab, lb = q.pos_at(tb)
                if not (aa and ab) or la != lb:
                    continue
                x, y, z, alive, life = xa + (xb - xa) * k, ya + (yb - ya) * k, za + (zb - za) * k, True, la
            elif view is not None:                             # past snapshot ta, extrapolated like the client
                x, y, z, alive, life = self._extrapolated(q, view[0], view[2])
            else:
                x, y, z, alive, life = q.pos_at(tv)
            if not alive or life != q.life:
                continue
            if self.lagging(q, now):
                continue                            # its own inputs stopped: frozen ghost, not hittable
            targets.append((q.pid, (x, y, z)))
            rewound[q.pid] = (x, y, z)
        kind, dist, dmg, vid = raycast(origin[0], origin[1], origin[2], d, targets=targets, weapon=weapon)
        if vid is None:
            for q in self.players.values():         # count shots that only missed because of a ghost
                if q is not p and q.alive and self.lagging(q, now):
                    t, _ = hit_player(origin, d, (q.x, q.y, q.z))
                    if t < dist:
                        self.stats["lagging_target"] += 1
                        break
            return kind, None, 0, rewound
        v = self.players[vid]
        self.stats["hits"] += 1
        v.hp = max(0, v.hp - dmg)
        kill = v.hp == 0
        if kill:
            v.deaths = (v.deaths + 1) & 0xFF
            v.dead_t = RESPAWN_TIME
            v.death_t = now
            v.heal_until = -INF
            p.kills = (p.kills + 1) & 0xFF
            self.stats["kills"] += 1
            self.drop_medkit(v.x, v.z, v.y, now)
        self.eid = (self.eid + 1) & 0xFF
        ev = SnapEvent(self.eid, p.pid, vid, dmg, kind == "head", kill, weapon)
        self.events.append([ev, 3])
        self.node._on_event(ev)
        return kind, vid, dmg, rewound

    def _extrapolated(self, q: HostPlayer, ta: float, ahead: float):
        """The client's extrapolation (Node._interp): from snapshot ta on with the velocity between the
        snapshot before it and ta, for at most EXTRAP_MAX s, never into a wall."""
        x, y, z, alive, life = q.pos_at(ta)
        prev = [t for t in self.snap_times.values() if t < ta]
        ahead = min(ahead, EXTRAP_MAX)
        if prev and alive and ahead > 0:
            tp = max(prev)
            xp, yp, zp, ap, lp = q.pos_at(tp)
            if ap and lp == life and 0 < ta - tp < 1.5:
                nx, nz = x + (x - xp) / max(0.02, ta - tp) * ahead, z + (z - zp) / max(0.02, ta - tp) * ahead
                if not collides(nx, nz):
                    x, z = nx, nz
        return x, y, z, alive, life

    # ---------------------------------------------------------------- snapshot
    def snapshot(self, seq: int, ts: int, now: float, closing=False) -> Snapshot:
        node = self.node
        self.snap_times[seq] = now
        if len(self.snap_times) > 512:
            for k in sorted(self.snap_times, key=lambda s: self.snap_times[s])[:128]:
                del self.snap_times[k]
        ps = sorted(self.players.values(), key=lambda q: q.pid)
        out = []
        for q in ps:
            fl = (P_DEAD if not q.alive else 0) | (P_FIRED if q.fired else 0) | (q.life << 3)
            fl |= P_RELOADING if now < q.reload_until else 0
            fl |= P_CORRECTED if q.corrected else 0
            fl |= P_HOST if q.pid == node.node else 0
            hold = int(max(0.0, now - q.last_input_rx) * 1000)
            gear = (q.weapon & 3) | (G_HEALING if q.healing(now) else 0) | (min(3, q.medkits) << 3)
            out.append(SnapPlayer(q.pid, q.x, q.y, q.z, q.yaw, q.pitch, q.hp, fl, q.kills, q.deaths,
                                  (q.last_input_seq or 0) & 0xFF, hold, gear))
            q.fired = False
            q.corrected = False
        drops = [Drop(k, d[0], d[1]) for k, d in sorted(self.drops.items())][:MAX_DROPS]
        room = max(1, (getattr(node.link, "frame_budget", FRAME_BUDGET) - 13 - 14 * len(out) - 3 * len(drops)) // 5)
        evs = []
        for e in list(self.events)[:min(room, MAX_EVENTS)]:
            evs.append(e[0])
            e[1] -= 1
        self.events = deque(e for e in self.events if e[1] > 0)
        fl = (S_CLOSING if closing else 0) | (S_FULL if now - self.full_t < 2.0 or len(ps) >= node.max_players else 0)
        return Snapshot(node.node, seq, ts, self.epoch, fl, node.max_players, out, evs, self.spots_mask(now), drops)


# ------------------------------------------------------------------ other players as this node draws them
class RemoteView:
    def __init__(self, pid: int):
        self.pid = pid
        self.name = f"P{pid}"
        self.x = self.y = self.z = 0.0
        self.yaw = self.pitch = 0.0
        self.hp = MAX_HP
        self.alive = False
        self.life = 0
        self.kills = self.deaths = 0
        self.tracer_t = 0.0
        self.reloading = False
        self.lagging = False
        self.is_host = False
        self.hit_flash = 0.0
        self.placed = False
        self.weapon = W_RIFLE
        self.healing = False


# ------------------------------------------------------------------ one node: lobby, election, host or client
class Node:
    """mode 'lobby' or 'arena'; role None / 'electing' / 'host' / 'client' while in the arena."""

    def __init__(self, node: int, link, name: str = "", rate=DEFAULT_RATE, max_players=DEFAULT_MAX_PLAYERS,
                 max_lag=DEFAULT_MAX_LAG, arq=True, elect_random=False, clock_offset=0.0, seed=None,
                 election_window=ELECTION_WINDOW, adaptive=True):
        self.node = int(node)
        self.link = link
        self.me = Fighter(self.node, (name or f"P{node}")[:8])
        self.rate = max(MIN_RATE, min(MAX_RATE, float(rate)))
        self.max_players = max(2, min(MAX_PLAYERS_LIMIT, int(max_players)))
        self.max_lag = float(max_lag)
        self.arq = bool(arq)
        self.elect_random = bool(elect_random)
        self.election_window = election_window
        self.adaptive = bool(adaptive)
        if self.arq and hasattr(link, "listen_reliable"):
            link.listen_reliable = True
        self.rate_eff = self.rate                       # packet rate actually used (congestion control)
        self.cc_t = 0.0
        self.cc_queue = 0.0
        self.rtt_hist: Dict[int, deque] = {}
        self.queueing: deque = deque()
        self.clock_offset = clock_offset
        self.rng = random.Random(seed if seed is not None else node)
        self.now = 0.0
        self._dt = 0.0
        self.mode = "lobby"
        self.role: Optional[str] = None
        self.peers: Dict[int, Peer] = {}
        self.seq = 0
        self.sent_hist: List[Optional[Tuple[int, float]]] = [None] * SEQ_RING
        self.my_reported: Dict[int, float] = {}         # RTTs as sent in my last beacon
        self.beacon_t = 0.0
        self.send_t = 0.0
        # election / host / client state
        self.epoch_seen = 0
        self.elect_t0 = 0.0
        self.elect_window = election_window
        self.elect_round = 0
        self.excluded: set = set()
        self.expect_host: Optional[int] = None
        self.expect_t = 0.0
        self.mig_cands: Optional[List[int]] = None
        self.mig_snap: Optional[Snapshot] = None
        self.host_id: Optional[int] = None
        self.epoch = 0
        self.world: Optional[HostWorld] = None
        self.last_snap: Optional[Snapshot] = None       # newest snapshot of my host (for migration)
        self.last_snap_rx = -INF
        self.join_t = 0.0
        self.admitted = False
        self.snaps: deque = deque(maxlen=40)            # (host t, seq, {pid: SnapPlayer})
        self.host_unwrap = Unwrap()
        self.host_off: Optional[float] = None
        self.snap_interval = 1.0 / DEFAULT_RATE
        self.snap_ack: Optional[int] = None
        self.snap_ack_rx = 0.0
        self.views: Dict[int, RemoteView] = {}
        self.pred: Dict[int, Tuple[float, float, float]] = {}
        self.corr_seq = 0
        self.pending: List[dict] = []                   # actions not yet acknowledged by the host
        self.aid = 0
        self.seen_eids: deque = deque(maxlen=64)
        self.rtt_host: Optional[float] = None
        self.last_input_t = -INF
        self.render_t = 0.0
        self.err_vis = [0.0, 0.0, 0.0]                  # visual smoothing of corrections
        # effects / HUD
        self.hitmarker_t = 0.0
        self.confirm_t = 0.0
        self.damage_t = 0.0
        self.tracers: List[list] = []
        self.shots: deque = deque(maxlen=64)
        self.messages: deque = deque(maxlen=5)
        self.kill_feed: deque = deque(maxlen=5)
        self.pickups: List[Tuple[float, float, float, str]] = []      # medkits to draw: (x, y, z, 'spot'|'drop')
        self.old_version: set = set()
        self.stats = dict(corrections=0, snaps=0, snaps_late=0, snaps_old=0, inputs_sent=0, beacons=0,
                          elections=0, migrations=0, hosted=0, shots=0, frames=0, rate_cuts=0)
        self.on_shot = None        # sim hook (host): (node, shooter, aid, reject reason, result, tv, now)
        self.on_fire = None        # sim hook (shooter): (node, aid, origin, dir, {pid: drawn pos}, now)
        self.on_event = None       # sim hook (everyone): (node, SnapEvent)

    # ---------------------------------------------------------------- helpers
    def now_ms(self) -> int:
        return int(math.floor((self.now + self.clock_offset) * 1000.0)) & 0xFFFF

    def msg(self, text, ttl=3.0):
        self.messages.append([text, ttl])

    def _next_seq(self) -> int:
        s = self.seq
        self.seq = (self.seq + 1) & 0xFFFF
        self.sent_hist[s % SEQ_RING] = (s, self.now)
        return s

    def _sent_time_low(self, low: int) -> Optional[Tuple[int, float]]:
        """Newest sent seq whose low byte is `low`, with its send time."""
        s = (self.seq - 1 - ((self.seq - 1 - low) & 0xFF)) & 0xFFFF
        rec = self.sent_hist[s % SEQ_RING]
        return rec if rec and rec[0] == s else None

    def name_of(self, pid: int) -> str:
        if pid == self.node:
            return self.me.name
        p = self.peers.get(pid)
        return p.name if p else f"P{pid}"

    def _peer(self, pid: int) -> Peer:
        p = self.peers.get(pid)
        if p is None:
            p = self.peers[pid] = Peer(pid)
        return p

    # ---------------------------------------------------------------- what the UI calls
    def can_join(self) -> Tuple[bool, str]:
        h = self._active_host()
        if h is not None and h != self.node:
            hp = self.peers[h]
            if hp.snap_full and self.node not in hp.snap_ids:
                return False, f"Arena full ({len(hp.snap_ids)}/{hp.snap_max_players})"
        return True, ""

    def join_game(self) -> Tuple[bool, str]:
        ok, why = self.can_join()
        if not ok:
            return False, why
        self.mode = "arena"
        self.role = None
        self.excluded.clear()
        self.beacon_t = 0.0
        self.mig_cands = self.mig_snap = None
        self.elect_window = self.election_window
        self._decide()
        return True, ""

    def leave_game(self, why: str = ""):
        if self.mode != "arena":
            return
        if self.role == "host":
            self._close_host()
        elif self.role == "client":
            self._send_input(leaving=True)
        self._reset_arena()
        self.mode = "lobby"
        self.beacon_t = 0.0
        if why:
            self.msg(why, 5)

    def shutdown(self):
        if self.role == "host":
            self._close_host()
        elif self.role == "client":
            self._send_input(leaving=True)
        self._send_beacon(leaving=True)

    def start_reload(self):
        if self.me.start_reload():
            self._queue_action(A_RELOAD, 0.0, 0.0)
            if self.role == "host" and self.world is not None:
                w = WEAPONS[self.me.weapon]
                p = self.world.players[self.node]
                p.reload_until = self.now + w["reload"] * 0.9
                p.mags[self.me.weapon] = w["mag"]

    # ---------------------------------------------------------------- election
    @staticmethod
    def host_rank(n_players: int, epoch: int, pid: int):
        """Which arena wins when two hosts meet: more players, then the newer epoch, then the lower node ID
        (so one player who lost the host for a moment and started its own arena can't take over the others)."""
        return (n_players, epoch, -pid)

    def _active_host(self) -> Optional[int]:
        """Best host currently running an arena (me included)."""
        best = None
        if self.role == "host" and self.world is not None:
            best = self.host_rank(len(self.world.players), self.world.epoch, self.node)
        for p in self.peers.values():
            if p.connected(self.now) and self.now - p.hosting_t < PEER_TIMEOUT and p.pid not in self.excluded:
                key = self.host_rank(len(p.snap_ids) or 1, p.epoch, p.pid)
                if best is None or key > best:
                    best = key
        return None if best is None else -best[2]

    def _rtt_between(self, a: int, b: int) -> float:
        """RTT a<->b as both of them last broadcast it (so every node computes the same number)."""
        def reported(x, y):
            if x == self.node:
                return self.my_reported.get(y)
            p = self.peers.get(x)
            return p.reported.get(y) if p else None
        vals = [v for v in (reported(a, b), reported(b, a)) if v is not None]
        return min(vals) if vals else 1.0

    def elect(self, cands: List[int]) -> int:
        cands = sorted(set(cands))
        if len(cands) == 1:
            return cands[0]
        lowest = cands[0]
        if lowest == self.node:
            rnd = self.elect_random
        else:
            rnd = bool(self.peers[lowest].flags & B_ELECT_RANDOM) if lowest in self.peers else self.elect_random
        if rnd:
            return cands[zlib.crc32(bytes(cands) + bytes([self.elect_round & 0xFF])) % len(cands)]

        def score(n):
            s = sum(self._rtt_between(n, m) for m in cands if m != n) / (len(cands) - 1)
            return round(s / 0.02)                    # 20 ms buckets: small differences don't split the vote
        return min(cands, key=lambda n: (score(n), n))

    def _candidates(self) -> List[int]:
        c = [self.node]
        for p in self.peers.values():
            if p.connected(self.now) and p.ready and not p.in_arena and p.pid not in self.excluded:
                c.append(p.pid)
        return c

    def _current_cands(self) -> List[int]:
        if self.mig_cands is not None:                    # the players of my host's last snapshot
            cands = [c for c in self.mig_cands if c == self.node or
                     (c in self.peers and self.now - self.peers[c].last_rx < 10.0 and not self.peers[c].leaving
                      and c not in self.excluded)]
            if self.node not in cands:
                cands.append(self.node)
            return cands
        return self._candidates()

    def _decide(self):
        """Lobby -> arena: join a running host, or start an election."""
        h = self._active_host()
        if h is not None and h != self.node:
            self._become_client(h)
            return
        self.role = "electing"
        self.elect_t0 = self.now
        self.stats["elections"] += 1
        self.expect_host = None
        self.beacon_t = 0.0

    def _elect_step(self):
        h = self._active_host()
        if h is not None and h != self.node:
            self._become_client(h)
            return
        if self.expect_host is not None:
            if self.now - self.expect_t > HOST_WAIT:          # the elected host never showed up
                self.excluded.add(self.expect_host)          # (also skipped in the snapshot roster next time)
                if self.mig_cands:
                    self.mig_cands = [c for c in self.mig_cands if c != self.expect_host]
                self.expect_host = None
                self.elect_t0 = self.now
                self.elect_window = MIGRATION_WINDOW
                self.elect_round += 1
            return
        if self.now - self.elect_t0 < self.elect_window:
            return
        if self.mig_snap is not None:
            # migration: every survivor holds (almost) the same last snapshot, so all of them pick the same
            # host from it without talking: the lowest node ID among the players the old host still heard
            # well (hold < max_lag). The game goes on after ~MIGRATION_WINDOW s even on a busy channel.
            old = self.mig_snap.node
            alive = [sp for sp in self.mig_snap.players if sp.id != old and sp.id not in self.excluded]
            good = [sp.id for sp in alive if sp.hold <= self.max_lag * 1000] or [sp.id for sp in alive]
            win = min(good) if good else self.node
            if win == self.node:
                self._become_host(self.mig_snap)
            else:
                self.expect_host = win
                self.expect_t = self.now
            return
        cands = self._current_cands()
        # every candidate broadcasts its vote while electing; the most votes win (ties: lowest node ID),
        # so nodes whose RTT tables differ a little still end up with the same host
        votes = collections.Counter({self.elect(cands): 1})
        for p in self.peers.values():
            if p.vote is not None and p.vote not in self.excluded and self.now - p.vote_t < 2.0 and \
                    (p.pid in cands or (self.mig_cands is not None and p.pid in self.mig_cands)):
                votes[p.vote] += 1
        top = max(votes.values())
        win = min(c for c, v in votes.items() if v == top)
        if win == self.node:
            self._become_host(self.mig_snap)
        else:
            self.expect_host = win
            self.expect_t = self.now

    def _become_host(self, from_snap: Optional[Snapshot] = None):
        epoch = (max(self.epoch_seen, self.epoch) + 1) & 0xFF
        self.world = w = HostWorld(self, epoch)
        self.role = "host"
        self.host_id = self.node
        self.epoch = self.epoch_seen = epoch
        self.stats["hosted"] += 1
        self.excluded.clear()
        me = self.me
        if from_snap is not None:                       # migration: continue the old world
            for sp in from_snap.players:
                if sp.id == from_snap.node:
                    continue
                if sp.id == self.node:
                    p = w.add(sp.id, me.name, at=(me.x, me.y, me.z))
                else:
                    p = w.add(sp.id, self.name_of(sp.id), at=(sp.x, sp.y, sp.z))
                p.hp, p.kills, p.deaths, p.life = sp.hp, sp.kills, sp.deaths, sp.life
                p.yaw, p.pitch = sp.yaw, sp.pitch
                p.weapon, p.medkits = sp.weapon, sp.medkits
                if p.hp <= 0:
                    p.dead_t = 1.0
                w.record(p, self.now)
            self.msg(f"You are the new host (epoch {epoch})", 4)
        else:
            self.msg("You are the host", 3)
        if self.node not in w.players:
            p = w.add(self.node, me.name)
            me.respawn_at(p.x, p.z, p.y)
            me.life = p.life
        else:
            me.life = w.players[self.node].life
            me.hp = w.players[self.node].hp
        self.mig_cands = self.mig_snap = None
        self.views.clear()
        self.admitted = True
        self.send_t = 0.0
        self.beacon_t = 0.0

    def _become_client(self, host: int):
        if host != self.host_id:
            self.last_snap = None
        self.role = "client"
        self.host_id = host
        self.epoch = self.peers[host].epoch if host in self.peers else 0
        self.world = None
        self.join_t = self.now
        self.admitted = False
        self.snaps.clear()
        self.views.clear()
        self.seen_eids.clear()
        self.host_unwrap = Unwrap()
        self.host_off = None
        self.snap_ack = None
        self.send_t = 0.0
        self.beacon_t = 0.0
        self.expect_host = None
        self.mig_cands = self.mig_snap = None
        self.excluded.clear()

    def _close_host(self):
        if self.world is None:
            return
        for _ in range(2):
            seq = self._next_seq()
            self.link.send(build_payload(self.world.snapshot(seq, self.now_ms(), self.now, closing=True)), BCAST)
        self.world = None

    def _reset_arena(self):
        self.role = None
        self.world = None
        self.host_id = None
        self.admitted = False
        self.snaps.clear()
        self.views.clear()
        self.pending.clear()
        self.me.hp = 0
        self.expect_host = None
        self.mig_cands = self.mig_snap = None

    def _migrate(self, why: str):
        """My host is gone: elect a new one among the players of the newest snapshot."""
        old = self.host_id
        self.stats["migrations"] += 1
        if old is not None:
            self.excluded.add(old)
            if old in self.peers:
                self.peers[old].hosting_t = -INF
        snap = self.last_snap
        self.role = "electing"
        self.world = None
        self.host_id = None
        self.elect_t0 = self.now
        self.elect_window = MIGRATION_WINDOW
        self.elect_round += 1
        self.expect_host = None
        if snap is not None:
            self.mig_cands = [sp.id for sp in snap.players if sp.id != old]
        else:                                             # no snapshot yet: everyone who followed that host
            self.mig_cands = [self.node] + [p.pid for p in self.peers.values()
                                            if p.pid != old and p.host == old and self.now - p.last_rx < 10.0]
        self.mig_snap = snap
        self.beacon_t = self.rng.uniform(0.0, 0.25)      # exchange votes now (not all at the same instant)
        self.msg(f"{why} - choosing a new host", 3)

    # ---------------------------------------------------------------- receiving
    def _receive(self):
        for src, data in self.link.poll():
            try:
                m = parse_payload(data)
            except ValueError:
                if data and data[0] in (1, 2, 3) and src not in self.old_version:
                    self.old_version.add(src)                 # a Radio Arena v3 game: can't play together
                    self.msg(f"node {src} runs an older Radio Arena (v3) - update it to play together", 6)
                continue
            if m.node == self.node or m.node != src:
                continue
            self.stats["frames"] += 1
            p = self._peer(src)
            p.last_rx = self.now
            p.rx_times.append(self.now)
            p.last_seq = m.seq
            if p.leaving and not (m.type == T_BEACON and m.flags & B_LEAVING):
                p.leaving = False
            if m.type == T_BEACON:
                self._on_beacon(p, m)
            elif m.type == T_SNAPSHOT:
                self._on_snapshot(p, m)
            elif m.type == T_INPUT:
                if self.role == "host" and self.world is not None and m.host == self.node:
                    self.world.on_input(m, self.now, p)

    def _on_beacon(self, p: Peer, b: Beacon):
        p.beacon_t = self.now
        p.name = b.name or p.name
        p.flags = b.flags
        p.rate, p.max_players, p.max_lag = b.rate, b.max_players, b.max_lag
        if b.flags & B_ARQ and hasattr(self.link, "listen_reliable"):
            self.link.listen_reliable = True        # v6.9: this peer sends its inputs as reliable messages
        if b.flags & B_LEAVING:
            p.leaving = True
            p.flags = 0
            p.hosting_t = -INF
            return
        if b.flags & B_HOSTING:
            p.hosting_t = self.now
            p.epoch = b.epoch
            self.epoch_seen = max(self.epoch_seen, b.epoch)
        else:
            p.hosting_t = -INF
        p.host = b.host
        if b.flags & B_READY and not b.flags & (B_HOSTING | B_IN_ARENA):
            p.vote, p.vote_t = (None if b.host == NONE_ID else b.host), self.now   # electing: host = its vote
        p.reported = {q.id: q.rtt / 1000.0 for q in b.peers if q.rtt >= 0}
        mine = [q for q in b.peers if q.id == self.node]
        if mine:
            p.hears_me_t = self.now
            q = mine[0]
            rec = self._sent_time_low(q.echo)
            if rec is not None:
                sample = self.now - rec[1] - q.hold / 1000.0
                if -0.02 <= sample < 10.0:
                    sample = max(0.0, sample)
                    p.rtt = sample if p.rtt is None else 0.7 * p.rtt + 0.3 * sample
        elif len(b.peers) < MAX_PEERS:
            p.not_hears_me_t = self.now

    def _on_snapshot(self, p: Peer, s: Snapshot):
        if s.flags & S_CLOSING:
            p.hosting_t = -INF
            if self.role == "client" and self.host_id == p.pid:
                self.last_snap = s if s.players else self.last_snap
                self._migrate(f"{p.name} closed its arena")
            return
        p.hosting_t = self.now
        p.epoch = s.epoch
        p.snap_ids = [sp.id for sp in s.players]
        p.snap_max_players = s.max_players
        p.snap_full = bool(s.flags & S_FULL)
        self.epoch_seen = max(self.epoch_seen, s.epoch)
        if self.role == "electing" and self.mig_snap is not None and p.pid == self.mig_snap.node:
            self.excluded.discard(p.pid)                      # my old host is back: no migration after all
            self.msg(f"{p.name} is back", 2)
            self._become_client(p.pid)
        if self.role == "host" and self.world is not None:   # two hosts: the better one stays
            if self.host_rank(len(s.players), s.epoch, p.pid) > \
                    self.host_rank(len(self.world.players), self.world.epoch, self.node):
                self.msg(f"{p.name} already hosts an arena - joining it", 4)
                self._close_host()
                self._become_client(p.pid)
            else:
                return
        if self.role != "client" or p.pid != self.host_id:
            return                                         # electing: _elect_step() will join it
        self.epoch = s.epoch
        self.stats["snaps"] += 1
        t_host = self.host_unwrap(s.ts)
        if self.snaps and not seq_newer(s.seq, self.snaps[-1][1]):
            self.stats["snaps_old"] += 1
            return
        off = t_host - self.now
        self.host_off = off if self.host_off is None else max(self.host_off - 0.0002 * self._dt, off)
        if (self.now + self.host_off) - t_host > self.max_lag:
            self.stats["snaps_late"] += 1
        if self.snaps:
            gap = t_host - self.snaps[-1][0]
            if 0 < gap < 3.0:
                self.snap_interval = 0.85 * self.snap_interval + 0.15 * gap
        self.snaps.append((t_host, s.seq, {sp.id: sp for sp in s.players}))
        self.last_snap = s
        self.last_snap_rx = self.now
        self.snap_ack, self.snap_ack_rx = s.seq, self.now
        mine = next((sp for sp in s.players if sp.id == self.node), None)
        if mine is None:
            if self.admitted:
                self.leave_game("The host removed you from the arena")
            return
        me = self.me
        if not self.admitted:
            self.admitted = True
            self.msg(f"Joined {p.name}'s arena", 3)
            keep = me.alive and me.life == mine.life and math.hypot(me.x - mine.x, me.z - mine.z) < 4.0
            if not keep:
                me.respawn_at(mine.x, mine.z, mine.y)
            me.life = mine.life
            me.hp = mine.hp
            self.corr_seq = (self.seq - 1) & 0xFFFF
        self._reconcile(mine)
        self.pickups = self._pickups_from(s)
        for e in s.events:
            if e.eid in self.seen_eids:
                continue
            self.seen_eids.append(e.eid)
            self._on_event(e)
        ids = set()
        for sp in s.players:
            if sp.id == self.node:
                continue
            ids.add(sp.id)
            v = self.views.get(sp.id)
            if v is None:
                v = self.views[sp.id] = RemoteView(sp.id)
            v.name = self.name_of(sp.id)
            v.hp, v.kills, v.deaths = sp.hp, sp.kills, sp.deaths
            v.reloading = bool(sp.flags & P_RELOADING)
            v.weapon, v.healing = sp.weapon, sp.healing
            v.is_host = bool(sp.flags & P_HOST)
            v.lagging = sp.hold > self.max_lag * 1000 and not v.is_host
            if sp.flags & P_FIRED:
                v.tracer_t = 0.08
        for pid in list(self.views):
            if pid not in ids:
                del self.views[pid]

    @staticmethod
    def _pickups_from(s: Snapshot):
        out = []
        for k, (r, c) in enumerate(MEDKIT_SPOTS):
            if s.spots >> k & 1:
                x, z = cell_center(r, c)
                out.append((x, floor_at(x, z), z, "spot"))
        for d in s.drops:
            g = floor_at(d.x, d.z)
            out.append((d.x, 0.0 if g == INF else g, d.z, "drop"))
        return out

    def _gear_from_host(self, medkits: int, healing: bool):
        """Medkits and healing as the host has them, unless a heal of mine is still on its way to it."""
        me = self.me
        if any(a["kind"] == A_HEAL for a in self.pending):
            return
        if medkits > me.medkits:
            self.msg(f"+{medkits - me.medkits} medkit  (H to heal)", 2)
        me.medkits = medkits
        if healing and me.heal_t <= 0:
            me.heal_t = 0.5                                # healing on the host (shown until it says it stopped)
        elif not healing and me.heal_t > 0 and HEAL_TIME - me.heal_t > 0.6 + (self.rtt_host or 0.3):
            me.heal_t = 0.0

    def _reconcile(self, mine: SnapPlayer):
        me = self.me
        me.kills, me.deaths = mine.kills, mine.deaths
        rec = self._sent_time_low(mine.ack)
        if rec is not None:                               # RTT to the host
            sample = self.now - rec[1] - mine.hold / 1000.0
            if 0 <= sample < 10:
                self.rtt_host = sample if self.rtt_host is None else 0.8 * self.rtt_host + 0.2 * sample
                self._rtt_sample(self.host_id, sample)
            self.pending = [a for a in self.pending if a["first_seq"] is None or seq_newer(a["first_seq"], rec[0])]
        if mine.life != me.life:                          # the host respawned me
            me.life = mine.life
            me.respawn_at(mine.x, mine.z, mine.y)
            self.pending = [a for a in self.pending if a["kind"] != A_FIRE]
            self.corr_seq = (self.seq - 1) & 0xFFFF
            self.msg("Respawned", 2)
            return
        if mine.flags & P_DEAD:
            if me.alive:
                me.hp = 0
                me.dead_t = RESPAWN_TIME
                me.reload_t = 0.0
            return
        me.hp = mine.hp
        self._gear_from_host(mine.medkits, mine.healing)
        if rec is None or not seq_newer(rec[0], self.corr_seq):
            return                                        # input sent before my last correction
        pred = self.pred.get(rec[0])
        if pred is None:
            return
        err = math.hypot(mine.x - pred[0], mine.z - pred[2])
        if err > CORRECT_EPS or (mine.flags & P_CORRECTED and err > 0.05):
            nx, nz = mine.x + (me.x - pred[0]), mine.z + (me.z - pred[2])
            ny = mine.y + (me.y - pred[1])
            ytop = max(mine.y, ny) + 0.1
            if collides(nx, nz, PLAYER_R, ytop) or not path_clear(mine.x, mine.z, nx, nz, y=ytop):
                nx, nz, ny = mine.x, mine.z, mine.y
            self.err_vis = [self.err_vis[0] + me.x - nx, self.err_vis[1] + me.y - ny, self.err_vis[2] + me.z - nz]
            me.x, me.z = nx, nz
            me.y = max(ny, ground_at(nx, nz, ny + 0.05))
            self.corr_seq = (self.seq - 1) & 0xFFFF
            self.stats["corrections"] += 1

    def _on_event(self, e: SnapEvent):
        if self.on_event:
            self.on_event(self, e)
        shooter, victim = self.name_of(e.shooter), self.name_of(e.victim)
        if e.shooter == self.node:
            self.confirm_t = 0.3 if e.head or e.kill else 0.18
            if e.kill:
                self.msg(f"You eliminated {victim}!", 3)
        if e.victim == self.node:
            self.damage_t = 0.4
            if e.kill:
                self.msg(f"Eliminated by {shooter} - respawn in {RESPAWN_TIME:.0f}s", 4)
        if e.kill:
            self.kill_feed.append(["you" if e.shooter == self.node else shooter,
                                   "you" if e.victim == self.node else victim, e.head, e.weapon, 5.0])
        v = self.views.get(e.victim)
        if v is not None:
            v.hit_flash = 0.12

    # ---------------------------------------------------------------- client: draw the others
    def interp_delay(self) -> float:
        """How far behind the newest host time the others are drawn: a bit more than one snapshot interval
        (gaps are bridged by EXTRAP_MAX of extrapolation). Every 0.1 s here adds 0.1 s to the view age the
        host has to rewind, which must stay under max_lag."""
        return min(0.45, max(0.08, 1.15 * self.snap_interval + 0.03))

    def _interp(self):
        if not self.snaps or self.host_off is None:
            return
        rt = self.now + self.host_off - self.interp_delay()
        self.render_t = rt
        snaps = list(self.snaps)
        for pid, v in self.views.items():
            a = b = None
            for k in range(len(snaps) - 1, -1, -1):
                if pid not in snaps[k][2]:
                    continue
                if snaps[k][0] <= rt:
                    a = snaps[k]
                    break
                b = snaps[k]
            if a is None and b is None:
                continue
            if a is None:
                sp = b[2][pid]
                x, y, z, yaw, pitch = sp.x, sp.y, sp.z, sp.yaw, sp.pitch
            elif b is None:                                   # past the newest: short extrapolation
                sp = a[2][pid]
                x, y, z, yaw, pitch = sp.x, sp.y, sp.z, sp.yaw, sp.pitch
                prev = next((s for s in reversed(snaps) if s[0] < a[0] and pid in s[2]), None)
                ahead = min(rt - a[0], EXTRAP_MAX)
                if prev is not None and ahead > 0 and not sp.flags & P_DEAD and a[0] - prev[0] < 1.5:
                    q = prev[2][pid]
                    if q.life == sp.life:
                        span = max(0.02, a[0] - prev[0])
                        nx, nz = x + (sp.x - q.x) / span * ahead, z + (sp.z - q.z) / span * ahead
                        if not collides(nx, nz):
                            x, z = nx, nz
            else:
                sa, sb = a[2][pid], b[2][pid]
                k = (rt - a[0]) / max(1e-6, b[0] - a[0])
                sp = sa if k < 0.5 else sb
                if sa.life != sb.life or (sa.flags & P_DEAD) != (sb.flags & P_DEAD):
                    x, y, z, yaw, pitch = sp.x, sp.y, sp.z, sp.yaw, sp.pitch
                else:
                    x = sa.x + (sb.x - sa.x) * k
                    y = sa.y + (sb.y - sa.y) * k
                    z = sa.z + (sb.z - sa.z) * k
                    yaw = sa.yaw + wrap_angle(sb.yaw - sa.yaw) * k
                    pitch = sa.pitch + (sb.pitch - sa.pitch) * k
            v.x, v.y, v.z, v.yaw, v.pitch = x, y, z, yaw, pitch
            v.alive = not sp.flags & P_DEAD
            v.life = sp.life
            v.placed = True

    def _view_ref(self) -> Tuple[int, int, int]:
        """What this client's screen shows: (snapshot seq, +seq of the second snapshot, fraction 0..255), or
        (seq, 0, 4 ms steps past it) while extrapolating past the newest snapshot."""
        rt = self.render_t
        snaps = list(self.snaps)
        for i in range(len(snaps) - 1, -1, -1):
            t, seq, _ = snaps[i]
            if t <= rt:
                if i + 1 < len(snaps):
                    t2, seq2, _ = snaps[i + 1]
                    nxt = (seq2 - seq) & 0xFFFF
                    if 1 <= nxt <= 255 and t2 > t:
                        return seq, nxt, max(0, min(255, int(round((rt - t) / (t2 - t) * 255))))
                return seq, 0, max(0, min(255, int(round((rt - t) * 250))))
        return snaps[0][1], 0, 0

    def _host_views(self):
        """On the host the others are drawn at their authoritative position (no interpolation delay)."""
        w = self.world
        k = min(1.0, self._dt * 15.0)
        for pid, q in w.players.items():
            if pid == self.node:
                continue
            v = self.views.get(pid)
            if v is None or not q.alive or v.life != q.life:
                if v is None:
                    v = self.views[pid] = RemoteView(pid)
                v.x, v.y, v.z, v.yaw, v.pitch = q.x, q.y, q.z, q.yaw, q.pitch
            else:
                v.x += (q.x - v.x) * k
                v.y += (q.y - v.y) * k
                v.z += (q.z - v.z) * k
                v.yaw += wrap_angle(q.yaw - v.yaw) * k
                v.pitch += (q.pitch - v.pitch) * k
            v.name = q.name
            v.hp, v.alive, v.life, v.kills, v.deaths = q.hp, q.alive, q.life, q.kills, q.deaths
            v.reloading = self.now < q.reload_until
            v.weapon, v.healing = q.weapon, q.healing(self.now)
            v.lagging = w.lagging(q, self.now)
            v.placed = True
            if q.fired:
                v.tracer_t = 0.08
        for pid in list(self.views):
            if pid not in w.players:
                del self.views[pid]

    # ---------------------------------------------------------------- shooting
    def fire(self, pressed: bool = True):
        me = self.me
        weapon = me.weapon
        if not me.try_fire(pressed):
            return
        ex, ey, ez = me.eye()
        d = look_dir(me.yaw, me.pitch)
        if weapon == W_SNIPER and not me.scoped:            # hip-fired sniper: off by up to SNIPER_SPREAD
            a, r = self.rng.uniform(0, 2 * math.pi), SNIPER_SPREAD * math.sqrt(self.rng.random())
            yaw, pitch = me.yaw + r * math.cos(a) / max(0.2, math.cos(me.pitch)), me.pitch + r * math.sin(a)
            d = look_dir(yaw, max(-1.55, min(1.55, pitch)))
        yaw, pitch = dir_angles(d)
        if weapon == W_SNIPER:
            me.scoped = False                                # bolt action: the scope comes off after a shot
        targets = [(v.pid, (v.x, v.y, v.z)) for v in self.views.values() if v.alive and v.placed]
        kind, dist, _, pid = raycast(ex, ey, ez, d, targets=targets, weapon=weapon)
        end = (ex + d[0] * dist, ey + d[1] * dist, ez + d[2] * dist)
        if weapon != W_KNIFE:
            self.tracers.append([(ex, ey - 0.15, ez), end, 0.12 if weapon == W_SNIPER else 0.06])
            self.shots.append((end, d, kind))
        self.stats["shots"] += 1
        if pid is not None:
            self.hitmarker_t = 0.12                     # predicted; the host confirms with an event
            self.views[pid].hit_flash = 0.1
            if self.views[pid].lagging and self.role == "host":
                self.msg(f"{self.views[pid].name} is lagging - hit not counted", 2)
        if self.role == "host" and self.world is not None:
            w = self.world
            p = w.players[self.node]
            p.fired = True
            if self.on_fire:
                self.on_fire(self, -1, (ex, ey, ez), d, {pid: pos for pid, pos in targets}, self.now)
            p.heal_until = -INF
            res = w.fire(p, (ex, ey, ez), d, self.now, self.now, None, weapon)
            if self.on_shot:
                self.on_shot(self, self.node, -1, None, res, self.now, self.now)
        elif self.role == "client" and self.admitted:
            aid = self._queue_action(A_FIRE, yaw, pitch, weapon)
            if self.on_fire:
                self.on_fire(self, aid, (ex, ey, ez), d, {pid: pos for pid, pos in targets}, self.now)

    def _queue_action(self, kind, yaw, pitch, weapon=None) -> int:
        if self.role != "client":
            return -1
        self.aid = (self.aid + 1) & 0xFF
        vs, vn, vf = self._view_ref() if self.snaps else (0, 0, 0)
        self.pending.append(dict(aid=self.aid, kind=kind, t=self.now, yaw=yaw, pitch=pitch, vs=vs, vn=vn, vf=vf,
                                 x=self.me.x, z=self.me.z, first_seq=None, sends=0,
                                 weapon=self.me.weapon if weapon is None else weapon))
        return self.aid

    # ---------------------------------------------------------------- weapons, scope, healing (UI calls these)
    def switch_weapon(self, w: int):
        if self.me.switch_weapon(w) and self.role == "host" and self.world is not None:
            self.world.players[self.node].weapon = w

    def cycle_weapon(self, step: int):
        order = [W_RIFLE, W_SNIPER, W_KNIFE]
        self.switch_weapon(order[(order.index(self.me.weapon) + step) % len(order)])

    def set_scope(self, on: bool):
        me = self.me
        me.scoped = bool(on) and me.alive and me.weapon == W_SNIPER and not me.reloading and me.switch_t <= 0

    def heal(self) -> bool:
        """H: use a medkit (HEAL_AMOUNT HP over HEAL_TIME s, then HEAL_COOLDOWN s until the next)."""
        me = self.me
        if self.mode != "arena" or not self.admitted or not me.can_heal():
            if me.alive and me.medkits <= 0:
                self.msg("No medkit - pick one up (white boxes with a red cross)", 2)
            elif me.alive and me.heal_cd > 0 and not me.healing:
                self.msg(f"Heal ready in {me.heal_cd:.0f} s", 1.5)
            elif me.alive and me.hp >= MAX_HP:
                self.msg("Already at full health", 1.5)
            return False
        if self.role == "host" and self.world is not None:
            p = self.world.players[self.node]
            if not self.world.start_heal(p, self.now):
                return False
            me.start_heal()
            me.medkits = p.medkits
            return True
        me.start_heal()
        self._queue_action(A_HEAL, me.yaw, me.pitch)
        return True

    # ---------------------------------------------------------------- sending
    def current_rate(self) -> float:
        if self.role == "host" and self.world is not None and len(self.world.players) > 1:
            return self.rate_eff
        if self.role == "client" and self.host_id in self.peers and self.peers[self.host_id].connected(self.now):
            return self.rate_eff
        return min(self.rate, LOBBY_RATE)

    def _rtt_sample(self, pid: int, sample: float):
        """Round trip to/from pid, for congestion control: queueing = sample - smallest RTT of the last 20 s."""
        h = self.rtt_hist.setdefault(pid, deque())
        h.append((self.now, sample))
        while h and self.now - h[0][0] > 20.0:
            h.popleft()
        floor = min(x for _, x in h)
        self.queueing.append((self.now, sample - floor))

    def _adapt_rate(self):
        """AIMD on the packet rate. If the round trips show the radio queue filling up (median extra delay of the
        last 1.5 s > CC_QUEUE_S), send 30 % less; otherwise 0.5/s more, up to the menu rate. A FIFO queue in the
        MAC (v6.9 datagrams) then never holds seconds of game packets."""
        if self.now - self.cc_t < 1.0:
            return
        self.cc_t = self.now
        while self.queueing and self.now - self.queueing[0][0] > 1.5:
            self.queueing.popleft()
        if not self.adaptive:
            self.rate_eff = self.rate
            return
        if not self.queueing:
            return
        q = sorted(x for _, x in self.queueing)[len(self.queueing) // 2]
        self.cc_queue = q
        if q > CC_QUEUE_S:
            self.rate_eff = max(min(self.rate, CC_MIN_RATE), self.rate_eff * 0.7)
            self.stats["rate_cuts"] += 1
        else:
            self.rate_eff = min(self.rate, self.rate_eff + 0.5)

    def _send_beacon(self, leaving=False):
        fl = 0
        if self.mode == "arena":
            fl |= B_READY
            if self.role == "host" or (self.role == "client" and self.admitted):
                fl |= B_IN_ARENA
        if self.role == "host":
            fl |= B_HOSTING
        if leaving:
            fl = B_LEAVING
        fl |= (B_ARQ if self.arq else 0) | (B_ELECT_RANDOM if self.elect_random else 0)
        peers = []
        for p in sorted(self.peers.values(), key=lambda q: -q.last_rx):
            if not p.connected(self.now) or p.last_seq is None:
                continue
            peers.append(PeerInfo(p.pid, p.last_seq & 0xFF, int((self.now - p.last_rx) * 1000),
                                  int(p.rtt * 1000) if p.rtt is not None else -1))
        peers = peers[:MAX_PEERS]
        self.my_reported = {q.id: (q.rtt // 4 * 4) / 1000.0 for q in peers if q.rtt >= 0}
        seq = self._next_seq()
        host = self.host_id if self.host_id is not None else NONE_ID
        if self.role == "electing" and self.mode == "arena" and not leaving:
            host = self.expect_host if self.expect_host is not None else self.elect(self._current_cands())
        epoch = self.epoch if self.role in ("host", "client") else self.epoch_seen
        b = Beacon(self.node, seq, self.now_ms(), fl, self.me.name, self.rate, self.max_players, self.max_lag,
                   host, epoch, peers)
        self.link.send(build_payload(b), BCAST)
        self.stats["beacons"] += 1

    def _send_input(self, leaving=False):
        me = self.me
        seq = self._next_seq()
        fl = (I_IN if not leaving else 0) | (I_LEAVING if leaving else 0)
        fl |= (I_RELOADING if me.reloading else 0) | (I_ALIVE if me.alive else 0) | ((me.life & 3) << 4)
        fl |= (me.weapon & 3) << 6
        # actions not yet acknowledged: each goes out in up to ACTION_SENDS inputs (fewest sends first, so a
        # burst of shots can't starve the newest ones), until the host acknowledges an input that carried it
        self.pending = [a for a in self.pending
                        if self.now - a["t"] < self.max_lag + 0.5 and a["sends"] < ACTION_SENDS]
        acts = []
        for a in sorted(self.pending, key=lambda a: (a["sends"], a["t"]))[:MAX_ACTIONS]:
            if a["first_seq"] is None:
                a["first_seq"] = seq
            a["sends"] += 1
            dx, dz = a["x"] - me.x, a["z"] - me.z
            known = abs(dx) < 5.0 and abs(dz) < 5.0
            acts.append(Action(a["aid"], a["kind"], int((a["t"] - self.now) * 1000), a["yaw"], a["pitch"],
                               a["vs"], a["vn"], a["vf"], dx if known else None, dz if known else None,
                               a.get("weapon", W_RIFLE)))
        acts.sort(key=lambda a: a.dt)
        hold = int((self.now - self.snap_ack_rx) * 1000) if self.snap_ack is not None else 0
        inp = Input(self.node, seq, self.now_ms(), self.host_id, fl, me.x, me.y, me.z, me.yaw, me.pitch,
                    self.snap_ack or 0, hold, acts)
        self.pred[seq] = (me.x, me.y, me.z)
        if len(self.pred) > 300:
            for k in list(self.pred)[:100]:
                del self.pred[k]
        data = build_payload(inp)
        if self.arq and hasattr(self.link, "send_reliable"):
            self.link.send_reliable(data, self.host_id)     # v6.9: the bridge's reliable messages (SR-ARQ)
        elif self.arq or getattr(self.link, "unicast_ok", False):
            self.link.send(data, self.host_id)               # v6.8 Game Link: unicast = SR-ARQ; v6.9: datagram
        else:
            self.link.send(data, BCAST)
        self.stats["inputs_sent"] += 1
        self.last_input_t = self.now

    # ---------------------------------------------------------------- one frame
    def step(self, dt, fwd=0.0, strafe=0.0, sprint=False, jump=False, firing=False):
        self.now += dt
        self._dt = dt
        me = self.me
        if self.mode == "arena" and self.role in ("host", "client") and self.admitted:
            me.update(dt, fwd, strafe, sprint, jump)
            if me.alive and me.mag_size and me.ammo <= 0 and not me.reloading:
                self.start_reload()
            elif firing:
                self.fire(pressed=not me.trigger)
            me.trigger = bool(firing)
        self._receive()
        if self.role in ("host", "client"):
            self._adapt_rate()

        if self.mode == "arena":
            if self.role == "electing":
                self._elect_step()
            elif self.role == "client":
                hp = self.peers.get(self.host_id)
                if hp is None or not hp.connected(self.now) or \
                        self.now - max(self.last_snap_rx, self.join_t) > PEER_TIMEOUT:
                    self._migrate("Host lost")
                elif not self.admitted and self.now - self.join_t > FULL_WAIT and hp.snap_full:
                    self.leave_game("Arena full - back in the lobby")
        if self.role == "host" and self.world is not None:
            w = self.world
            hp = w.players[self.node]
            if hp.alive and me.alive and hp.life == me.life:
                hp.x, hp.y, hp.z, hp.yaw, hp.pitch = me.x, me.y, me.z, me.yaw, me.pitch
                hp.weapon = me.weapon
                me.hp = hp.hp
            elif not hp.alive and me.alive:
                me.hp = 0
                me.dead_t = hp.dead_t
                me.reload_t = 0.0
            w.tick(dt, self.now)
            if hp.alive and hp.life != me.life:              # the world respawned me
                me.respawn_at(hp.x, hp.z, hp.y)
                me.life = hp.life
                self.msg("Respawned", 2)
            if hp.medkits > me.medkits:
                self.msg(f"+{hp.medkits - me.medkits} medkit  (H to heal)", 2)
            me.medkits = hp.medkits
            if not hp.healing(self.now):
                me.heal_t = 0.0
            self.pickups = [w.spot_pos(k) + ("spot",) for k in range(len(MEDKIT_SPOTS)) if self.now >= w.spot_t[k]]
            self.pickups = [(x, y, z, kind) for x, y, z, kind in self.pickups] + \
                [(d[0], d[2], d[1], "drop") for d in w.drops.values()]
            me.kills, me.deaths = hp.kills, hp.deaths
            for pid, q in list(w.players.items()):
                if pid != self.node and self.now - q.last_input_rx > PEER_TIMEOUT:
                    w.remove(pid)
                    self.msg(f"{q.name} lost", 3)
            self._host_views()
        elif self.role == "client":
            self._interp()

        # sending
        if self.mode == "arena" and self.role == "electing":
            period = self.rng.uniform(0.35, 0.6)          # random: nodes that started together don't collide
        elif self.role in ("host", "client"):
            period = ARENA_BEACON_S
        else:
            period = 1.0 / LOBBY_RATE
        self.beacon_t -= dt
        if self.beacon_t <= 0:
            self.beacon_t = period
            self._send_beacon()
        if self.role in ("host", "client"):
            rate = self.current_rate()
            self.send_t -= dt
            if self.send_t > 1.0 / rate:
                self.send_t = 1.0 / rate
            # a new shot goes out early (at most twice the packet rate): the host judges it sooner
            urgent = self.role == "client" and self.now - self.last_input_t >= 0.5 / rate and \
                any(a["sends"] == 0 for a in self.pending)
            if self.send_t <= 0 or urgent:
                self.send_t = 1.0 / rate if urgent else max(self.send_t + 1.0 / rate, 0.0)
                if self.role == "host":
                    seq = self._next_seq()
                    self.link.send(build_payload(self.world.snapshot(seq, self.now_ms(), self.now)), BCAST)
                else:
                    self._send_input()

        # timers
        self.hitmarker_t = max(0.0, self.hitmarker_t - dt)
        self.confirm_t = max(0.0, self.confirm_t - dt)
        self.damage_t = max(0.0, self.damage_t - dt)
        k = min(1.0, dt * 10.0)
        self.err_vis = [e * (1 - k) for e in self.err_vis]
        for v in self.views.values():
            v.tracer_t = max(0.0, v.tracer_t - dt)
            v.hit_flash = max(0.0, v.hit_flash - dt)
        for t in self.tracers:
            t[2] -= dt
        self.tracers = [t for t in self.tracers if t[2] > 0]
        for lst in (self.messages, self.kill_feed):
            for m in lst:
                m[-1] -= dt
            while lst and lst[0][-1] <= 0:
                lst.popleft()
        for p in list(self.peers.values()):
            if self.now - p.last_rx > 30.0:
                del self.peers[p.pid]

    # ---------------------------------------------------------------- for the UI
    def link_state(self, p: Peer) -> str:
        if not p.connected(self.now):
            return "lost"
        if p.not_hears_me_t > p.hears_me_t and self.now - p.not_hears_me_t < PEER_TIMEOUT:
            return "one-way"
        if self.now - p.hears_me_t < PEER_TIMEOUT + ARENA_BEACON_S:
            return "two-way"
        return "linked"

    def peer_status(self, p: Peer) -> str:
        if not p.connected(self.now):
            return "lost"
        if self.now - p.hosting_t < PEER_TIMEOUT:
            return "HOST"
        if p.in_arena:
            return "arena"
        if p.ready:
            return "ready"
        return "lobby"

    def roster(self) -> List[Peer]:
        ps = [p for p in self.peers.values() if not p.leaving]
        order = {"HOST": 0, "arena": 1, "ready": 2, "lobby": 3, "lost": 4}
        return sorted(ps, key=lambda p: (order[self.peer_status(p)], p.pid))

    def arena_summary(self) -> str:
        h = self._active_host()
        if h is None:
            return "no arena running - Enter arena starts the host election"
        if h == self.node and self.world is not None:
            return f"arena hosted by you: {len(self.world.players)}/{self.max_players} players"
        p = self.peers[h]
        return f"arena hosted by {p.name} (node {h}): {len(p.snap_ids)}/{p.snap_max_players} players"

    def arena_summary_short(self) -> str:
        if self.role == "host" and self.world is not None:
            return f"{len(self.world.players)}/{self.max_players} players  ·  you host"
        p = self.peers.get(self.host_id) if self.host_id is not None else None
        if p is not None:
            return f"{len(p.snap_ids)}/{p.snap_max_players} players  ·  host {p.name}"
        return ""

    def role_text(self) -> str:
        if self.mode != "arena":
            return "lobby"
        if self.role == "electing":
            if self.expect_host is not None:
                return f"elected {self.name_of(self.expect_host)} - waiting for it"
            left = max(0.0, self.elect_window - (self.now - self.elect_t0))
            return f"electing a host ({left:.1f} s)"
        if self.role == "host":
            return "HOST"
        if self.role == "client":
            return f"joining {self.name_of(self.host_id)}" if not self.admitted else f"client of {self.name_of(self.host_id)}"
        return "-"

    def ping_host(self) -> Optional[float]:
        return self.rtt_host if self.role == "client" else 0.0 if self.role == "host" else None

    def scoreboard(self):
        """[(pid, name, kills, deaths, ping s or None, is_host, is_me, lagging)] sorted by kills."""
        rows = []
        if self.role == "host" and self.world is not None:
            for q in self.world.players.values():
                me = q.pid == self.node
                rows.append((q.pid, self.me.name if me else q.name, q.kills, q.deaths, None if me else q.rtt, me, me,
                             self.world.lagging(q, self.now)))
        elif self.role == "client":
            rows.append((self.node, self.me.name, self.me.kills, self.me.deaths, self.rtt_host, False, True, False))
            for v in self.views.values():
                rows.append((v.pid, v.name, v.kills, v.deaths, self.peers[v.pid].rtt if v.pid in self.peers else None,
                             v.is_host, False, v.lagging))
        return sorted(rows, key=lambda r: (-r[2], r[3], r[0]))

    def others(self) -> List[RemoteView]:
        return [v for v in self.views.values() if v.placed]


# ------------------------------------------------------------------ bots (practice mode and simulation)
def _side_height(r: int, c: int, dr: int, dc: int) -> float:
    """Height of cell (r, c) at the middle of its side facing (dr, dc)."""
    x, z = cell_center(r, c)
    x += dc * (CELL / 2 - 0.01)
    z += dr * (CELL / 2 - 0.01)
    return _surface(cell_info(r, c), r, c, x, z)


def _build_nav():
    """Walkable cell graph for the bots: up at most a step, down any drop up to 3.6 m."""
    nav = {}
    for r, c in open_cells():
        out = []
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            r2, c2 = r + dr, c + dc
            if solid_cell(r2, c2):
                continue
            ha, hb = _side_height(r, c, dr, dc), _side_height(r2, c2, -dr, -dc)
            if hb - ha <= STEP_H * 0.9 and ha - hb <= 3.6:
                out.append((r2, c2))
        nav[(r, c)] = out
    return nav


NAV = _build_nav()


def nav_path(a: Tuple[int, int], b: Tuple[int, int]) -> Optional[List[Tuple[int, int]]]:
    """Shortest walkable cell path a -> b (BFS), without a; None if b can't be reached."""
    if a == b:
        return []
    prev = {a: None}
    q = deque([a])
    while q:
        cur = q.popleft()
        for nb in NAV.get(cur, ()):
            if nb not in prev:
                prev[nb] = cur
                if nb == b:
                    path = [b]
                    while prev[path[-1]] != a:
                        path.append(prev[path[-1]])
                    return path[::-1]
                q.append(nb)
    return None


class BotBrain:
    """Drives a Node like a player would: walks to random places (stairs and roofs included), aims at the
    nearest visible player (with aim error), fires, reloads; with weapons=True it also switches weapons
    (knife close, sniper far), heals when hurt and fetches medkits. Uses only what its Node received."""

    def __init__(self, node: Node, seed=None, skill=0.5, weapons=False):
        self.n = node
        self.rng = random.Random(seed)
        self.skill = skill
        self.weapons = weapons
        self.path: List[Tuple[int, int]] = []
        self.goal = None
        self.stuck_t = 0.0
        self.last_dist = 1e9
        self.think_t = 0.0
        self.strafe_dir = 1.0
        self.aim_err = (0.0, 0.0)
        self.tick = 0

    def _cell(self):
        b = self.n.me
        return int(math.floor(b.z / CELL)), int(math.floor(b.x / CELL))

    def _new_goal(self):
        n, b = self.n, self.n.me
        here = self._cell()
        goal = None
        if self.weapons and b.medkits == 0 and b.hp < 70 and n.pickups:
            x, _, z, _ = min(n.pickups, key=lambda pk: math.hypot(pk[0] - b.x, pk[2] - b.z))
            goal = (int(z // CELL), int(x // CELL))
        for _ in range(8):
            g = goal or self.rng.choice(open_cells())
            path = nav_path(here, g)
            if path is not None:
                self.goal, self.path = g, path
                return
            goal = None
        self.goal, self.path = None, []

    def step(self, dt):
        n, b = self.n, self.n.me
        self.tick += 1
        fwd = strafe = 0.0
        firing = jump = False
        if n.mode == "arena" and b.alive and n.admitted:
            target, best = None, 1e9
            for v in n.others():
                if not v.alive or v.lagging:
                    continue
                chest = (v.x, v.y + 1.2, v.z)
                dd = math.dist(b.eye(), chest)
                if dd < 40 and dd < best and line_of_sight(b.eye(), chest):
                    target, best = (v.x, v.y, v.z), dd
            if self.weapons:
                if target is not None:
                    want = W_KNIFE if best < 2.8 else W_SNIPER if best > 16 else W_RIFLE
                    if want != b.weapon:
                        n.switch_weapon(want)
                elif b.hp < 60 and b.medkits > 0 and b.can_heal():
                    n.heal()
            if target is not None:
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
                if b.weapon == W_KNIFE:
                    fwd = 1.0
                else:
                    fwd = 0.5 if horiz > 8 else (-0.3 if horiz < 3 else 0.0)
                aimed = abs(wrap_angle(want_yaw - b.yaw)) < 0.25 and b.ammo > 0
                if b.weapon == W_SNIPER:
                    n.set_scope(True)
                    firing = aimed and self.tick % 2 == 0          # one click per shot
                else:
                    firing = aimed
            else:
                n.set_scope(False)
                if not self.path:
                    self._new_goal()
                if self.path:
                    r, c = self.path[0]
                    wx, wz = cell_center(r, c)
                    d = math.hypot(wx - b.x, wz - b.z)
                    want_yaw = math.atan2(wx - b.x, -(wz - b.z))
                    b.yaw = (b.yaw + wrap_angle(want_yaw - b.yaw) * min(1, dt * 6)) % (2 * math.pi)
                    b.pitch *= 0.9
                    fwd = 1.0 if abs(wrap_angle(want_yaw - b.yaw)) < 0.8 else 0.2
                    if d < 0.6 or self._cell() == (r, c) and d < 0.9:
                        self.path.pop(0)
                        self.stuck_t, self.last_dist = 0.0, 1e9
                    else:
                        self.stuck_t = self.stuck_t + dt if d > self.last_dist - 0.01 else 0.0
                        self.last_dist = min(self.last_dist, d)
                        if self.stuck_t > 0.8:
                            jump = True
                        if self.stuck_t > 1.8:                       # give up: somewhere else
                            self.path, self.stuck_t, self.last_dist = [], 0.0, 1e9
                if b.mag_size and b.ammo < b.mag_size // 2 and not b.reloading:
                    n.start_reload()
        n.step(dt, fwd=fwd, strafe=strafe, firing=firing, jump=jump)


class Practice:
    """Offline practice: you + bots on a LocalHub. Every bot is a full Node (same protocol); you are
    the host (you enter first), the bots join your arena."""

    NAMES = ["Bot-A", "Bot-B", "Bot-C", "Bot-D", "Bot-E", "Bot-F", "Bot-G"]

    def __init__(self, my_node=0, n_bots=2, rate=DEFAULT_RATE, max_players=DEFAULT_MAX_PLAYERS,
                 max_lag=DEFAULT_MAX_LAG, name="You", loss=0.0, delay=0.03, seed=1):
        self.hub = LocalHub(delay=delay, loss=loss, seed=seed)
        self.node = Node(my_node, self.hub.endpoint(my_node), name=name, rate=rate, max_players=max_players,
                         max_lag=max_lag, arq=False, election_window=0.2)
        self.bots: List[BotBrain] = []
        ids = [i for i in range(1, 30) if i != my_node][:n_bots]
        for k, nid in enumerate(ids):
            bn = Node(nid, self.hub.endpoint(nid), name=self.NAMES[k % len(self.NAMES)], rate=rate,
                      max_players=max_players, max_lag=max_lag, arq=False, clock_offset=7.3 * (k + 1))
            self.bots.append(BotBrain(bn, seed=seed * 31 + nid, skill=0.45, weapons=True))
        self.node.link.desc = f"practice vs {n_bots} bot{'s' if n_bots != 1 else ''}"

    def step(self, dt, *a, **kw):
        self.hub.advance(dt)
        for b in self.bots:
            if b.n.mode == "lobby" and self.node.role == "host" and b.n._active_host() == self.node.node:
                b.n.join_game()
            b.step(dt)
        self.node.step(dt, *a, **kw)
