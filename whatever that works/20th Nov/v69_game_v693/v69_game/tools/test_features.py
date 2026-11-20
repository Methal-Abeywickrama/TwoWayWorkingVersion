#!/usr/bin/env python3
"""
test_features.py - tests of the v4 features: elevation (stairs, roofs, blocks), healing (H, timer,
cooldown, medkit pickups and drops), and the weapons (rifle, sniper, knife), through the real network code.

    python3 tools/test_features.py
"""
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "game"))
import game_core as gc                                              # noqa: E402
from test_core import DT, PASS, Net, check, place, run              # noqa: E402
from payload import W_KNIFE, W_RIFLE, W_SNIPER                      # noqa: E402


def walk(f, yaw, secs, jump_at=None, stop=None):
    f.yaw = yaw % (2 * math.pi)
    for i in range(int(secs / DT)):
        f.update(DT, fwd=1.0, jump=(jump_at is not None and i == int(jump_at / DT)))
        if stop and stop(f):
            break


WEST, EAST, NORTH, SOUTH = -math.pi / 2, math.pi / 2, 0.0, math.pi       # yaw: north = -z


def t_geometry():
    print("elevation: stairs, roofs, blocks, rays")
    f = gc.Fighter(0)
    f.respawn_at(15.0, 5.0)                                     # ground east of building A (3.0 m)
    walk(f, WEST, 1.6)
    check("stairs lead up to the 3.0 m roof", abs(f.y - 3.0) < 0.01 and f.x < 10.0, f"x {f.x:.1f} y {f.y:.2f}")
    walk(f, WEST, 3.0)
    check("walking off the roof edge falls down to the ground", f.y == 0.0 and f.x < 4.0, f"x {f.x:.1f} y {f.y:.2f}")
    f.respawn_at(27.0, 29.0)                                    # south of building B's stairs
    walk(f, NORTH, 2.2)
    check("the other stairs lead up to the 2.0 m roof", abs(f.y - 2.0) < 0.01, f"z {f.z:.1f} y {f.y:.2f}")
    f.respawn_at(5.0, 11.0)                                     # ground south of building A
    walk(f, NORTH, 1.5)
    check("the 3.0 m building wall can't be walked up", f.y == 0.0 and f.z > 10.0, f"z {f.z:.2f}")
    f.respawn_at(5.0, 11.0)
    walk(f, NORTH, 1.5, jump_at=0.05)
    check("... nor jumped up", f.y < 0.01 and f.z > 10.0, f"z {f.z:.2f} y {f.y:.2f}")
    f.respawn_at(21.0, 21.5)                                    # 1.0 m crate at row 8 col 10 (z 16..18)
    walk(f, NORTH, 1.5, jump_at=0.4, stop=lambda q: q.z < 17.0 and q.on_ground)
    check("a 1.0 m crate can be jumped onto", abs(f.y - 1.0) < 0.01, f"z {f.z:.2f} y {f.y:.2f}")
    f.respawn_at(21.0, 21.5)
    walk(f, NORTH, 1.5)
    check("... but not walked onto", f.y == 0.0 and f.z > 18.0, f"z {f.z:.2f}")
    k1, _ = gc.wall_hit(1.0, 1.6, 9.0, (1.0, 0.0, 0.0), 30.0)
    k2, _ = gc.wall_hit(1.0, 4.4, 9.0, (1.0, 0.0, 0.0), 30.0)
    check("a shot at 1.6 m hits the 4 m wall, at 4.4 m it flies over", k1 == "wall" and k2 == "none", f"{k1}, {k2}")
    k, t = gc.wall_hit(5.0, 6.0, 5.0, (0.0, -1.0, 0.0), 10.0)
    check("a shot down onto the roof stops on the roof", k == "floor" and abs(t - 3.0) < 1e-6, f"{k} at {t:.2f} m")
    k, t = gc.wall_hit(15.0, 0.5, 5.0, (-1.0, 0.0, 0.0), 10.0)
    check("the stairs stop a low shot", k in ("wall", "floor") and 1.0 < t < 3.5, f"{k} at {t:.2f} m")
    path = gc.nav_path((1, 7), (3, 3))
    check("bots find the way up the stairs", path is not None and path[-1] == (3, 3), str(path))


def _arena(rate=10, seed=1):
    net = Net(base=0.05, seed=seed)
    nodes = net.make([0, 1], rate=rate, max_lag=1.5)
    run(net, nodes, 1.5)
    nodes[0].join_game()
    run(net, nodes, 3)
    nodes[1].join_game()
    run(net, nodes, 3)
    return net, nodes, nodes[0], nodes[1]


