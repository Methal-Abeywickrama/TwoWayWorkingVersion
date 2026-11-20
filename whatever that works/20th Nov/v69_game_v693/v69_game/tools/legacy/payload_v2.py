"""payload_v2.py - the earlier peer-to-peer packet (58 bytes, every player sends its own state and its own
hit reports). Kept only so tools/worstcase_sim.py can compare the old design with the host-authoritative one."""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from typing import List

PAYLOAD_LEN = 58
MAX_EVENTS = 3
MAX_HEARD = 3
NAME_LEN = 8
NONE_ID = 255
FLAG_FIRED = 0x01
FLAG_RELOADING = 0x02
FLAG_DEAD = 0x04
FLAG_IN_GAME = 0x08
FLAG_LEAVING = 0x10
_PKT = struct.Struct("<BHHB" "hhh" "Hh" "BBBBBBB" "BHH" "BBB" "8s" "B" + "HBBH" * MAX_EVENTS)
TWO_PI = 2.0 * math.pi


@dataclass
class Event:
    eid: int
    target: int
    damage: int
    tseq: int = 0


@dataclass
class PlayerState:
    pid: int = 0
    seq: int = 0
    ts: int = 0
    flags: int = 0
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    yaw: float = 0.0
    pitch: float = 0.0
    hp: int = 100
    ammo: int = 0
    kills: int = 0
    deaths: int = 0
    killer: int = NONE_ID
    rate: float = 5.0
    max_players: int = 4
    echo_id: int = NONE_ID
    echo_seq: int = 0
    echo_age: int = 0
    heard: List[int] = field(default_factory=list)
    name: str = ""
    events: List[Event] = field(default_factory=list)


def _rnd(v):
    return int(math.floor(v + 0.5))


def _c(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def build_payload(s: PlayerState) -> bytes:
    ev = list(s.events[:MAX_EVENTS])
    flat = []
    for e in ev:
        flat += [e.eid & 0xFFFF, _c(int(e.target), 0, 255), _c(int(e.damage), 0, 255), e.tseq & 0xFFFF]
    flat += [0, 0, 0, 0] * (MAX_EVENTS - len(ev))
    heard = [_c(int(h), 0, 255) for h in s.heard[:MAX_HEARD]] + [NONE_ID] * (MAX_HEARD - min(len(s.heard), MAX_HEARD))
    r = math.fmod(s.yaw, TWO_PI)
    r = r + TWO_PI if r < 0 else r
    d = max(-89.99, min(89.99, math.degrees(s.pitch)))
    return _PKT.pack(_c(s.pid, 0, 255), s.seq & 0xFFFF, s.ts & 0xFFFF, _c(s.flags, 0, 255),
                     _c(_rnd(s.x * 100), -32768, 32767), _c(_rnd(s.y * 100), -32768, 32767), _c(_rnd(s.z * 100), -32768, 32767),
                     _rnd(r / TWO_PI * 65536) & 0xFFFF, _c(_rnd(d * 100), -32768, 32767),
                     _c(int(s.hp), 0, 255), _c(int(s.ammo), 0, 255), s.kills & 0xFF, s.deaths & 0xFF, _c(int(s.killer), 0, 255),
                     _c(_rnd(s.rate * 10), 0, 255), _c(int(s.max_players), 0, 255), _c(int(s.echo_id), 0, 255),
                     s.echo_seq & 0xFFFF, _c(int(s.echo_age), 0, 65535), *heard, s.name.encode("ascii", "replace")[:NAME_LEN],
                     len(ev), *flat)


def parse_payload(data: bytes) -> PlayerState:
    f = _PKT.unpack_from(bytes(data))
    n = min(f[23], MAX_EVENTS)
    raw = f[24:]
    events = [Event(raw[4 * i], raw[4 * i + 1], raw[4 * i + 2], raw[4 * i + 3]) for i in range(n)]
    return PlayerState(pid=f[0], seq=f[1], ts=f[2], flags=f[3], x=f[4] / 100.0, y=f[5] / 100.0, z=f[6] / 100.0,
                       yaw=f[7] / 65536.0 * TWO_PI, pitch=math.radians(f[8] / 100.0), hp=f[9], ammo=f[10], kills=f[11],
                       deaths=f[12], killer=f[13], rate=f[14] / 10.0, max_players=f[15], echo_id=f[16], echo_seq=f[17],
                       echo_age=f[18], heard=[h for h in f[19:22] if h != NONE_ID],
                       name=f[22].rstrip(b"\0").decode("ascii", "replace"), events=events)
