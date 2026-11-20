#!/usr/bin/env python3
"""
worstcase_sim.py - Radio Arena over a simulated v6.8 radio channel, worst cases, OLD vs NEW design.

Both games are the real code, driven by bots:
  OLD = the earlier peer-to-peer game (tools/legacy/): every player sends its own state and decides its own
        hits; the victim applies whatever arrives. Every frame queues FIFO in the MAC (no newest-only).
  NEW = this version (game/game_core.py): host election, host-authoritative world with rewind (lag
        compensation), client prediction + reconciliation, late shots rejected, lagging players not
        hittable, newest-only game frames in the MAC.
The radio is tools/radio_model.py: a millisecond-step model of the v6.8 CSMA/CA DCF + SR-ARQ MAC with its
default timing on one shared half-duplex channel (collisions, NAV, Block ACKs, retries, queue limit).

Ground truth: every hit that was applied is checked against the victim's TRUE position (on the victim's own
machine) at the moment the shot was fired.

    python3 tools/worstcase_sim.py                  # all scenarios, 120 s each (a few minutes)
    python3 tools/worstcase_sim.py --quick          # 40 s per run
    python3 tools/worstcase_sim.py --only stall,host_dies --seconds 180
    python3 tools/worstcase_sim.py -o report.md
"""
from __future__ import annotations

import argparse
import collections
import math
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "game"))
sys.path.insert(0, os.path.join(HERE, "legacy"))
sys.path.insert(0, HERE)

import game_core as gc                                          # noqa: E402
import p2p_core as old                                          # noqa: E402
from radio_model import Channel, Mac, Mac69, SimLink, SimLink69  # noqa: E402


def pct(v, q):
    v = sorted(x for x in v if x == x)
    if not v:
        return float("nan")
    return v[min(len(v) - 1, int(q / 100.0 * len(v)))]


def windows(spec, duration_s):
    out = []
    if not spec:
        return out
    every, length, first = spec
    t = first
    while t < duration_s:
        out.append((int(t * 1000), int((t + length) * 1000)))
        t += every
    return out


