#!/usr/bin/env python3
"""
test_core.py - functional tests of the host-authoritative game core (no radio, no graphics).

    python3 tools/test_core.py

Each test builds a few Nodes on a simulated network with a delay per direction, and checks one feature:
election (lowest ping, random, conflict), joining, arena full, host migration, lag compensation (rewind),
late shots, lagging targets, prediction / reconciliation, and the action ack / repeat path.
"""
import math
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "game"))
import game_core as gc                                                     # noqa: E402
from game_core import BCAST, BotBrain, Node                                # noqa: E402

DT = 1 / 60


class Net:
    """Every node hears every frame. delay[(src, dst)] in s (default `base`), optional loss, cuts."""

    def __init__(self, base=0.05, loss=0.0, seed=1):
        self.base, self.loss = base, loss
        self.delay = {}
        self.cut = set()                 # (src, dst) pairs that hear nothing
        self.dead = set()                # nodes that are switched off
        self.rng = random.Random(seed)
        self.now = 0.0
        self.q = []                      # (t, src, dst, data)
        self.sent = 0

    class Ep:
        def __init__(self, net, node):
            self.net, self.node, self.desc = net, node, "test"

        def send(self, data, dest):
            n = self.net
            if self.node in n.dead:
                return
            n.sent += 1
            for other in n.nodes:
                if other == self.node or (dest != BCAST and dest != other) or (self.node, other) in n.cut:
                    continue
                if n.rng.random() < n.loss:
                    continue
                n.q.append((n.now + n.delay.get((self.node, other), n.base), self.node, other, bytes(data)))

        def poll(self):
            n = self.net
            if self.node in n.dead:
                return []
            out = [(s, d) for t, s, dst, d in n.q if dst == self.node and t <= n.now]
            n.q = [m for m in n.q if not (m[2] == self.node and m[0] <= n.now)]
            return out

    def make(self, ids, **kw):
        self.nodes = list(ids)
        return {i: Node(i, Net.Ep(self, i), name=f"N{i}", clock_offset=13.7 * i, **kw) for i in ids}


def run(net, nodes, secs, drivers=None, every=None):
    for i in range(int(secs / DT)):
        net.now += DT
        for k, n in nodes.items():
            if k in net.dead:
                continue
            if drivers and k in drivers:
                drivers[k](n)
            else:
                n.step(DT)
        if every:
            every(i)


PASS = []


def check(name, cond, info=""):
    PASS.append(bool(cond))
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f"   ({info})" if info else ""))


def roles(nodes):
    return {k: (n.role, n.host_id) for k, n in nodes.items()}


# ---------------------------------------------------------------------------------------------------------
def t_election_lowest_ping():
    print("election: lowest average ping wins")
    net = Net(base=0.05)
    nodes = net.make([0, 1, 2])
    # node 2 is close to both others (40 ms one way), 0 <-> 1 are far apart (200 ms)
    for a, b, d in ((0, 1, 0.20), (1, 0, 0.20), (0, 2, 0.02), (2, 0, 0.02), (1, 2, 0.02), (2, 1, 0.02)):
        net.delay[(a, b)] = d
    run(net, nodes, 6)                                    # lobby: beacons measure the RTTs
    for n in nodes.values():
        n.join_game()
    run(net, nodes, 6)
    r = roles(nodes)
    check("every node agrees, node 2 hosts", all(v == ("host" if k == 2 else "client", 2) for k, v in r.items()), r)
    check("both clients admitted", nodes[0].admitted and nodes[1].admitted)


def t_election_random():
    print("election: random mode, same pick everywhere")
    picks = set()
    for seed in range(6):
        net = Net(base=0.05, seed=seed)
        nodes = net.make([3, 5, 9], elect_random=True)
        run(net, nodes, 3)
        for n in nodes.values():
            n.elect_round = seed
            n.join_game()
        run(net, nodes, 6)
        hosts = {n.host_id for n in nodes.values()}
        picks |= hosts
        if len(hosts) != 1:
            check(f"seed {seed}: one host", False, roles(nodes))
            return
    check("one host per game, and different games pick different hosts", len(picks) >= 2, f"hosts seen {sorted(picks)}")


