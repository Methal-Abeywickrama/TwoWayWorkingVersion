#!/usr/bin/env python3
"""
fake_session.py - writes made-up session logs (the same files and columns as the flowgraph's Session Logger) into a
folder, growing in real time, so the dashboard can be tried without a radio:

    python3 tools/fake_session.py /tmp/demo_session --seconds 120 &
    python3 cdp_metrics.py /tmp/demo_session
"""
import argparse
import json
import math
import os
import random
import socket
import time

COLS = {
    "csma_txop.csv": "t,kind,dst,bsn,n,bytes,retx,q,wait_ms,att_ms,p_ms,p_draws,bo,cw,defer,busy_ms,nav_ms,unmute_ms,"
                     "air_ms,off_ms,echo_ms,res,acked,rtt_ms,bitmap,sns",
    "csma_1s.csv": "t,busy_pct,nav_pct,tx_pct,floor_db,thr_db,queue,udp_q,ba_due,cw,k,phase,awaiting_ba,rate_pct,echo_ms,"
                   "mute,mute_wr_max_ms,deferrals,busy_on,nav_set,tx_D,tx_BA,ba_rx,ba_timeout,retry_drop,rx_data,rx_dup,"
                   "windows,proc_cpu,load1",
    "arq_tx.csv": "t,dst,what,bsn,acked,nacked,dropped,ws0,ws,used,backlog",
    "arq_rx.csv": "t,src,sn,bsn,idx,cnt,dup,deliv,buf,gaveup,rws,retry,fec",
    "udp.csv": "t,dir,peer,useq,bytes,owd_ms,iat_ms,gap,mac_sn",
    "msgs.csv": "t,dir,peer,mid,ref,bytes,segs,got,frames_tx,resent,dups,wait_s,dur_s,kBps,owd_first_ms,owd_max_ms,res",
    "phy_1s.csv": "t,corr_tags,corr_best,train,train_ok,locks,crc_fail,bad_len,ok,other,own,sq_eob,noise_db,squelch_db",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--seconds", type=float, default=120.0)
    ap.add_argument("--backfill", type=float, default=0.0, help="also write this many seconds of history at once")
    ap.add_argument("--node", type=int, default=0)
    ap.add_argument("--idle-after", type=float, default=None,
                    help="after this many live seconds: no traffic, per-second files only every 10 s (like the logger)")
    a = ap.parse_args()
    os.makedirs(a.dir, exist_ok=True)
    rng = random.Random(3)
    t0 = time.time() - a.backfill
    json.dump({"t0": t0, "node": a.node, "host": socket.gethostname(), "env": {"CDP_SAMP_RATE": "1e6"}},
              open(os.path.join(a.dir, "flowgraph.json"), "w"))
    f = {n: open(os.path.join(a.dir, n), "w", buffering=1) for n in COLS}
    for n, c in COLS.items():
        f[n].write(c + "\n")
    ev = open(os.path.join(a.dir, "events.log"), "w", buffering=1)
    useq, bsn, mid = 0, 0, 0
    sec = 0

    def second(s):
        nonlocal useq, bsn, mid
        load = 0.5 + 0.4 * math.sin(s / 40.0)                       # traffic goes up and down
        storm = 60 < s % 180 < 75                                    # interference for 15 s every 3 min
        busy = min(99, 20 + 50 * load + (35 if storm else 0) + rng.uniform(-5, 5))
        q = int(max(0, rng.gauss(3 + 20 * load * load + (25 if storm else 0), 2)))
        for k in range(int(8 + 10 * load)):
            t = s + k / (8 + 10 * load)
            bsn += 1
            kind = "U" if k % 3 else "R"
            to = kind == "R" and (rng.random() < (0.45 if storm else 0.05))
            wait = abs(rng.gauss(60 + 600 * load * load + (1500 if storm else 0), 40))
            f["csma_txop.csv"].write(f"{t:.3f},{kind},1,{bsn},{2 if kind == 'R' else 1},{760 if kind == 'R' else 420},0,{q},"
                                     f"{wait:.1f},{abs(rng.gauss(18, 6)):.1f},3,2,5,15,0,4,0,1,{14:.1f},20,6,"
                                     f"{'timeout' if to else ('ba' if kind == 'R' else '-')},,{9 if kind == 'R' else ''},,\n")
            if kind == "R" and to:
                f["arq_tx.csv"].write(f"{t + 0.05:.3f},1,noba,{bsn},,1-2,,10,10,2,0\n")
        for k in range(int(5 + 6 * load)):
            t = s + k / (5 + 6 * load)
            useq += 1
            f["udp.csv"].write(f"{t:.3f},tx,1,{useq},64,,,,\n")
            gap = 1 if rng.random() < (0.25 if storm else 0.02) else 0
            owd = 812345 + abs(rng.gauss(40 + 300 * load * load + (900 if storm else 0), 25))
            f["udp.csv"].write(f"{t + 0.01:.3f},rx,1,{useq},66,{owd:.1f},200,{gap},{useq}\n")
        ok = int(15 + 10 * load)
        crc = int(ok * (0.3 if storm else 0.02) + rng.random())
        f["phy_1s.csv"].write(f"{s + 0.99:.3f},40,0.9,{ok},{ok},{ok + crc},{crc},0,{ok},0,{ok},2,-62.5,-55\n")
        f["csma_1s.csv"].write(f"{s + 0.99:.3f},{busy:.1f},2,{8 + 20 * load:.1f},-70,-60,{q},{q // 2},0,15,0,backoff,0,"
                               f"{99.9 if rng.random() > 0.03 else 97.5},6,1,3,4,12,0,{int(8 + 10 * load)},4,4,"
                               f"{5 if storm else 0},{1 if storm and rng.random() < 0.4 else 0},{ok},0,"
                               f"{{\"1\":\"16/2/0\"}},{55 + 30 * load:.0f},1.2\n")
        if s % 17 == 5:
            mid += 1
            size = rng.choice([120, 900, 52000, 410000])
            dur = size / 6500 * (3 if storm else 1)
            f["msgs.csv"].write(f"{s + 0.5:.3f},tx,1,{mid},{1000 + mid},{size},{-(-size // 1000)},{-(-size // 1000)},"
                                f"{-(-size // 1000) + 2},0,,0.1,{dur:.2f},{size / dur / 1000:.2f},,,sent\n")
        if storm and s % 5 == 0:
            ev.write(f"{s + 0.3:.3f} csma udp_drop dst=1 age_ms=3200\n")

    start = time.time()
    for s in range(int(a.backfill)):
        second(s)
    sec = int(a.backfill)
    while time.time() - start < a.seconds:
        if a.idle_after is not None and time.time() - start > a.idle_after:
            if sec % 10 == 0:
                f["csma_1s.csv"].write(f"{sec + 0.99:.3f},3.0,0,0,-70,-60,0,0,0,15,0,idle,0,99.9,6,1,3,0,0,0,0,0,0,"
                                       f"0,0,0,0,{{}},12,0.4\n")
                f["phy_1s.csv"].write(f"{sec + 0.99:.3f},0,0,0,0,0,0,0,0,0,0,0,-63,-55\n")
        else:
            second(sec)
        sec += 1
        time.sleep(max(0.0, t0 + sec - time.time()))


if __name__ == "__main__":
    main()
