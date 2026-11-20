#!/usr/bin/env python3
"""
test_payload_c.py - builds the C payload library and checks that it produces exactly the same bytes as
game/payload.py for thousands of random BEACON / INPUT / SNAPSHOT messages.

    python3 tools/test_payload_c.py          (needs gcc)
"""
import ctypes as C
import os
import random
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.join(HERE, "..", "game")
sys.path.insert(0, GAME)
from payload import (Action, Beacon, Drop, Input, PeerInfo, SnapEvent, SnapPlayer, Snapshot,  # noqa: E402
                     build_payload, parse_payload)

lib_path = os.path.join(tempfile.mkdtemp(), "libgamepayload.so")
subprocess.check_call(["gcc", "-O2", "-shared", "-fPIC", os.path.join(GAME, "c", "payload.c"), "-o", lib_path, "-lm"])
lib = C.CDLL(lib_path)


class Peer(C.Structure):
    _fields_ = [("id", C.c_uint8), ("echo", C.c_uint8), ("hold_ms", C.c_int), ("rtt_ms", C.c_int)]


class Bea(C.Structure):
    _fields_ = [("node", C.c_uint8), ("seq", C.c_uint16), ("ts", C.c_uint16), ("flags", C.c_uint8),
                ("name", C.c_char * 9), ("rate", C.c_double), ("max_players", C.c_uint8), ("max_lag", C.c_double),
                ("host", C.c_uint8), ("epoch", C.c_uint8), ("n_peers", C.c_uint8), ("peers", Peer * 7)]


class Act(C.Structure):
    _fields_ = [("aid", C.c_uint8), ("kind", C.c_uint8), ("dt_ms", C.c_int), ("yaw", C.c_double),
                ("pitch", C.c_double), ("view_seq", C.c_uint16), ("view_next", C.c_uint8), ("view_frac", C.c_uint8),
                ("pos_known", C.c_int), ("dx", C.c_double), ("dz", C.c_double), ("weapon", C.c_uint8)]


class Inp(C.Structure):
    _fields_ = [("node", C.c_uint8), ("seq", C.c_uint16), ("ts", C.c_uint16), ("host", C.c_uint8),
                ("flags", C.c_uint8), ("x", C.c_double), ("y", C.c_double), ("z", C.c_double), ("yaw", C.c_double),
                ("pitch", C.c_double), ("snap_ack", C.c_uint16), ("snap_hold_ms", C.c_int), ("n_actions", C.c_uint8),
                ("actions", Act * 3)]


class SP(C.Structure):
    _fields_ = [("id", C.c_uint8), ("x", C.c_double), ("y", C.c_double), ("z", C.c_double), ("yaw", C.c_double),
                ("pitch", C.c_double), ("hp", C.c_uint8), ("flags", C.c_uint8), ("kills", C.c_uint8),
                ("deaths", C.c_uint8), ("ack", C.c_uint8), ("hold_ms", C.c_int), ("gear", C.c_uint8)]


class Ev(C.Structure):
    _fields_ = [("eid", C.c_uint8), ("shooter", C.c_uint8), ("victim", C.c_uint8), ("damage", C.c_uint8),
                ("head", C.c_uint8), ("kill", C.c_uint8), ("weapon", C.c_uint8)]


class Dr(C.Structure):
    _fields_ = [("id", C.c_uint8), ("x", C.c_double), ("z", C.c_double)]


class Sn(C.Structure):
    _fields_ = [("node", C.c_uint8), ("seq", C.c_uint16), ("ts", C.c_uint16), ("epoch", C.c_uint8),
                ("flags", C.c_uint8), ("max_players", C.c_uint8), ("n_players", C.c_uint8), ("n_events", C.c_uint8),
                ("players", SP * 8), ("events", Ev * 3), ("spots", C.c_uint8), ("n_drops", C.c_uint8),
                ("drops", Dr * 4)]