def t_join_running_and_full():
    print("joining a running arena; arena full")
    net = Net(base=0.04)
    nodes = net.make([0, 1, 2], max_players=2)
    run(net, nodes, 2)
    nodes[0].join_game()
    run(net, nodes, 4)
    nodes[1].join_game()
    run(net, nodes, 3)
    check("node 0 hosts, node 1 joined", nodes[0].role == "host" and nodes[1].admitted)
    ok, why = nodes[2].join_game()
    check("node 2 refused: arena full (2/2)", not ok and "full" in why.lower(), why)


def t_migration():
    print("host migration: the host disappears, a client takes over, scores are kept")
    net = Net(base=0.05)
    nodes = net.make([0, 1, 2])
    bots = {k: BotBrain(n, seed=k) for k, n in nodes.items()}
    run(net, nodes, 2)
    for n in nodes.values():
        n.join_game()
    run(net, nodes, 40, drivers={k: (lambda n, b=bots[k]: b.step(DT)) for k in nodes})
    host = next(k for k, n in nodes.items() if n.role == "host")
    others = [k for k in nodes if k != host]
    before = {k: (nodes[k].me.kills, nodes[k].me.deaths) for k in others}
    net.dead.add(host)
    t_cut = net.now
    took = []

    def watch(i):
        if not took and any(nodes[k].role == "host" for k in others) and \
                all(nodes[k].role in ("host", "client") and nodes[k].admitted for k in others):
            took.append(net.now - t_cut)
    run(net, {k: nodes[k] for k in others}, 15, drivers={k: (lambda n, b=bots[k]: b.step(DT)) for k in others},
        every=watch)
    new = [k for k in others if nodes[k].role == "host"]
    check("exactly one new host among the survivors", len(new) == 1, roles(nodes))
    check("the other survivor is its client", all(nodes[k].host_id == new[0] for k in others if k != new[0]))
    after = {k: (nodes[k].me.kills, nodes[k].me.deaths) for k in others}
    check("kills / deaths carried over", all(after[k][0] >= before[k][0] and after[k][1] >= before[k][1] for k in others),
          f"before {before} after {after}")
    check("migration finished within PEER_TIMEOUT + 3 s", took and took[0] < gc.PEER_TIMEOUT + 3.0,
          f"{took[0]:.1f} s" if took else "never")


def t_conflict():
    print("two hosts meet (network split heals): the better one stays")
    net = Net(base=0.04)
    nodes = net.make([0, 1, 2, 3])
    for a in (0, 1):
        for b in (2, 3):
            net.cut |= {(a, b), (b, a)}
    run(net, nodes, 2)
    for n in nodes.values():
        n.join_game()
    run(net, nodes, 6)
    hosts = sorted(k for k, n in nodes.items() if n.role == "host")
    check("split network: two arenas", hosts == [0, 2], roles(nodes))
    net.cut.clear()
    run(net, nodes, 10)
    hosts = sorted(k for k, n in nodes.items() if n.role == "host")
    check("healed: one host, everyone in its arena", len(hosts) == 1 and
          all(n.role == "client" and n.host_id == hosts[0] and n.admitted for k, n in nodes.items() if k != hosts[0]),
          roles(nodes))


def place(host, pid, x, z):
    """Test helper: teleport a player in the host's world (and the host's own fighter)."""
    q = host.world.players[pid]
    q.x, q.z, q.last_ct = x, z, None
    q.track.clear()
    if pid == host.node:
        host.me.x, host.me.z = x, z


