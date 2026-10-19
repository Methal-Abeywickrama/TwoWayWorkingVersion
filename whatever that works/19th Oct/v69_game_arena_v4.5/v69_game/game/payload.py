"""
payload.py - Radio Arena v4 packets (host-authoritative). Pure application data: no marker, no version
byte, no CRC (the radio adds its own header: v6.9 bridge 11 B / v6.8 tag C5 12; the framer adds CRC32 + FEC).
v4 = v3 + weapons, healing, medkit pickups, 2 cm height steps. The type numbers moved from 1-3 to 4-6, so a
v3 game and a v4 game simply don't see each other (instead of misreading each other's packets).

Pure Python, no dependencies (GNU Radio's `pmt` only for the PDU helpers). The C mirror (c/payload.c,
c/payload.h) produces byte-identical packets.

Every packet starts with the same 6-byte header, little-endian:
    type u8 | node u8 | seq u16 (per node, all types share it) | ts u16 (sender clock, ms mod 65536)

type 4  BEACON    every node, broadcast, 1/s in the lobby (every 3 s in the arena)    21 + 4/peer bytes
        flags u8   bit0 READY (wants to play)  bit1 IN_ARENA  bit2 HOSTING  bit3 LEAVING  bit4 ARQ
                   bit5 ELECT_RANDOM (host election: random instead of lowest ping)
        name 8s | rate u8 (0.1/s) | max_players u8 | max_lag u8 (10 ms) | host u8 (255 none) | epoch u8
        n u8 | n x peer: id u8, echo u8 (low byte of the newest seq heard from that node),
                         hold u8 (4 ms: time since it was heard), rtt u8 (4 ms, 255 = unknown)

type 5  INPUT     client -> host, at the client's packet rate                           22 + 14/action bytes
        host u8 | flags u8  bit0 IN (wants to be in the arena)  bit1 LEAVING  bit2 RELOADING
                            bit3 ALIVE (as the client believes)  bits4-5 life (respawn counter mod 4)
                            bits6-7 weapon in hand (0 rifle, 1 sniper, 2 knife)
        x, y, z i16 cm | yaw u16 (65536 = 360 deg) | pitch i16 (centi-degrees)
        snap_ack u16 (newest snapshot seq received) | snap_hold u8 (4 ms since it arrived)
        n u8 | n x action (max 3): aid u8 | kind u8 (bits0-3: 1 fire, 2 reload, 3 heal; bits4-5 weapon)
                   | dt i16 (ms, action time - ts)
                   | yaw u16 | pitch i16 | view_seq u16 | view_next u8 | view_frac u8 | dx i8 | dz i8
                   view = what the shooter's screen showed: interpolated between snapshot view_seq and
                          snapshot view_seq + view_next at view_frac/255; view_next 0 = extrapolating
                          view_frac x 4 ms past snapshot view_seq
                   dx, dz = shooter position at the shot minus the position in this packet (4 cm steps,
                          -128 = unknown)

type 6  SNAPSHOT  host -> everyone, broadcast, at the host's packet rate   13 + 14/player + 5/event + 3/drop
        epoch u8 | flags u8 (bit0 CLOSING, bit1 FULL) | max_players u8 | n_players u8 | n_events u8
        | spots u8 (bit k = medkit at fixed spot k is there) | n_drops u8
        n_players x: id u8 | pos u32 (x 12 bit cm | z 12 bit cm << 12 | y 8 bit 2 cm << 24) | yaw u8
                     | pitch i8 (90/127 deg) | hp u8 | flags u8 (bit0 DEAD bit1 FIRED bit2 RELOADING
                     bits3-4 life bit5 CORRECTED bit6 HOST) | gear u8 (bits0-1 weapon, bit2 HEALING,
                     bits3-4 medkits carried) | kills u8 | deaths u8
                     | ack u8 (low byte of the newest INPUT seq the host applied) | hold u8 (4 ms)
        n_events x:  eid u8 | shooter u8 | victim u8 | damage u8 | info u8 (bit0 HEAD, bit1 KILL, bits2-3 weapon)
        n_drops x:   id u8 | x u8 | z u8 (12.5 cm steps): medkits dropped by eliminated players

With 4 players a snapshot is 69-74 bytes and an input at most 64.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from typing import List, Optional, Union

T_BEACON, T_INPUT, T_SNAPSHOT = 4, 5, 6
GAME_TYPES = (T_BEACON, T_INPUT, T_SNAPSHOT)
NONE_ID = 255
NAME_LEN = 8
MAX_PEERS = 7
MAX_ACTIONS = 3
MAX_SNAP_PLAYERS = 8
MAX_EVENTS = 3
MAX_DROPS = 4
FRAME_BUDGET = 68            # payload bytes that still fit a 3-buffer (12.3 ms) frame on v6.8
FRAME_BUDGET_V69 = 59        # the same frame on v6.9: the bridge's datagram header is 11 B instead of the 2 B tag

# BEACON flags
B_READY, B_IN_ARENA, B_HOSTING, B_LEAVING, B_ARQ, B_ELECT_RANDOM = 0x01, 0x02, 0x04, 0x08, 0x10, 0x20
# INPUT flags
I_IN, I_LEAVING, I_RELOADING, I_ALIVE = 0x01, 0x02, 0x04, 0x08
# SNAPSHOT header flags
S_CLOSING, S_FULL = 0x01, 0x02
# SNAPSHOT player flags
P_DEAD, P_FIRED, P_RELOADING, P_CORRECTED, P_HOST = 0x01, 0x02, 0x04, 0x20, 0x40
# actions (low nibble of Action.kind on the air; the weapon is in the high nibble)
A_FIRE, A_RELOAD, A_HEAL = 1, 2, 3
# weapons
W_RIFLE, W_SNIPER, W_KNIFE = 0, 1, 2
# SNAPSHOT player gear
G_HEALING = 0x04
# event info
E_HEAD, E_KILL = 0x01, 0x02

_HDR = struct.Struct("<BBHH")
_BEACON = struct.Struct("<B8sBBBBBB")
_PEER = struct.Struct("<BBBB")
_INPUT = struct.Struct("<BBhhhHhHBB")
_ACTION = struct.Struct("<BBhHhHBBbb")
_SNAP = struct.Struct("<BBBBBBB")
_SPLAYER = struct.Struct("<BIBbBBBBBBB")
_EVENT = struct.Struct("<BBBBB")
_DROP = struct.Struct("<BBB")
assert (_HDR.size, _BEACON.size, _PEER.size, _INPUT.size, _ACTION.size, _SNAP.size, _SPLAYER.size, _EVENT.size,
        _DROP.size) == (6, 15, 4, 16, 14, 7, 14, 5, 3)

TWO_PI = 2.0 * math.pi
MAX_PITCH_DEG = 89.99


# ------------------------------------------------------------------ messages
@dataclass
class PeerInfo:
    id: int
    echo: int = 0          # low byte of the newest seq heard from that node
    hold: int = 0          # ms since that packet arrived (sent in 4 ms steps)
    rtt: int = -1          # ms, -1 = unknown (sent in 4 ms steps)


@dataclass
class Beacon:
    node: int = 0
    seq: int = 0
    ts: int = 0
    flags: int = 0
    name: str = ""
    rate: float = 5.0
    max_players: int = 4
    max_lag: float = 0.8   # s (sent in 10 ms steps)
    host: int = NONE_ID
    epoch: int = 0
    peers: List[PeerInfo] = field(default_factory=list)
    type: int = T_BEACON


@dataclass
class Action:
    aid: int
    kind: int              # A_FIRE / A_RELOAD
    dt: int = 0            # ms relative to the packet ts (<= 0)
    yaw: float = 0.0
    pitch: float = 0.0
    view_seq: int = 0      # snapshot the view starts from
    view_next: int = 0     # + this many seq to the second snapshot (0 = extrapolating)
    view_frac: int = 0     # 0..255 between the two; extrapolating: 4 ms steps past view_seq
    dx: Optional[float] = None   # shooter position at the shot - packet position (m), None = unknown
    dz: Optional[float] = None
    weapon: int = 0        # W_* (fire / reload)


@dataclass
class Input:
    node: int = 0
    seq: int = 0
    ts: int = 0
    host: int = NONE_ID
    flags: int = 0         # I_* plus life in bits 4-5
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    yaw: float = 0.0
    pitch: float = 0.0
    snap_ack: int = 0
    snap_hold: int = 0     # ms (4 ms steps)
    actions: List[Action] = field(default_factory=list)
    type: int = T_INPUT

    @property
    def life(self) -> int:
        return (self.flags >> 4) & 3

    @property
    def weapon(self) -> int:
        return (self.flags >> 6) & 3


@dataclass
class SnapPlayer:
    id: int
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    yaw: float = 0.0
    pitch: float = 0.0
    hp: int = 100
    flags: int = 0         # P_* plus life in bits 3-4
    kills: int = 0
    deaths: int = 0
    ack: int = 0           # low byte of the newest input seq applied
    hold: int = 0          # ms (4 ms steps)
    gear: int = 0          # weapon bits0-1 | G_HEALING | medkits << 3

    @property
    def life(self) -> int:
        return (self.flags >> 3) & 3

    @property
    def weapon(self) -> int:
        return self.gear & 3

    @property
    def healing(self) -> bool:
        return bool(self.gear & G_HEALING)

    @property
    def medkits(self) -> int:
        return (self.gear >> 3) & 3


@dataclass
class SnapEvent:
    eid: int
    shooter: int
    victim: int
    damage: int
    head: bool = False
    kill: bool = False
    weapon: int = 0


@dataclass
class Drop:
    id: int
    x: float = 0.0
    z: float = 0.0


@dataclass
class Snapshot:
    node: int = 0          # the host
    seq: int = 0
    ts: int = 0
    epoch: int = 0
    flags: int = 0
    max_players: int = 4
    players: List[SnapPlayer] = field(default_factory=list)
    events: List[SnapEvent] = field(default_factory=list)
    spots: int = 0         # bitmask: fixed medkit spots that hold a medkit
    drops: List[Drop] = field(default_factory=list)
    type: int = T_SNAPSHOT


Message = Union[Beacon, Input, Snapshot]


# ------------------------------------------------------------------ quantisation (same as the C code)
def _rnd(v: float) -> int:
    return int(math.floor(v + 0.5))


def _clamp(v: int, lo: int, hi: int) -> int:
    return lo if v < lo else hi if v > hi else v


def _u8(v) -> int:
    return _clamp(int(v), 0, 255)


def _q4(ms) -> int:                     # ms -> 4 ms steps in a byte
    return _clamp(_rnd(ms / 4.0), 0, 255)


def _yaw16(rad: float) -> int:
    r = math.fmod(rad, TWO_PI)
    if r < 0:
        r += TWO_PI
    return _rnd(r / TWO_PI * 65536.0) & 0xFFFF


def _yaw8(rad: float) -> int:
    r = math.fmod(rad, TWO_PI)
    if r < 0:
        r += TWO_PI
    return _rnd(r / TWO_PI * 256.0) & 0xFF


def _pitch16(rad: float) -> int:
    d = max(-MAX_PITCH_DEG, min(MAX_PITCH_DEG, math.degrees(rad)))
    return _clamp(_rnd(d * 100.0), -32768, 32767)


def _pitch8(rad: float) -> int:
    d = max(-90.0, min(90.0, math.degrees(rad)))
    return _clamp(_rnd(d * 127.0 / 90.0), -127, 127)


def _d4(m) -> int:                      # metres -> 4 cm steps in an i8, -128 = unknown
    if m is None:
        return -128
    return _clamp(_rnd(m * 25.0), -127, 127)


def _cm16(m: float) -> int:
    return _clamp(_rnd(m * 100.0), -32768, 32767)


def _pack_pos(x, y, z) -> int:
    xc = _clamp(_rnd(x * 100.0), 0, 4095)
    zc = _clamp(_rnd(z * 100.0), 0, 4095)
    yc = _clamp(_rnd(y * 50.0), 0, 255)            # 2 cm steps: 0 .. 5.10 m
    return xc | (zc << 12) | (yc << 24)


def _p8(m: float) -> int:                # metres -> 12.5 cm steps in a byte (0 .. 31.9 m)
    return _clamp(_rnd(m * 8.0), 0, 255)


# ------------------------------------------------------------------ build
def build_payload(m: Message) -> bytes:
    """Beacon / Input / Snapshot -> bytes."""
    hdr = _HDR.pack(m.type, _u8(m.node), m.seq & 0xFFFF, m.ts & 0xFFFF)
    if m.type == T_BEACON:
        peers = m.peers[:MAX_PEERS]
        out = hdr + _BEACON.pack(_u8(m.flags), m.name.encode("ascii", "replace")[:NAME_LEN],
                                 _u8(_rnd(m.rate * 10.0)), _u8(m.max_players), _u8(_rnd(m.max_lag * 100.0)),
                                 _u8(m.host), m.epoch & 0xFF, len(peers))
        for p in peers:
            out += _PEER.pack(_u8(p.id), p.echo & 0xFF, _q4(p.hold), 255 if p.rtt < 0 else min(254, _q4(p.rtt)))
        return out
    if m.type == T_INPUT:
        acts = m.actions[:MAX_ACTIONS]
        out = hdr + _INPUT.pack(_u8(m.host), _u8(m.flags), _cm16(m.x), _cm16(m.y), _cm16(m.z), _yaw16(m.yaw),
                                _pitch16(m.pitch), m.snap_ack & 0xFFFF, _q4(m.snap_hold), len(acts))
        for a in acts:
            unknown = a.dx is None or a.dz is None
            kind = (int(a.kind) & 0x0F) | ((int(a.weapon) & 3) << 4)
            out += _ACTION.pack(a.aid & 0xFF, kind, _clamp(int(a.dt), -32768, 32767), _yaw16(a.yaw),
                                _pitch16(a.pitch), a.view_seq & 0xFFFF, _u8(a.view_next), _u8(a.view_frac),
                                -128 if unknown else _d4(a.dx), -128 if unknown else _d4(a.dz))
        return out
    if m.type == T_SNAPSHOT:
        pl = m.players[:MAX_SNAP_PLAYERS]
        ev = m.events[:MAX_EVENTS]
        dr = m.drops[:MAX_DROPS]
        out = hdr + _SNAP.pack(m.epoch & 0xFF, _u8(m.flags), _u8(m.max_players), len(pl), len(ev), m.spots & 0xFF,
                               len(dr))
        for p in pl:
            out += _SPLAYER.pack(_u8(p.id), _pack_pos(p.x, p.y, p.z), _yaw8(p.yaw), _pitch8(p.pitch), _u8(p.hp),
                                 _u8(p.flags), p.gear & 0xFF, p.kills & 0xFF, p.deaths & 0xFF, p.ack & 0xFF,
                                 _q4(p.hold))
        for e in ev:
            info = (E_HEAD if e.head else 0) | (E_KILL if e.kill else 0) | ((int(e.weapon) & 3) << 2)
            out += _EVENT.pack(e.eid & 0xFF, _u8(e.shooter), _u8(e.victim), _u8(e.damage), info)
        for d in dr:
            out += _DROP.pack(d.id & 0xFF, _p8(d.x), _p8(d.z))
        return out
    raise ValueError(f"unknown message type {m.type}")


# ------------------------------------------------------------------ parse
def parse_payload(data: bytes) -> Message:
    """bytes -> Beacon / Input / Snapshot. Raises ValueError on a short or unknown packet."""
    data = bytes(data)
    if len(data) < _HDR.size:
        raise ValueError("short packet")
    typ, node, seq, ts = _HDR.unpack_from(data)
    o = _HDR.size
    try:
        if typ == T_BEACON:
            fl, name, rate, maxp, lag, host, epoch, n = _BEACON.unpack_from(data, o)
            o += _BEACON.size
            peers = []
            for _ in range(min(n, MAX_PEERS)):
                pid, echo, hold, rtt = _PEER.unpack_from(data, o)
                o += _PEER.size
                peers.append(PeerInfo(pid, echo, hold * 4, -1 if rtt == 255 else rtt * 4))
            return Beacon(node, seq, ts, fl, name.rstrip(b"\0").decode("ascii", "replace"), rate / 10.0, maxp,
                          lag / 100.0, host, epoch, peers)
        if typ == T_INPUT:
            host, fl, x, y, z, yaw, pitch, sack, shold, n = _INPUT.unpack_from(data, o)
            o += _INPUT.size
            acts = []
            for _ in range(min(n, MAX_ACTIONS)):
                aid, kind, dt, ay, ap, vs, vn, vf, dx, dz = _ACTION.unpack_from(data, o)
                o += _ACTION.size
                unk = dx == -128 or dz == -128
                acts.append(Action(aid, kind & 0x0F, dt, ay / 65536.0 * TWO_PI, math.radians(ap / 100.0), vs, vn, vf,
                                   None if unk else dx / 25.0, None if unk else dz / 25.0, (kind >> 4) & 3))
            return Input(node, seq, ts, host, fl, x / 100.0, y / 100.0, z / 100.0, yaw / 65536.0 * TWO_PI,
                         math.radians(pitch / 100.0), sack, shold * 4, acts)
        if typ == T_SNAPSHOT:
            epoch, fl, maxp, n, ne, spots, nd = _SNAP.unpack_from(data, o)
            o += _SNAP.size
            pl = []
            for _ in range(min(n, MAX_SNAP_PLAYERS)):
                pid, pos, yaw, pitch, hp, pf, gear, k, d, ack, hold = _SPLAYER.unpack_from(data, o)
                o += _SPLAYER.size
                pl.append(SnapPlayer(pid, (pos & 0xFFF) / 100.0, ((pos >> 24) & 0xFF) / 50.0,
                                     ((pos >> 12) & 0xFFF) / 100.0, yaw / 256.0 * TWO_PI,
                                     math.radians(pitch * 90.0 / 127.0), hp, pf, k, d, ack, hold * 4, gear))
            ev = []
            for _ in range(min(ne, MAX_EVENTS)):
                eid, sh, vi, dmg, info = _EVENT.unpack_from(data, o)
                o += _EVENT.size
                ev.append(SnapEvent(eid, sh, vi, dmg, bool(info & E_HEAD), bool(info & E_KILL), (info >> 2) & 3))
            dr = []
            for _ in range(min(nd, MAX_DROPS)):
                did, dx, dz = _DROP.unpack_from(data, o)
                o += _DROP.size
                dr.append(Drop(did, dx / 8.0, dz / 8.0))
            return Snapshot(node, seq, ts, epoch, fl, maxp, pl, ev, spots, dr)
    except struct.error as e:
        raise ValueError(f"truncated type-{typ} packet") from e
    raise ValueError(f"unknown message type {typ}")


# ------------------------------------------------------------------ GNU Radio helpers
def to_pdu(data: bytes, meta=None):
    import pmt
    return pmt.cons(meta if meta is not None else pmt.PMT_NIL, pmt.init_u8vector(len(data), list(data)))


def from_pdu(msg) -> bytes:
    import pmt
    vec = pmt.cdr(msg) if pmt.is_pair(msg) else msg
    return bytes(pmt.u8vector_elements(vec))


def sample_messages():
    """The three messages used by the self-tests (same values as c/test_payload.c)."""
    b = Beacon(node=3, seq=65535, ts=54321, flags=B_READY | B_ARQ, name="Methal", rate=4.5, max_players=4,
               max_lag=0.8, host=1, epoch=7, peers=[PeerInfo(0, 200, 120, 88), PeerInfo(1, 17, 4, -1)])
    i = Input(node=3, seq=1000, ts=777, host=1, flags=I_IN | I_ALIVE | (2 << 4), x=12.34, y=0.5, z=-3.21,
              yaw=math.radians(270), pitch=math.radians(-12.5), snap_ack=4242, snap_hold=36,
              actions=[Action(9, A_FIRE, -120, math.radians(271.5), math.radians(-12.0), 4240, 1, 180, -0.36, 1.2,
                              W_SNIPER),
                       Action(10, A_RELOAD, -20), Action(11, A_HEAL, -8)])
    s = Snapshot(node=1, seq=4243, ts=60000, epoch=7, flags=0, max_players=4,
                 players=[SnapPlayer(1, 3.0, 0.0, 29.5, math.radians(90), math.radians(10), 100, P_HOST, 5, 2, 7, 16,
                                     W_KNIFE | (1 << 3)),
                          SnapPlayer(3, 12.34, 3.52, 30.0, math.radians(270), math.radians(-12.5), 40,
                                     P_FIRED | (2 << 3), 1, 4, 0xE8, 8, W_SNIPER | G_HEALING | (3 << 3))],
                 events=[SnapEvent(77, 1, 3, 20), SnapEvent(78, 1, 3, 150, head=True, kill=True, weapon=W_SNIPER)],
                 spots=0x2D, drops=[Drop(5, 12.5, 20.125)])
    return b, i, s


def _selftest() -> None:
    b, i, s = sample_messages()
    hexes = []
    for m in (b, i, s):
        raw = build_payload(m)
        r = parse_payload(raw)
        assert r.type == m.type and r.node == m.node and r.seq == m.seq and r.ts == m.ts
        assert build_payload(r) == raw, "re-encode mismatch"
        hexes.append(raw.hex())
    rb, ri, rs = (parse_payload(build_payload(m)) for m in (b, i, s))
    assert rb.name == "Methal" and abs(rb.rate - 4.5) < 1e-9 and rb.peers[0].rtt == 88 and rb.peers[1].rtt == -1
    assert ri.life == 2 and abs(ri.x - 12.34) < 0.006 and ri.actions[0].dt == -120 and ri.actions[0].view_frac == 180
    assert ri.actions[0].view_next == 1 and abs(ri.actions[0].dx + 0.36) < 1e-9 and ri.actions[1].dx is None
    assert ri.actions[1].kind == A_RELOAD and ri.snap_hold == 36 and ri.actions[0].weapon == W_SNIPER
    assert ri.actions[2].kind == A_HEAL and ri.weapon == 0
    assert rs.players[1].life == 2 and abs(rs.players[1].x - 12.34) < 0.006 and abs(rs.players[0].z - 29.5) < 0.006
    assert abs(rs.players[1].y - 3.52) < 0.011 and rs.players[1].healing and rs.players[1].medkits == 3
    assert rs.players[0].weapon == W_KNIFE and rs.players[0].medkits == 1 and not rs.players[0].healing
    assert rs.events[1].kill and rs.events[1].head and rs.events[1].damage == 150 and rs.events[1].weapon == W_SNIPER
    assert rs.spots == 0x2D and rs.drops[0].id == 5 and rs.drops[0].x == 12.5 and rs.drops[0].z == 20.125
    for name, h in zip(("beacon", "input", "snapshot"), hexes):
        print(f"{name:8s} {len(h) // 2:3d} B  {h}")
    print("payload self-test OK")


if __name__ == "__main__":
    _selftest()
