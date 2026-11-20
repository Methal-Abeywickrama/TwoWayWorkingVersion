#!/usr/bin/env python3
"""
cdp_logsum.py - turn the session logs of one or two PCs into a short summary (summary.md).

    python3 cdp_logsum.py debug/20261018_223400_node1                     # one PC
    python3 cdp_logsum.py debug/..._node1 other_pc/debug/..._node2 -o summary.md   # both PCs (clock offset fixed)

The summary is meant to be read by a person or pasted to an LLM (a few hundred lines at most). It covers:
  - messages / files: duration, throughput, the slowest ones and what the MAC was doing while they were sent
  - datagrams (UDP): rate, loss, one-way delay, late episodes, and the MAC queue at those moments
  - CSMA: per channel access (contention, persistence, deferrals, airtime, Block ACK timeouts), busy share, CPU
  - ARQ: retransmissions, drops, window stalls, reordering
  - receiver: sync / CRC counters; and the events.log lines
Log formats: LOGS.md.
"""
import argparse
import collections
import csv
import json
import os
import sys
from pathlib import Path


def q(v, p):
    v = sorted(x for x in v if x is not None)
    if not v:
        return None
    k = (len(v) - 1) * p / 100.0
    i = int(k)
    return v[i] if i + 1 >= len(v) else v[i] + (v[i + 1] - v[i]) * (k - i)


def d3(v, unit="", nd=1):
    v = [x for x in v if x is not None]
    if not v:
        return "-"
    return f"p50 {q(v, 50):.{nd}f} / p90 {q(v, 90):.{nd}f} / max {max(v):.{nd}f}{unit} (n={len(v)})"


def num(x):
    if x in (None, ""):
        return None
    try:
        return float(x)
    except ValueError:
        return None


def read_csv(p):
    if not p.exists():
        return []
    with open(p, newline="") as f:
        return list(csv.DictReader(f))


class Node:
    def __init__(self, d):
        self.dir = Path(d)
        fg = self._json("flowgraph.json")
        ses = self._json("session.json")
        self.node = fg.get("node", ses.get("node"))
        self.t0 = fg.get("t0") or ses.get("t0") or 0.0
        self.t0_daemon = ses.get("t0", self.t0)
        self.ses, self.fg = ses, fg
        self.tables = {n: read_csv(self.dir / n) for n in ("csma_txop.csv", "csma_1s.csv", "arq_tx.csv", "arq_rx.csv",
                                                             "udp.csv", "msgs.csv", "phy_1s.csv", "chat.csv")}
        for name, rows in self.tables.items():
            base = self.t0_daemon if name == "chat.csv" else self.t0
            for r in rows:
                r["T"] = base + (num(r.get("t")) or 0.0)       # absolute epoch on this PC's clock
        p = self.dir / "events.log"
        self.events = p.read_text(errors="replace").splitlines() if p.exists() else []

    def _json(self, n):
        try:
            return json.loads((self.dir / n).read_text())
        except Exception:
            return {}

    def t(self, name):
        return self.tables[name]


def offset_from_udp(a, b):
    """clock(b) - clock(a) from the minimum one-way delays in both directions (needs both PCs)."""
    ab = [num(r["owd_ms"]) for r in b.t("udp.csv") if r["dir"] == "rx" and str(r["peer"]) == str(a.node)]
    ba = [num(r["owd_ms"]) for r in a.t("udp.csv") if r["dir"] == "rx" and str(r["peer"]) == str(b.node)]
    ab, ba = [x for x in ab if x is not None], [x for x in ba if x is not None]
    if not ab or not ba:
        ab = [num(r["owd_first_ms"]) for r in b.t("msgs.csv") if r["dir"] == "rx" and r.get("owd_first_ms")]
        ba = [num(r["owd_first_ms"]) for r in a.t("msgs.csv") if r["dir"] == "rx" and r.get("owd_first_ms")]
        ab, ba = [x for x in ab if x is not None], [x for x in ba if x is not None]
    if not ab or not ba:
        return None, None
    lo_ab, lo_ba = q(ab, 5), q(ba, 5)
    return (lo_ab - lo_ba) / 2.0, (lo_ab + lo_ba) / 2.0          # ms: offset, minimum one-way delay


def window(rows, t0, t1):
    return [r for r in rows if t0 <= r["T"] <= t1]


