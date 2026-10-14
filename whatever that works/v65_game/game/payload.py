"""
payload.py - game state <-> bytes for the 3D shooter. Pure application data:
no marker, no version byte, no CRC. Error checking is left to your link layer.

Pure Python, no dependencies (GNU Radio's `pmt` is only imported by the PDU helpers).
The C mirror (c/payload.c, c/payload.h) produces byte-identical packets.

Packet: fixed 31 bytes, little-endian
---------------------------------------------------------------------------
 off size field        meaning
  0   1   pid          player id (0/1)  == your flowgraph's MY_ID
  1   2   seq          u16, +1 per packet (game drops older packets, counts loss)
  3   2   x            i16, centimetres
  5   2   y            i16, centimetres (feet height, >0 while jumping)
  7   2   z            i16, centimetres
  9   2   yaw          u16, 0..65535  ==  0..360 deg
 11   2   pitch        i16, centi-degrees (+ = looking up)
 13   1   hp           0..100
 14   1   ammo         rounds left in magazine
 15   1   flags        bit0 FIRED (shot since last packet), bit1 RELOADING, bit2 DEAD
 16   1   deaths       times this player died (opponent's score), wraps at 256
 17   1   aux          free byte for you (game sends 0)
 18   1   n_events     0..3 valid entries below
 19  12   events[3]    each: eid u16, target u8, damage u8   (hit reports)
---------------------------------------------------------------------------
Hits are decided by the shooter and sent as events. Each event is repeated in
several consecutive packets (no ACK needed); the receiver de-duplicates by eid.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from typing import List

PAYLOAD_LEN = 31
MAX_EVENTS = 3

FLAG_FIRED = 0x01
FLAG_RELOADING = 0x02
FLAG_DEAD = 0x04

_PKT = struct.Struct("<BH" "hhh" "Hh" "BBBBBB" + "HBB" * MAX_EVENTS)
assert _PKT.size == PAYLOAD_LEN

TWO_PI = 2.0 * math.pi
MAX_PITCH_DEG = 89.99


@dataclass
class Event:
    eid: int      # u16 event id, unique per sender
    target: int   # player id that was hit
    damage: int   # hit points to remove


@dataclass
class PlayerState:
    pid: int = 0
    seq: int = 0
    x: float = 0.0       # metres
    y: float = 0.0
    z: float = 0.0
    yaw: float = 0.0     # radians
    pitch: float = 0.0   # radians
    hp: int = 100
    ammo: int = 0
    flags: int = 0
    deaths: int = 0
    aux: int = 0
    events: List[Event] = field(default_factory=list)


# ---------------------------------------------------------------- quantisation
def _rnd(v: float) -> int:          # round half up (same as C floor(v+0.5))
    return int(math.floor(v + 0.5))


def _i16(v: int) -> int:
    return -32768 if v < -32768 else 32767 if v > 32767 else v


def _u8(v: int) -> int:
    return 0 if v < 0 else 255 if v > 255 else int(v)


def _q_pos(m: float) -> int:
    return _i16(_rnd(m * 100.0))


def _q_yaw(rad: float) -> int:
    r = math.fmod(rad, TWO_PI)
    if r < 0:
        r += TWO_PI
    return _rnd(r / TWO_PI * 65536.0) & 0xFFFF


def _q_pitch(rad: float) -> int:
    d = max(-MAX_PITCH_DEG, min(MAX_PITCH_DEG, math.degrees(rad)))
    return _i16(_rnd(d * 100.0))


# ---------------------------------------------------------------- API
def build_payload(s: PlayerState) -> bytes:
    """PlayerState -> 31 bytes. At most MAX_EVENTS events are packed."""
    ev = list(s.events[:MAX_EVENTS])
    flat: List[int] = []
    for e in ev:
        flat += [e.eid & 0xFFFF, _u8(e.target), _u8(e.damage)]
    flat += [0, 0, 0] * (MAX_EVENTS - len(ev))
    return _PKT.pack(
        _u8(s.pid), s.seq & 0xFFFF,
        _q_pos(s.x), _q_pos(s.y), _q_pos(s.z),
        _q_yaw(s.yaw), _q_pitch(s.pitch),
        _u8(s.hp), _u8(s.ammo), _u8(s.flags), _u8(s.deaths), _u8(s.aux), len(ev),
        *flat,
    )


def parse_payload(data: bytes) -> PlayerState:
    """Bytes -> PlayerState. Reads the first 31 bytes; anything after is ignored.
    Raises ValueError only if fewer than 31 bytes are given (nothing to unpack)."""
    data = bytes(data)
    if len(data) < PAYLOAD_LEN:
        raise ValueError(f"need {PAYLOAD_LEN} bytes, got {len(data)}")
    f = _PKT.unpack_from(data)
    n = min(f[12], MAX_EVENTS)
    raw = f[13:]
    events = [Event(raw[3 * i], raw[3 * i + 1], raw[3 * i + 2]) for i in range(n)]
    return PlayerState(
        pid=f[0], seq=f[1],
        x=f[2] / 100.0, y=f[3] / 100.0, z=f[4] / 100.0,
        yaw=f[5] / 65536.0 * TWO_PI, pitch=math.radians(f[6] / 100.0),
        hp=f[7], ammo=f[8], flags=f[9], deaths=f[10], aux=f[11],
        events=events,
    )


# ---------------------------------------------------------------- GNU Radio helpers
def to_pdu(data: bytes, meta=None):
    """bytes -> PMT PDU (meta . u8vector), for message_port_pub in a GNU Radio block."""
    import pmt
    return pmt.cons(meta if meta is not None else pmt.PMT_NIL,
                    pmt.init_u8vector(len(data), list(data)))


def from_pdu(msg) -> bytes:
    """PMT PDU (or bare u8vector) -> bytes."""
    import pmt
    vec = pmt.cdr(msg) if pmt.is_pair(msg) else msg
    return bytes(pmt.u8vector_elements(vec))


# ---------------------------------------------------------------- CLI / self-test
def _selftest() -> None:
    s = PlayerState(pid=1, seq=65535, x=12.34, y=0.5, z=-3.21, yaw=math.radians(270),
                    pitch=math.radians(-12.5), hp=73, ammo=17,
                    flags=FLAG_FIRED | FLAG_RELOADING, deaths=4, aux=9,
                    events=[Event(500, 0, 20), Event(501, 0, 50)])
    b = build_payload(s)
    assert len(b) == PAYLOAD_LEN
    r = parse_payload(b + b"\x00" * 19)               # padded frame still parses
    assert (r.pid, r.seq, r.hp, r.ammo, r.flags, r.deaths, r.aux) == (1, 65535, 73, 17, 3, 4, 9)
    assert abs(r.x - 12.34) < 0.006 and abs(r.z + 3.21) < 0.006
    assert abs(math.degrees(r.yaw) - 270) < 0.01 and abs(math.degrees(r.pitch) + 12.5) < 0.01
    assert [(e.eid, e.target, e.damage) for e in r.events] == [(500, 0, 20), (501, 0, 50)]
    print("payload self-test OK  (%d bytes)  %s" % (len(b), b.hex()))


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "--hex":      # decode a captured frame
        print(parse_payload(bytes.fromhex("".join(sys.argv[2:]))))
    else:
        _selftest()