def _duel(delay, max_lag=0.8, rewind=True, rate=5.0, secs=8.0, moving=True, seed=1):
    """Host 0 stands still and strafes left/right; client 1 aims at what it sees and fires.
    Returns (hits applied, shots fired, reject counts)."""
    net = Net(base=delay, seed=seed)
    nodes = net.make([0, 1], rate=rate, max_lag=max_lag)
    run(net, nodes, 1.5)
    nodes[0].join_game()
    run(net, nodes, 3)
    nodes[1].join_game()
    run(net, nodes, 3)
    h, c = nodes[0], nodes[1]
    w = h.world
    if not rewind:                                       # ablation: judge every shot at "now"
        orig = w.fire
        w.fire = lambda p, o, d, tv, now, view=None, weapon=0: orig(p, o, d, now, now, None, weapon)
    place(h, 0, *gc.cell_center(1, 6))                 # target strafes along row 1 (x 13..18 m)
    place(h, 1, *gc.cell_center(3, 6))                 # shooter 4 m away, looking across that path
    c.me.x, c.me.z = gc.cell_center(3, 6)
    c.corr_seq = (c.seq - 1) & 0xFFFF
    hits0 = w.stats["hits"]
    shots0 = w.stats["shots"]
    t = [0.0]

    def drive_host(n):
        t[0] += DT
        s = math.sin(t[0] * 2.0) if moving else 0.0
        n.me.yaw = 0.0                                   # faces -z: strafing moves along the open row 1
        n.step(DT, strafe=s)                             # strafes at up to 5 m/s across the client's view
        n.me.hp = 100
        w.players[0].hp = 100

    def drive_client(n):
        v = n.views.get(0)
        firing = False
        if v is not None and v.placed:
            n.me.yaw = math.atan2(v.x - n.me.x, -(v.z - n.me.z))
            n.me.pitch = math.atan2(1.2 - gc.EYE_H, math.hypot(v.x - n.me.x, v.z - n.me.z))
            firing = True
        n.me.ammo = 30
        n.step(DT, firing=firing)
    run(net, nodes, secs, drivers={0: drive_host, 1: drive_client})
    st = w.stats
    return st["hits"] - hits0, st["shots"] - shots0, {k: v for k, v in st.items() if k.startswith("rej") and v}


def t_lag_compensation():
    print("lag compensation: the client shoots what it sees, the host rewinds")
    for delay in (0.05, 0.15, 0.30):
        h_on, s_on, r_on = _duel(delay, rewind=True, max_lag=1.5)
        h_off, s_off, r_off = _duel(delay, rewind=False, max_lag=1.5)
        s_on -= r_on.get("rej_reloading", 0)            # the test gives the client endless ammo; the host
        s_off -= r_off.get("rej_reloading", 0)          # makes it reload every 30 shots: not counted
        check(f"one-way delay {delay * 1000:.0f} ms: rewind {h_on}/{s_on} hits, without rewind {h_off}/{s_off}",
              h_on / max(1, s_on) > 0.75 and h_on > 1.5 * h_off)


def t_late_rejected():
    print("shots judged at a view older than max_lag are rejected")
    hits, shots, rej = _duel(0.45, max_lag=0.8)       # view age ~ 0.45 + 1.6/5 + 0.1 + 0.45 > 0.8
    check("one-way 450 ms (view age ~1.3 s), max_lag 800 ms: (almost) everything rejected as late",
          rej.get("rej_late", 0) > 0.8 * shots and hits < 0.2 * shots, f"{hits}/{shots}, {rej}")
    hits, shots, rej = _duel(0.45, max_lag=2.0)
    shots -= rej.get("rej_reloading", 0)
    check("same link, max_lag 2 s: accepted", hits > 0.75 * shots, f"{hits}/{shots}, {rej}")