def txop_line(rows):
    if not rows:
        return "no channel accesses"
    k = collections.Counter(r["kind"] for r in rows)
    to = sum(1 for r in rows if r["res"] == "timeout")
    return (f"{len(rows)} accesses ({', '.join(f'{a}:{b}' for a, b in sorted(k.items()))}), Block ACK timeouts {to}, "
            f"contention {d3([num(r['att_ms']) for r in rows], ' ms', 0)}, medium busy during contention "
            f"{d3([num(r['busy_ms']) for r in rows], ' ms', 0)}")


def summarize(nodes, out):
    P = out.append
    P("# CDP link session summary")
    P("")
    for n in nodes:
        s, c = n.ses, n.fg.get("blocks", {}).get("csma", {})
        P(f"- node {n.node}: folder `{n.dir.name}`, host {s.get('host') or n.fg.get('host')}, os {s.get('os', '?')}, "
          f"opponents {s.get('opponents', '?')}, started {s.get('started') or n.fg.get('t0_local')}")
        if c:
            P(f"  MAC: M {c.get('burst_m')} N {c.get('window_n')} TXOP {c.get('txop_limit_s')} s, p {c.get('p_persist')}, "
              f"slot {c.get('slot_s')} s, DIFS {round(c.get('difs_s') or 0, 4)} s, CW {c.get('cw_min')}..{c.get('cw_max')} K "
              f"{c.get('k_max')}, retries {c.get('retry_limit')}, BA timeout {c.get('ba_timeout_s')} s, samp_rate "
              f"{c.get('samp_rate')}, TX mute {c.get('tx_mute')}, udp queue {c.get('udp_queue')}")
        b = n.fg.get("blocks", {}).get("bridge", {})
        if b:
            P(f"  bridge: mss {b.get('mss')} B, inflight {b.get('inflight')}")
    off = None
    if len(nodes) == 2:
        off, owd = offset_from_udp(nodes[0], nodes[1])
        if off is not None:
            P(f"- clock of node {nodes[1].node} minus node {nodes[0].node}: {off:+.1f} ms (minimum one-way delay "
              f"{owd:.1f} ms); one-way delays below are corrected with it")
    P("")
    for n in nodes:
        other = nodes[1] if len(nodes) == 2 and n is nodes[0] else nodes[0] if len(nodes) == 2 else None
        corr = 0.0
        if off is not None and other is not None:
            corr = off if n is nodes[0] else -off       # owd_raw = true + (my clock - sender clock)
        section(n, P, corr)
    return out