# ------------------------------------------------------------------ one run
def run(cfg, design, duration_s, seed=1):
    rng = random.Random(seed)
    n = cfg.get("players", 4)
    ms_total = int(duration_s * 1000)
    stalls = {k: windows(v, duration_s) for k, v in cfg.get("stalls", {}).items()}
    deaf = {k: windows(v, duration_s) for k, v in cfg.get("deaf", {}).items()}
    for k, t_off in cfg.get("off", {}).items():           # node switched off for good
        stalls.setdefault(k, []).append((int(t_off * 1000), ms_total + 1))
        deaf.setdefault(k, []).append((int(t_off * 1000), ms_total + 1))
    ch = Channel(random.Random(seed + 7), loss=cfg.get("loss", 0.0),
                 interference=windows(cfg.get("interference"), duration_s), stalls=stalls, deaf=deaf)
    now = [0.0]
    clock = lambda: int(now[0] * 1000)                    # noqa: E731
    macs, players, bots = [], [], []
    max_lag = cfg.get("max_lag", gc.DEFAULT_MAX_LAG)
    for i in range(n):
        if design.startswith("V69"):
            mac = Mac69(i, ch, random.Random(seed * 100 + i))
        else:
            mac = Mac(i, ch, random.Random(seed * 100 + i), newest_only=(design == "NEW"))
        if design == "OLD":
            s = old.Session(i, SimLink(mac, clock), name=f"N{i}", rate=cfg["rate"], max_players=8,
                            arq=cfg["arq"], clock_offset=rng.uniform(0, 60), protect=False)
            b = old.BotBrain(s, seed=seed * 10 + i, skill=0.6)
        elif design.startswith("V69"):
            lk = SimLink69(mac, clock)
            lk.unicast_ok = True
            s = gc.Node(i, lk, name=f"N{i}", rate=cfg["rate"], max_players=8, max_lag=max_lag, arq=cfg["arq"],
                        clock_offset=rng.uniform(0, 60), adaptive=(design == "V69CC"))
            b = gc.BotBrain(s, seed=seed * 10 + i, skill=0.6)
        else:
            s = gc.Node(i, SimLink(mac, clock, kind=lambda d: ("game", d[0])), name=f"N{i}", rate=cfg["rate"],
                        max_players=8, max_lag=max_lag, arq=cfg["arq"], clock_offset=rng.uniform(0, 60))
            b = gc.BotBrain(s, seed=seed * 10 + i, skill=0.6)
        macs.append(mac)
        players.append(s)
        bots.append(b)

    # ---------------- ground truth bookkeeping
    pos = {i: collections.deque(maxlen=8000) for i in range(n)}       # (t, x, y, z, alive)
    fires = {}                       # (shooter, id) -> newest fire with that id (ids wrap at 256)
    all_fires = []
    applied = []
    deaths = []
    rejected = collections.Counter()
    view_ages = []
    host_seq = collections.Counter()
    last_host_fire = {}

    def true_pos(pid, t):
        for rec in reversed(pos[pid]):
            if rec[0] <= t + 1e-9:
                return rec
        return None

    if design == "OLD":
        def on_fire(s, eid, target, origin, d, view, tseq):
            fires[(s.node, eid)] = f = dict(t=now[0], shooter=s.node, origin=origin, d=d, seen={target: view})
            all_fires.append(f)

        def on_hit(s, shooter, eid, dmg, ok, why):
            f = fires.get((shooter, eid))
            if f is None:
                return
            f["delivered"] = True
            if not ok:
                rejected[why] += 1
                f["rejected"] = why
                return
            applied.append(dict(t=now[0], victim=s.node, shooter=shooter, dmg=dmg, fire=f,
                                used=f["seen"].get(s.node), view_age=float("nan")))
            if not s.me.alive:
                deaths.append((now[0], s.node, shooter))
        for s in players:
            s.on_fire, s.on_hit_taken = on_fire, on_hit
    else:
        def on_fire(nd, aid, origin, d, seen, t):
            if aid == -1:
                host_seq[nd.node] += 1
                key = (nd.node, "h", host_seq[nd.node])
                last_host_fire[nd.node] = key
            else:
                key = (nd.node, aid)
            fires[key] = f = dict(t=now[0], shooter=nd.node, origin=origin, d=d, seen=dict(seen))
            all_fires.append(f)

        def on_shot(nd, shooter, aid, why, res, tv, t):
            key = last_host_fire.get(shooter) if aid == -1 else (shooter, aid)
            f = fires.get(key)
            if f is None:
                return
            f["delivered"] = True
            if tv is not None and aid != -1:            # client shots only (the host's own are judged at 0)
                view_ages.append(t - tv)
            if why is not None:
                rejected[why] += 1
                f["rejected"] = why
                return
            kind, vid, dmg, rewound = res
            f["excluded"] = {pid for pid in f["seen"] if pid not in rewound}   # dead / respawned / lagging
            if vid is None:
                return
            applied.append(dict(t=now[0], victim=vid, shooter=shooter, dmg=dmg, fire=f, used=rewound.get(vid),
                                view_age=t - tv))
            if nd.world.players[vid].hp == 0:
                deaths.append((now[0], vid, shooter))
        for s in players:
            s.on_fire, s.on_shot = on_fire, on_shot

    # ---------------- run
    join_at = {i: 2.0 + 0.7 * i for i in range(n)}
    frame_ms = 1000.0 / 60.0
    next_frame = 0.0
    off_at = dict(cfg.get("off", {}))
    for t in range(ms_total):
        now[0] = t / 1000.0
        ch.tick(t)
        for m in macs:
            m.tick(t)
        if t >= next_frame:
            dt = frame_ms / 1000.0
            next_frame += frame_ms
            for i, (s, b) in enumerate(zip(players, bots)):
                if i in off_at and now[0] >= off_at[i]:
                    continue
                if s.mode == "lobby" and now[0] >= join_at[i]:
                    s.join_game()
                b.step(dt)
                me = s.me
                pos[i].append((now[0], me.x, me.y, me.z, me.alive))

    # ---------------- metrics
    on_screen = 0
    on_screen_applied = 0
    on_screen_dead = 0
    applied_keys = {id(a["fire"]) for a in applied}
    for f in all_fires:
        tg = [(pid, p) for pid, p in f["seen"].items() if p is not None]
        if not tg:
            continue
        kind, dist, dmg, key = gc.raycast(*f["origin"], f["d"], targets=tg)
        if key is not None:
            if key in f.get("excluded", ()):           # already dead on the host (or lagging): not a lost hit
                on_screen_dead += 1
                continue
            on_screen += 1
            on_screen_applied += id(f) in applied_keys
    real_shots = 0
    for f in all_fires:
        f["true"] = False
        hits = []
        for pid in range(n):
            if pid == f["shooter"]:
                continue
            tp = true_pos(pid, f["t"])
            if tp and tp[4]:
                th, _ = gc.hit_player(f["origin"], f["d"], (tp[1], tp[2], tp[3]))
                hits.append(th)
        if hits:
            _, wt = gc.wall_hit(*f["origin"], f["d"])
            f["true"] = min(hits) < wt
        real_shots += f["true"]
    for a in applied:
        tp = true_pos(a["victim"], a["fire"]["t"])
        a["disp"] = math.dist((tp[1], tp[3]), (a["used"][0], a["used"][2])) if tp and a["used"] else 0.0
        a["lat"] = a["t"] - a["fire"]["t"]
    late_kills = 0
    ghost_kills = 0
    for td, victim, killer in deaths:
        hs = [a for a in applied if a["victim"] == victim and td - 0.1 <= a["t"] <= td + 0.001]
        if any(a["lat"] > 1.0 for a in hs):
            late_kills += 1
        last = [a for a in hs if a["shooter"] == killer]
        if last and not last[-1]["fire"]["true"] and last[-1]["disp"] > 1.0:
            ghost_kills += 1
    mst = collections.Counter()
    for m in macs:
        mst.update(m.st)
    qdel = [d for m in macs for d in m.qdelay]
    res = dict(
        hits=len(applied),
        ghost=sum(1 for a in applied if not a["fire"]["true"] and a["disp"] > 1.5),
        disp_p95=pct([a["disp"] for a in applied], 95),
        late_hit=sum(1 for a in applied if a["lat"] > 1.0),
        lat_max=max([a["lat"] for a in applied], default=float("nan")),
        deaths=len(deaths), late_kills=late_kills, ghost_kills=ghost_kills,
        real_shots=real_shots,
        real_delivered=sum(1 for f in all_fires if f["true"] and f.get("delivered")),
        real_applied=sum(1 for a in applied if a["fire"]["true"]),
        real_rejected=sum(1 for f in all_fires if f["true"] and f.get("rejected") in ("late", "stale",
                                                                                           "unknown_view")),
        rejected=dict(rejected), on_screen=on_screen, on_screen_applied=on_screen_applied,
        on_screen_dead=on_screen_dead,
        view_p50=pct(view_ages, 50), view_p95=pct(view_ages, 95),
        qmax=max(m.qmax for m in macs), qdel_p50=pct(qdel, 50), qdel_p95=pct(qdel, 95),
        qdel_max=max(qdel, default=0),
        qdrop=mst["qdrop"], replaced=mst["replaced"], tx_D=ch.st["tx_D"], tx_BA=ch.st["tx_BA"],
        coll=ch.st["collided_D"] + ch.st["collided_BA"], air_s=mst["air_ms"] / 1000.0,
        kd=[(s.me.kills, s.me.deaths) for s in players],
    )
    if design != "OLD":
        res.update(
            rate_end=[round(s.rate_eff, 1) for s in players], rate_cuts=sum(s.stats["rate_cuts"] for s in players),
            udp_drop=mst["udp_drop"],
            roles=dict(collections.Counter(s.role for i, s in enumerate(players) if i not in off_at)),
            migrations=sum(s.stats["migrations"] for s in players),
            corrections=sum(s.stats["corrections"] for s in players),
            moves_corrected=sum(s.world.stats["moves_corrected"] for s in players if s.world),
            lagging_target=sum(s.world.stats["lagging_target"] for s in players if s.world),
            joined=all(s.mode == "arena" and s.admitted for i, s in enumerate(players) if i not in off_at),
        )
    return res