def t_lagging_target():
    print("a player whose inputs stopped (lagging) can't be hit at its frozen position")
    net = Net(base=0.04)
    nodes = net.make([0, 1, 2], max_lag=0.8)
    run(net, nodes, 1.5)
    nodes[0].join_game()
    run(net, nodes, 3)
    nodes[1].join_game()
    nodes[2].join_game()
    run(net, nodes, 3)
    h = nodes[0]
    place(h, 0, *gc.cell_center(1, 3))
    place(h, 2, *gc.cell_center(1, 10))
    nodes[2].me.x, nodes[2].me.z = gc.cell_center(1, 10)
    net.cut |= {(2, 0)}                                  # node 2's packets no longer reach the host
    run(net, nodes, 2.0)
    q = h.world.players[2]
    lag = h.world.lagging(q, h.now)
    me = h.me
    me.yaw = math.atan2(q.x - me.x, -(q.z - me.z))
    me.pitch = math.atan2(q.y + 1.2 - (me.y + gc.EYE_H), math.hypot(q.x - me.x, q.z - me.z))
    hp0 = q.hp
    for _ in range(20):
        me.fire_cd = 0
        h.fire()
    check("frozen player marked lagging", lag)
    check("host's shots at it do no damage", q.hp == hp0, f"hp {q.hp}")
    check("counted as 'lagging_target'", h.world.stats["lagging_target"] > 0, h.world.stats["lagging_target"])


def t_prediction():
    print("client prediction + reconciliation")
    net = Net(base=0.10, loss=0.05)
    nodes = net.make([0, 1], rate=5)
    bots = {k: BotBrain(n, seed=k) for k, n in nodes.items()}
    run(net, nodes, 1.5)
    nodes[0].join_game()
    run(net, nodes, 3)
    nodes[1].join_game()
    run(net, nodes, 30, drivers={k: (lambda n, b=bots[k]: b.step(DT)) for k in nodes})
    c, h = nodes[1], nodes[0]
    check("normal play: host accepts the client's moves (no corrections)",
          h.world.stats["moves_corrected"] <= 2 and c.stats["corrections"] <= 2,
          f"host corrected {h.world.stats['moves_corrected']}, client corrections {c.stats['corrections']}")
    # teleport hack / desync: the client jumps 6 m at once
    run(net, nodes, 4)                                  # bots stopped: a client eliminated just now respawns
    c.me.x, c.me.z = gc.cell_center(1, 1)
    run(net, nodes, 0.4)
    c.me.x, c.me.z = gc.cell_center(1, 4)
    run(net, nodes, 1.5)
    q = h.world.players[1]
    check("host refused the jump and the client snapped back",
          h.world.stats["moves_corrected"] > 0 and c.stats["corrections"] > 0 and
          math.hypot(c.me.x - q.x, c.me.z - q.z) < 1.0,
          f"client at ({c.me.x:.1f}, {c.me.z:.1f}), host has ({q.x:.1f}, {q.z:.1f})")


def t_action_repeat():
    print("fire actions survive packet loss (repeated until the host acknowledges them)")
    for loss in (0.0, 0.3):
        hits, shots, rej = _duel(0.05, rate=5, seed=4, moving=False) if loss == 0 else (None, None, None)
        net = Net(base=0.05, loss=loss, seed=7)
        nodes = net.make([0, 1], rate=5)
        run(net, nodes, 1.5)
        nodes[0].join_game()
        run(net, nodes, 3)
        nodes[1].join_game()
        run(net, nodes, 4)
        c, h = nodes[1], nodes[0]
        fired = []
        for i in range(40):
            if i % 10 == 0 and c.me.alive:
                c.me.fire_cd = 0
                c.me.ammo = 30
                c.fire()
                fired.append(c.aid)
            run(net, nodes, 0.1)
        run(net, nodes, 1.0)
        got = h.world.stats["shots"]
        check(f"loss {loss:.0%}: host received {got} of {len(fired)} fire actions", got == len(fired))


if __name__ == "__main__":
    for t in (t_election_lowest_ping, t_election_random, t_join_running_and_full, t_migration, t_conflict,
              t_lag_compensation, t_late_rejected, t_lagging_target, t_prediction, t_action_repeat):
        t()
    print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
    sys.exit(0 if all(PASS) else 1)