def section(n, P, corr):
    P(f"## Node {n.node}")
    tx = n.t("csma_txop.csv")
    s1 = n.t("csma_1s.csv")
    # ---------------- messages
    msgs = n.t("msgs.csv")
    P("### Reliable messages (msgs.csv)")
    for d in ("tx", "rx"):
        rows = [r for r in msgs if r["dir"] == d]
        if not rows:
            continue
        P(f"- {d}: {len(rows)} messages, {sum(int(num(r['bytes']) or 0) for r in rows)} B; duration "
          f"{d3([num(r['dur_s']) for r in rows], ' s', 2)}; throughput {d3([num(r['kBps']) for r in rows], ' kB/s', 2)}; "
          f"results {dict(collections.Counter(r['res'] for r in rows))}")
    big = sorted([r for r in msgs if r["dir"] == "tx" and (num(r["bytes"]) or 0) >= 2000],
                 key=lambda r: num(r["kBps"]) or 0)
    if big:
        P("- transfers >= 2 kB, slowest first (with what the MAC did during each):")
        for r in big[:6] + ([None] if len(big) > 8 else []) + big[-2:] if len(big) > 8 else big:
            if r is None:
                P("  ...  fastest:")
                continue
            t1 = r["T"]
            t0 = t1 - (num(r["dur_s"]) or 0)
            w = window(tx, t0, t1)
            ws = window(s1, t0, t1)
            P(f"  - t={num(r['t']):.1f}s {r['bytes']} B to {r['peer']} in {r['dur_s']} s = {r['kBps']} kB/s, "
              f"{r['segs']} segs / {r['frames_tx']} frame transmissions, waited {r['wait_s']} s before the first "
              f"segment | {txop_line(w)} | busy {d3([num(x['busy_pct']) for x in ws], ' %', 0)}, udp_q max "
              f"{max([num(x['udp_q']) or 0 for x in ws] or [0]):.0f}, cpu max {max([num(x['proc_cpu']) or 0 for x in ws] or [0]):.0f} %")
    # ---------------- datagrams
    udp = n.t("udp.csv")
    if udp:
        P("### Datagrams (udp.csv)")
        txr = [r for r in udp if r["dir"] == "tx"]
        rxr = [r for r in udp if r["dir"] == "rx"]
        if txr:
            span = max(1e-3, txr[-1]["T"] - txr[0]["T"])
            P(f"- sent {len(txr)} ({len(txr) / span:.1f}/s)")
        if rxr:
            span = max(1e-3, rxr[-1]["T"] - rxr[0]["T"])
            gaps = [int(num(r["gap"])) for r in rxr if num(r["gap"]) is not None]
            lost = sum(g for g in gaps if g > 0)
            owd = [num(r["owd_ms"]) + corr if num(r["owd_ms"]) is not None else None for r in rxr]
            P(f"- received {len(rxr)} ({len(rxr) / span:.1f}/s), missing in sequence {lost} "
              f"({100.0 * lost / max(1, lost + len(rxr)):.1f} %), out of order {sum(1 for g in gaps if g < 0)}")
            P(f"- one-way delay {d3(owd, ' ms', 0)}{'' if corr else ' (raw: includes the clock offset)'}; "
              f"inter-arrival {d3([num(r['iat_ms']) for r in rxr], ' ms', 0)}")
            med = q([o for o in owd if o is not None], 50) or 0
            late = [(r, o) for r, o in zip(rxr, owd) if o is not None and o > max(3 * med, med + 300)]
            if late:
                P(f"- late datagrams (> max(3x median, median + 300 ms)): {len(late)}; episodes:")
                eps, cur = [], None
                for r, o in late:
                    if cur and r["T"] - cur[1] < 2.0:
                        cur[1], cur[2], cur[3] = r["T"], max(cur[2], o), cur[3] + 1
                    else:
                        cur = [r["T"], r["T"], o, 1]
                        eps.append(cur)
                for a, b, mx, cnt in eps[:8]:
                    ws = window(s1, a - 1.5, b + 0.5)
                    P(f"  - t={a - n.t0:.1f}..{b - n.t0:.1f}s: {cnt} late, worst {mx:.0f} ms | sender-side note: "
                      f"this PC's udp_q max {max([num(x['udp_q']) or 0 for x in ws] or [0]):.0f}, busy "
                      f"{d3([num(x['busy_pct']) for x in ws], ' %', 0)}")
            ut = [r for r in tx if r["kind"] == "U"]
            if ut:
                P(f"- this PC's datagram bursts: {len(ut)}, frames per burst {d3([num(r['n']) for r in ut], '', 1)}, "
                  f"queue wait (oldest datagram) {d3([num(r['wait_ms']) for r in ut], ' ms', 0)}")
    # ---------------- CSMA
    P("### CSMA (csma_txop.csv, csma_1s.csv)")
    for kind, nm in (("R", "reliable bursts"), ("U", "datagram bursts"), ("B", "broadcast"), ("A", "Block ACKs sent")):
        rows = [r for r in tx if r["kind"] == kind]
        if not rows:
            continue
        line = (f"- {nm}: {len(rows)}; frames {d3([num(r['n']) for r in rows], '', 1)}; wait {d3([num(r['wait_ms']) for r in rows], ' ms', 0)}; "
                f"contention {d3([num(r['att_ms']) for r in rows], ' ms', 0)} (persistence {d3([num(r['p_ms']) for r in rows], ' ms', 0)}, "
                f"busy {d3([num(r['busy_ms']) for r in rows], ' ms', 0)}, deferrals {d3([num(r['defer']) for r in rows], '', 0)}); "
                f"air {d3([num(r['air_ms']) for r in rows], ' ms', 0)}; RX off {d3([num(r['off_ms']) for r in rows], ' ms', 0)}; "
                f"unmute {d3([num(r['unmute_ms']) for r in rows], ' ms', 1)}")
        if kind == "R":
            res = collections.Counter(r["res"] for r in rows)
            line += (f"; results {dict(res)}; Block ACK round trip {d3([num(r['rtt_ms']) for r in rows], ' ms', 0)}; "
                     f"CW used {dict(collections.Counter(r['cw'] for r in rows))}")
        P(line)
    if s1:
        P(f"- per second: busy {d3([num(r['busy_pct']) for r in s1], ' %', 0)}, own TX {d3([num(r['tx_pct']) for r in s1], ' %', 0)}, "
          f"NAV {d3([num(r['nav_pct']) for r in s1], ' %', 0)}, queue {d3([num(r['queue']) for r in s1], '', 0)}, "
          f"udp_q {d3([num(r['udp_q']) for r in s1], '', 0)}")
        lowrate = [r for r in s1 if (num(r["rate_pct"]) or 100) < 99]
        P(f"- radio samples delivered {d3([num(r['rate_pct']) for r in s1], ' %', 1)}; seconds below 99 %: {len(lowrate)}"
          f"{' (PC not keeping up)' if lowrate else ''}; flowgraph CPU {d3([num(r['proc_cpu']) for r in s1], ' %', 0)}; "
          f"noise floor {d3([num(r['floor_db']) for r in s1], ' dB', 1)}")
    slow = sorted([r for r in tx if r["kind"] in ("R", "U")], key=lambda r: -(num(r["att_ms"]) or 0))[:5]
    if slow:
        P("- slowest channel accesses (contention):")
        for r in slow:
            P(f"  - t={num(r['t']):.1f}s {r['kind']} to {r['dst']}: contention {r['att_ms']} ms (persistence "
              f"{r['p_ms']} ms / {r['p_draws']} draws, backoff {r['bo']} of CW {r['cw']}, {r['defer']} deferrals, medium "
              f"busy {r['busy_ms']} ms, NAV {r['nav_ms']} ms), result {r['res']}")
    # ---------------- ARQ
    at, ar = n.t("arq_tx.csv"), n.t("arq_rx.csv")
    if at or ar:
        P("### ARQ (arq_tx.csv, arq_rx.csv)")
        cnt = lambda s: sum(len(rng_expand(s)) for s in s)
        P(f"- Block ACK results {dict(collections.Counter(r['what'] for r in at))}; acknowledged "
          f"{cnt([r['acked'] for r in at])}, sent again {cnt([r['nacked'] for r in at])}, dropped "
          f"{cnt([r['dropped'] for r in at])}")
        N = n.fg.get("blocks", {}).get("csma", {}).get("window_n")
        full = sum(1 for r in at if N and num(r["used"]) == N and (num(r["backlog"]) or 0) > 0)
        P(f"- window full with frames waiting behind it: {full} of {len(at)} Block ACK results")
        if ar:
            P(f"- frames received {len(ar)}, duplicates {sum(1 for r in ar if r['dup'] == '1')}, held for reordering "
              f"max {max([num(r['buf']) or 0 for r in ar] or [0]):.0f}, given up {sum(int(num(r['gaveup']) or 0) for r in ar)}, "
              f"FEC bits fixed {d3([num(r['fec']) for r in ar], '', 0)}")
    # ---------------- PHY
    ph = n.t("phy_1s.csv")
    if ph:
        tot = lambda k: int(sum(num(r.get(k)) or 0 for r in ph))
        P("### Receiver (phy_1s.csv)")
        P(f"- train words {tot('train')} (clean {tot('train_ok')}), sync locks {tot('locks')}, CRC ok {tot('ok')}, CRC fail "
          f"{tot('crc_fail')}, bad length {tot('bad_len')}, own frames {tot('own')}, squelch closures {tot('sq_eob')}; "
          f"squelch threshold {d3([num(r['squelch_db']) for r in ph], ' dB', 1)}")
    # ---------------- chat / events
    ch = n.t("chat.csv")
    if ch:
        done = [r for r in ch if r["dir"] == "tx" and r["res"] != "queued"]
        P(f"### Chat (chat.csv): sent {len(done)} ({dict(collections.Counter(r['kind'] for r in done))}), results "
          f"{dict(collections.Counter(r['res'] for r in done))}, time to delivered {d3([num(r['dur_s']) for r in done], ' s', 2)}; "
          f"received {sum(1 for r in ch if r['dir'] == 'rx')}")
    if n.events:
        kinds = collections.Counter(" ".join(l.split()[1:3]) for l in n.events if len(l.split()) > 2)
        P(f"### events.log: {dict(kinds.most_common(12))}")
        for l in n.events[:12]:
            P(f"    {l[:200]}")
    P("")


def rng_expand(s):
    out = []
    for part in str(s or "").split():
        if "-" in part[1:]:
            a, b = part.split("-", 1)
            out += list(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("folders", nargs="+", help="session folder(s): debug/<date>_<time>_node<N>")
    ap.add_argument("-o", "--out", default=None)
    a = ap.parse_args()
    nodes = [Node(f) for f in a.folders[:2]]
    out = summarize(nodes, [])
    txt = "\n".join(out) + "\n"
    if a.out:
        Path(a.out).write_text(txt)
        print(f"summary written to {a.out} ({len(out)} lines)")
    else:
        print(txt)


if __name__ == "__main__":
    main()