def main(n_iter=6000):
    R = random.Random(5)
    bad = 0
    buf = (C.c_uint8 * 200)()
    for _ in range(n_iter):
        k = R.randrange(8)
        peers = [PeerInfo(R.randrange(255), R.randrange(256), R.randrange(1100), R.choice([-1, R.randrange(1100)]))
                 for _ in range(k)]
        nm = "".join(R.choice("abXY09_") for _ in range(R.randrange(9)))
        b = Beacon(R.randrange(255), R.randrange(65536), R.randrange(65536), R.randrange(64), nm,
                   R.randrange(1, 101) / 10, R.randrange(2, 9), R.randrange(0, 250) / 100, R.randrange(256),
                   R.randrange(256), peers)
        cb = Bea(b.node, b.seq, b.ts, b.flags, nm.encode(), b.rate, b.max_players, b.max_lag, b.host, b.epoch, k)
        for j, p in enumerate(peers):
            cb.peers[j] = Peer(p.id, p.echo, p.hold, p.rtt)
        L = lib.gp_build_beacon(C.byref(cb), buf)
        bad += bytes(buf[:L]) != build_payload(b)

        acts = []
        for _ in range(R.randrange(4)):
            known = R.random() < 0.8
            acts.append(Action(R.randrange(256), R.choice([1, 2, 3]), -R.randrange(2000), R.uniform(-9, 9),
                               R.uniform(-1.6, 1.6), R.randrange(65536), R.randrange(4), R.randrange(256),
                               R.uniform(-6, 6) if known else None, R.uniform(-6, 6) if known else None,
                               R.randrange(3)))
        i = Input(R.randrange(255), R.randrange(65536), R.randrange(65536), R.randrange(256), R.randrange(256),
                  R.uniform(-5, 40), R.uniform(0, 5), R.uniform(-5, 40), R.uniform(-9, 9), R.uniform(-1.6, 1.6),
                  R.randrange(65536), R.randrange(1100), acts)
        ci = Inp(i.node, i.seq, i.ts, i.host, i.flags, i.x, i.y, i.z, i.yaw, i.pitch, i.snap_ack, i.snap_hold, len(acts))
        for j, a in enumerate(acts):
            ci.actions[j] = Act(a.aid, a.kind, a.dt, a.yaw, a.pitch, a.view_seq, a.view_next, a.view_frac,
                                a.dx is not None, a.dx or 0.0, a.dz or 0.0, a.weapon)
        L = lib.gp_build_input(C.byref(ci), buf)
        bad += bytes(buf[:L]) != build_payload(i)

        pl = [SnapPlayer(R.randrange(255), R.uniform(-1, 42), R.uniform(-0.2, 5.5), R.uniform(-1, 42), R.uniform(-9, 9),
                         R.uniform(-1.6, 1.6), R.randrange(101), R.randrange(128), R.randrange(256), R.randrange(256),
                         R.randrange(256), R.randrange(1100), R.randrange(32)) for _ in range(R.randrange(9))]
        ev = [SnapEvent(R.randrange(256), R.randrange(255), R.randrange(255), R.choice([20, 50, 56, 80, 150]),
                        R.random() < .5, R.random() < .5, R.randrange(3)) for _ in range(R.randrange(4))]
        dr = [Drop(R.randrange(256), R.uniform(-1, 33), R.uniform(-1, 33)) for _ in range(R.randrange(5))]
        s = Snapshot(R.randrange(255), R.randrange(65536), R.randrange(65536), R.randrange(256), R.randrange(4),
                     R.randrange(2, 9), pl, ev, R.randrange(256), dr)
        cs = Sn(s.node, s.seq, s.ts, s.epoch, s.flags, s.max_players, len(pl), len(ev))
        for j, p in enumerate(pl):
            cs.players[j] = SP(p.id, p.x, p.y, p.z, p.yaw, p.pitch, p.hp, p.flags, p.kills, p.deaths, p.ack, p.hold,
                               p.gear)
        for j, e in enumerate(ev):
            cs.events[j] = Ev(e.eid, e.shooter, e.victim, e.damage, e.head, e.kill, e.weapon)
        cs.spots, cs.n_drops = s.spots, len(dr)
        for j, d in enumerate(dr):
            cs.drops[j] = Dr(d.id, d.x, d.z)
        L = lib.gp_build_snapshot(C.byref(cs), buf)
        raw = build_payload(s)
        bad += bytes(buf[:L]) != raw
        # C parse -> C build gives the same bytes again
        cs2 = Sn()
        bad += lib.gp_parse_snapshot(buf, C.c_size_t(L), C.byref(cs2)) != 0
        L2 = lib.gp_build_snapshot(C.byref(cs2), buf)
        bad += bytes(buf[:L2]) != raw
        ci2 = Inp()
        raw_i = build_payload(i)
        C.memmove(buf, raw_i, len(raw_i))
        bad += lib.gp_parse_input(buf, C.c_size_t(len(raw_i)), C.byref(ci2)) != 0
        bad += bytes(buf[:lib.gp_build_input(C.byref(ci2), buf)]) != raw_i
        for m in (b, i, s):
            raw = build_payload(m)
            bad += build_payload(parse_payload(raw)) != raw
    print(f"{3 * n_iter} random messages: C vs Python and Python round trip -> {bad} mismatches")
    return bad


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