# ------------------------------------------------------------------ scenarios
SCENARIOS = {
    "normal": dict(desc="4 players, 5 packets/s, ARQ on, 2 % loss", rate=5, arq=True, loss=0.02),
    "normal_bypass": dict(desc="4 players, 5 packets/s, ARQ bypassed (CDP_GAME_ARQ=0), 2 % loss",
                          rate=5, arq=False, loss=0.02),
    "overload": dict(desc="4 players at 10 packets/s, ARQ on: far more than the channel carries", rate=10,
                     arq=True, loss=0.02),
    "stall": dict(desc="4 players, 5/s, ARQ on; node 1 cannot get the channel for 3 s every 15 s (its frames "
                       "queue up), node 2 hears nothing for 2 s every 20 s", rate=5, arq=True, loss=0.02,
                  stalls={1: (15.0, 3.0, 20.0)}, deaf={2: (20.0, 2.0, 27.0)}),
    "worst": dict(desc="4 players, 5/s, ARQ on, 10 % loss, 2 s interference every 15 s, node 1 stalls 3 s "
                       "every 20 s, node 3 deaf 2 s every 25 s", rate=5, arq=True, loss=0.10,
                  interference=(15.0, 2.0, 22.0), stalls={1: (20.0, 3.0, 26.0)}, deaf={3: (25.0, 2.0, 33.0)}),
    "worst_bypass": dict(desc="the same worst case with the ARQ bypassed", rate=5, arq=False, loss=0.10,
                         interference=(15.0, 2.0, 22.0), stalls={1: (20.0, 3.0, 26.0)},
                         deaf={3: (25.0, 2.0, 33.0)}),
    "host_dies": dict(desc="4 players, 5/s, ARQ on, 5 % loss; node 0 (the host in NEW) is switched off at 60 s",
                      rate=5, arq=True, loss=0.05, off={0: 60.0}),
    "two_worst": dict(desc="2 players (the two PCs), 5/s, ARQ on, 10 % loss, 2 s interference every 15 s, node 1 "
                           "stalls 3 s every 20 s", players=2, rate=5, arq=True, loss=0.10,
                      interference=(15.0, 2.0, 22.0), stalls={1: (20.0, 3.0, 26.0)}),
    "two_normal_bypass": dict(desc="2 players (the two PCs), 5/s, ARQ bypassed, 2 % loss", players=2, rate=5,
                              arq=False, loss=0.02),
    "two_worst_bypass": dict(desc="the two-PC worst case with the ARQ bypassed", players=2, rate=5, arq=False,
                             loss=0.10, interference=(15.0, 2.0, 22.0), stalls={1: (20.0, 3.0, 26.0)}),
    "three_bypass": dict(desc="3 players, 5/s, ARQ bypassed, 5 % loss", players=3, rate=5, arq=False, loss=0.05),
}