def _put_client(h, c, x, z):
    place(h, 1, x, z)
    q = h.world.players[1]
    q.y = gc.floor_at(x, z)
    c.me.x, c.me.z, c.me.y = x, z, q.y
    c.corr_seq = (c.seq - 1) & 0xFFFF


def t_climb_net():
    print("elevation over the network: the host accepts stairs, refuses flying")
    net, nodes, h, c = _arena()
    _put_client(h, c, 15.0, 5.0)
    run(net, nodes, 0.5)
    c0 = h.world.stats["moves_corrected"]

    def drive(n):
        n.me.yaw = WEST % (2 * math.pi)
        n.step(DT, fwd=1.0 if n.me.x > 7.0 else 0.0)
    run(net, nodes, 3.0, drivers={1: drive})
    q = h.world.players[1]
    check("client walked up the stairs onto the roof; the host agrees", abs(q.y - 3.0) < 0.05 and
          h.world.stats["moves_corrected"] == c0, f"host y {q.y:.2f}, corrections {h.world.stats['moves_corrected'] - c0}")
    _put_client(h, c, 15.0, 9.0)                                # ground, 2 m south-east of the roof edge
    run(net, nodes, 0.5)
    c.me.x, c.me.z, c.me.y = 9.0, 7.0, 3.0                      # "teleports" up onto the roof
    run(net, nodes, 1.5)
    q = h.world.players[1]
    check("a client that claims to be on the roof without climbing is corrected", q.y < 0.1 and c.me.y < 0.1 and
          math.hypot(c.me.x - q.x, c.me.z - q.z) < 1.0, f"host ({q.x:.1f}, {q.y:.1f}, {q.z:.1f}) "
          f"client ({c.me.x:.1f}, {c.me.y:.1f}, {c.me.z:.1f})")


def t_heal():
    print("healing: H, the timer, the cooldown, firing stops it")
    net, nodes, h, c = _arena()
    _put_client(h, c, 21.0, 3.0)
    run(net, nodes, 0.3)
    q = h.world.players[1]
    q.hp = 40
    run(net, nodes, 0.5)
    check("client starts with one medkit", c.me.medkits == 1 and q.medkits == 1, f"{c.me.medkits} / {q.medkits}")
    ok = c.heal()
    run(net, nodes, 1.0)
    mid = q.hp
    run(net, nodes, 3.0)
    check("H heals 50 HP over 3 s", ok and 45 < mid < 75 and q.hp == 90, f"hp after 1 s {mid}, after 4 s {q.hp}")
    check("the medkit is used up (host and client)", q.medkits == 0 and c.me.medkits == 0)
    check("the HUD timer counts the cooldown", 0 < c.me.heal_cd <= gc.HEAL_COOLDOWN - 3.5, f"{c.me.heal_cd:.1f} s")
    q.medkits = 1
    run(net, nodes, 0.5)
    refused0 = h.world.stats["heals_refused"]
    c.me.heal_cd = 0                                            # a client that ignores its own cooldown timer
    c.me.hp = 50
    q.hp = 50
    c.heal()
    run(net, nodes, 1.5)
    check("the host refuses a heal before the cooldown is over", h.world.stats["heals_refused"] > refused0 and
          q.hp == 50 and q.medkits == 1 and c.me.medkits == 1, f"hp {q.hp}, medkits {q.medkits}")
    run(net, nodes, gc.HEAL_COOLDOWN)
    c.heal()
    run(net, nodes, 1.0)
    c.me.fire_cd = 0
    c.fire()
    run(net, nodes, 3.0)
    check("firing stops the heal (the rest of the medkit is lost)", 50 < q.hp < 85 and q.medkits == 0,
          f"hp {q.hp}")


