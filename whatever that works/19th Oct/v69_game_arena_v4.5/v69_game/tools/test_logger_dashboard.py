#!/usr/bin/env python3
"""
test_logger_dashboard.py - the Session Logger writes the session logs and the dashboard shows them.

  1. the .grc wires the logger: every enabled embedded block has at least one connection. GNU Radio only runs blocks
     on a stream or message connection; an unconnected block is built but never started, and writes nothing.
  2. the logger (code from the .grc) runs next to the real MAC block on a realistic signal with traffic: it prints
     "[LOG] performance logs -> <folder>", writes csma_1s.csv / csma_txop.csv / phy_1s.csv / events.log
  3. cdp_metrics (the dashboard) reads that folder: live rows with channel busy, frames, CPU

Run: python3 tools/test_logger_dashboard.py
"""
import contextlib
import io
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import epy_harness as H  # noqa: E402

FAILS = []


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({info})" if info else ""), flush=True)
    if not ok:
        FAILS.append(name)


def main():
    d = yaml.safe_load(open(H.GRC))
    used = {c[0] for c in d["connections"]} | {c[2] for c in d["connections"]}
    lonely = [b["name"] for b in d["blocks"] if b["id"] == "epy_block"
              and b.get("states", {}).get("state", "enabled") == "enabled" and b["name"] not in used]
    check("every enabled embedded block is connected (else GNU Radio never starts it)", not lonely, lonely)
    check("logger input 'attach' is connected", ["epy_bridge", "mac_ctrl", "epy_logger", "attach"] in d["connections"])

    tmp = Path(tempfile.mkdtemp(prefix="cdp_log_"))
    H.new_bus()
    L = H.load("epy_logger")
    lg = L(my_id=0, log_dir=str(tmp), flush_s=0.5, idle_every_s=10.0)
    mac = H.make_csma(H.load("epy_csma"))
    mac._rng = np.random.default_rng(5)
    fr = H.make_framer(0)
    mac.route("to_phy", fr.handle_msg)
    fr.route("pdu_out", mac._on_phy)
    x = H.sig_csma(12)
    plan = [int(s * H.SR) for s in (1.0, 2.5, 4.0, 6.0, 8.5)]

    def before(a):
        while plan and plan[0] <= a:
            plan.pop(0)
            mac._on_app(H.app_msg(1, bytes(300)))
        # pace the stream roughly in real time so the logger's 1 s snapshots see it
        target = t_start + a / H.SR / 4
        while time.time() < target:
            time.sleep(0.005)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        lg.start()
        mac.start()
        t_start = time.time()
        H.run_sync(mac, x, out_dtype=np.complex64, before=before)
        time.sleep(1.5)
        lg.stop()
    log = out.getvalue()
    check("prints '[LOG] performance logs -> <folder>'", f"[LOG] performance logs -> {tmp}" in log)

    def rows(n):
        p = tmp / n
        return max(0, len(p.read_text().splitlines()) - 1) if p.exists() else -1
    r = {n: rows(n) for n in ("csma_1s.csv", "csma_txop.csv", "phy_1s.csv", "arq_tx.csv", "udp.csv", "msgs.csv")}
    check("flowgraph.json written", (tmp / "flowgraph.json").exists())
    check("csma_1s.csv has per-second rows", r["csma_1s.csv"] >= 2, r)
    check("csma_txop.csv has the channel accesses", r["csma_txop.csv"] >= 3, r)
    check("events.log written", (tmp / "events.log").exists())

    import cdp_metrics
    m = cdp_metrics.Metrics(tmp, node=0, search=[tmp.parent])
    s = m.snapshot(0)
    busy = [row.get("busy_pct") for row in s["rows"] if row.get("busy_pct") is not None]
    frames = [row.get("tx_frames") for row in s["rows"] if row.get("tx_frames")]
    check("dashboard reads this folder", s["dir"] == str(tmp), s["dir"])
    check("dashboard has channel-busy values", len(busy) >= 2, f"{len(busy)} seconds")
    check("dashboard has sent frames", len(frames) >= 1, f"{len(frames)} seconds with frames")
    m.stop()
    print("ALL PASS" if not FAILS else f"FAILED: {FAILS}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