def fmt(v, unit="", digits=0):
    if v != v:
        return "-"
    return f"{v:.{digits}f}{unit}"


ROWS = [
    ("hits applied", lambda r: f"{r['hits']}"),
    ("ghost hits: victim really > 1.5 m from where the hit was judged",
     lambda r: f"{r['ghost']} ({100.0 * r['ghost'] / max(1, r['hits']):.0f} %)"),
    ("distance between judged and true victim position, p95", lambda r: fmt(r["disp_p95"], " m", 2)),
    ("hits applied > 1 s after the shot (max)", lambda r: f"{r['late_hit']} ({fmt(r['lat_max'], ' s', 1)})"),
    ("deaths / killed by a hit fired > 1 s earlier / killed by a ghost hit",
     lambda r: f"{r['deaths']} / {r['late_kills']} / {r['ghost_kills']}"),
    ("hits on the shooter's screen that counted (NEW: not counting shots at players already dead on the host)",
     lambda r: f"{r['on_screen_applied']} of {r['on_screen']} ({100.0 * r['on_screen_applied'] / max(1, r['on_screen']):.0f} %)"),
    ("shots rejected (reason count)",
     lambda r: ", ".join(f"{k} {v}" for k, v in sorted(r["rejected"].items())) or "0"),
    ("view age the host judged shots at, p50 / p95",
     lambda r: f"{fmt(r['view_p50'] * 1000, ' ms')} / {fmt(r['view_p95'] * 1000, ' ms')}"),
    ("MAC queue max / payload age when sent p50 / p95 / max",
     lambda r: f"{r['qmax']} / {fmt(r['qdel_p50'], ' ms')} / {fmt(r['qdel_p95'], ' ms')} / "
               f"{fmt(r['qdel_max'] / 1000, ' s', 1)}"),
    ("MAC queue-full drops / newest-only replacements", lambda r: f"{r['qdrop']} / {r['replaced']}"),
    ("frames on air DATA / Block ACK, collisions, channel airtime",
     lambda r: f"{r['tx_D']} / {r['tx_BA']}, {r['coll']}, {r['air_s']:.0f} s"),
    ("NEW: roles at the end, migrations, client corrections, shots at lagging players",
     lambda r: (f"{r['roles']}, {r['migrations']}, {r['corrections']}, {r['lagging_target']}" if "roles" in r else "-")),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="40 s per run instead of 120 s")
    ap.add_argument("--seconds", type=float, default=None)
    ap.add_argument("--only", default=None, help="comma list of scenario names")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("-o", "--out", default=None, help="write a markdown report")
    ap.add_argument("--v69", action="store_true",
                    help="compare the new game on the v6.9 stack (bridge datagrams, FIFO queue): without / with "
                         "congestion control, instead of OLD vs NEW on v6.8")
    a = ap.parse_args()
    dur = a.seconds or (40.0 if a.quick else 120.0)
    names = a.only.split(",") if a.only else list(SCENARIOS)
    lines = [f"# Radio Arena worst-case simulation ({dur:.0f} s per run, seed {a.seed})", "",
             "OLD = earlier peer-to-peer game, FIFO MAC queue. NEW = host-authoritative game with rewind, "
             "newest-only game frames.", ""] if not a.v69 else [
             f"# Radio Arena on the v6.9 stack ({dur:.0f} s per run, seed {a.seed})", "",
             "Both columns: the host-authoritative game on the unchanged v6.9 MAC (FIFO datagram queue of 32 per "
             "destination, oldest dropped, p 0.2, CWmax 63, 11 B bridge header; ARQ on = inputs as reliable "
             "messages). Fixed rate = the menu rate always; adaptive rate = the game's congestion control (rate x0.7 "
             "when the median queueing delay over 1.5 s > 0.25 s, +0.5/s per second otherwise).", ""]
    for name in names:
        cfg = SCENARIOS[name]
        t0 = time.time()
        cols = ("V69", "V69CC") if a.v69 else ("OLD", "NEW")
        res = {k: run(cfg, k, dur, seed=a.seed) for k in cols}
        heads = {"OLD": "OLD (v6.8)", "NEW": "NEW (v6.8)", "V69": "v6.9, fixed rate", "V69CC": "v6.9, adaptive rate"}
        block = [f"## {name}: {cfg['desc']}", "", f"| | {heads[cols[0]]} | {heads[cols[1]]} |", "|---|---|---|"]
        rows = ROWS + ([("v6.9: datagrams dropped from full queues / rate cuts / rates at the end",
                         lambda r: f"{r['udp_drop']} / {r['rate_cuts']} / {r['rate_end']}")] if a.v69 else [])
        for label, f in rows:
            block.append(f"| {label} | {f(res[cols[0]])} | {f(res[cols[1]])} |")
        block.append("")
        lines += block
        print("\n".join(block), flush=True)
        print(f"   ({time.time() - t0:.0f} s)\n", flush=True)
    if a.out:
        with open(a.out, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        print("report written to", a.out)


if __name__ == "__main__":
    main()