def t_pickups():
    print("medkits: spots, pickups, drops")
    net, nodes, h, c = _arena()
    q = h.world.players[1]
    q.medkits = c.me.medkits = 0
    run(net, nodes, 0.5)
    n_before = sum(1 for p in c.pickups if p[3] == "spot")
    x, z = gc.cell_center(*gc.MEDKIT_SPOTS[2])
    _put_client(h, c, x + 0.3, z)
    run(net, nodes, 1.0)
    n_after = sum(1 for p in c.pickups if p[3] == "spot")
    check("walking over a medkit picks it up", q.medkits == 1 and c.me.medkits == 1, f"{q.medkits} / {c.me.medkits}")
    check("... and it disappears from every screen", n_before == len(gc.MEDKIT_SPOTS) and n_after == n_before - 1,
          f"{n_before} -> {n_after}")
    x, z = gc.cell_center(*gc.MEDKIT_SPOTS[0])                  # on top of building A
    _put_client(h, c, x, z)
    run(net, nodes, 1.0)
    check("the medkit on the roof can be picked up too", q.medkits == 2)
    _put_client(h, c, 21.0, 3.0)
    run(net, nodes, 0.5)
    q.hp = 1
    hp0 = h.world.players[0]
    h.me.x, h.me.z, h.me.y = 15.0, 3.0, 0.0
    hp0.x, hp0.z = 15.0, 3.0
    h.me.yaw = EAST % (2 * math.pi)
    h.me.pitch = math.atan2(1.2 - gc.EYE_H, 6.0)
    run(net, nodes, 0.3)
    h.me.fire_cd = 0
    h.fire()
    run(net, nodes, 0.6)
    drops = [p for p in c.pickups if p[3] == "drop"]
    check("an eliminated player drops a medkit where it fell", not q.alive and len(h.world.drops) == 1 and len(drops) == 1
          and math.hypot(drops[0][0] - 21.0, drops[0][2] - 3.0) < 0.5, f"{drops}")


def t_weapons():
    print("weapons: sniper and knife, judged by the host")
    net, nodes, h, c = _arena()
    hp0 = h.world.players[0]
    h.me.x, h.me.z, h.me.y = 21.0, 3.0, 0.0
    hp0.x, hp0.z = 21.0, 3.0
    _put_client(h, c, 11.0, 3.0)                                # 10 m west, open row 1

    def aim(n, tx, tz, err=0.0):
        n.me.yaw = (math.atan2(tx - n.me.x, -(tz - n.me.z)) + err) % (2 * math.pi)
        n.me.pitch = math.atan2(1.15 - gc.EYE_H, math.hypot(tx - n.me.x, tz - n.me.z))
    run(net, nodes, 0.5)
    c.switch_weapon(W_SNIPER)
    run(net, nodes, 0.6)
    aim(c, 21.0, 3.0)
    c.set_scope(True)
    hits = h.world.stats["hits"]
    evs = []
    c.on_event = lambda n, e: evs.append(e)
    c.fire(pressed=True)
    run(net, nodes, 0.8)
    check("scoped sniper body shot: 80 damage", hp0.hp == 20 and h.world.stats["hits"] == hits + 1, f"hp {hp0.hp}")
    check("the client's hit event names the sniper (kill feed)", any(e.weapon == W_SNIPER and e.damage == 80
                                                                     for e in evs), str(evs))
    hp0.hp = h.me.hp = 100
    rate0 = h.world.stats["rej_rate"]
    for _ in range(2):                                          # a client that ignores the bolt action
        c.me.fire_cd = 0
        c.fire(pressed=True)
        run(net, nodes, 0.2)
    run(net, nodes, 0.6)
    check("a second sniper shot after 0.2 s is refused by the host", h.world.stats["rej_rate"] == rate0 + 1 and
          hp0.hp == 20, f"hp {hp0.hp}, rate rejects {h.world.stats['rej_rate'] - rate0}")
    hp0.hp = h.me.hp = 100
    c.switch_weapon(W_KNIFE)
    _put_client(h, c, 19.2, 3.0)
    run(net, nodes, 0.6)
    aim(c, 21.0, 3.0, err=0.35)                                 # 1.8 m away, aim 20 deg off
    c.fire(pressed=True)
    run(net, nodes, 0.6)
    check("knife at 1.8 m hits even 20 degrees off target (55)", hp0.hp == 45, f"hp {hp0.hp}")
    _put_client(h, c, 17.0, 3.0)
    run(net, nodes, 0.6)
    aim(c, 21.0, 3.0)
    c.me.fire_cd = 0
    c.fire(pressed=True)
    run(net, nodes, 0.6)
    check("knife at 4 m doesn't reach", hp0.hp == 45, f"hp {hp0.hp}")
    c.switch_weapon(W_RIFLE)
    run(net, nodes, 0.6)
    check("the host knows the weapon in hand", h.world.players[1].weapon == W_RIFLE and
          h.views[1].weapon == W_RIFLE)


if __name__ == "__main__":
    for t in (t_geometry, t_climb_net, t_heal, t_pickups, t_weapons):
        t()
    print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
    sys.exit(0 if all(PASS) else 1)
