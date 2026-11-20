#!/usr/bin/env python3
"""
test_dashboard.py - the dashboard (cdp_metrics.py) with logs written in real time by tools/fake_session.py:
  1. the flowgraph writes into an OLDER session's folder (GRC left open from an earlier start): the dashboard finds it
  2. a flowgraph of another node on the same PC is not taken
  3. the chat app's own traffic is charted every second even with no flowgraph logs at all
  4. when logs appear in this session's own folder, the dashboard goes back to it
  5. the dashboard window's tiles and chart series (cdp_dashboard.py; the window itself: test_dashboard_qt.py)
Run: python3 tools/test_dashboard.py
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import cdp_metrics  # noqa: E402

FAILS = []


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({info})" if info else ""), flush=True)
    if not ok:
        FAILS.append(name)


def fake(d, node, seconds=40):
    return subprocess.Popen([sys.executable, str(HERE / "tools" / "fake_session.py"), str(d), "--seconds", str(seconds),
                             "--node", str(node), "--backfill", "20"], stdout=subprocess.DEVNULL)


def wait_for(fn, timeout=10.0):
    t = time.time()
    while time.time() - t < timeout:
        r = fn()
        if r:
            return r
        time.sleep(0.25)
    return None


def main():
    root = Path(tempfile.mkdtemp(prefix="dash_"))
    debug = root / "debug"
    old = debug / "20261019_100000_node0"           # the earlier session: its flowgraph still runs
    other = debug / "20261019_100500_node1"         # another node's flowgraph on this PC (test setups)
    home = debug / "20261019_110000_node0"          # this chat window's session folder
    for d in (old, other, home):
        d.mkdir(parents=True)
    (home / "daemon.log").write_text("x\n")

    # ---- 3 first: no logs at all, only the app's counters
    tot = {"chat_tx": 0, "chat_rx": 0, "voice_tx": 0, "voice_rx": 0}
    t_start = time.time()

    def counters():
        el = time.time() - t_start
        tot["chat_tx"] = 2000 * el                  # 2 kB/s delivered
        tot["chat_rx"] = 500 * el
        return dict(tot)
    m = cdp_metrics.Metrics(home, node=0, search=[debug])
    m.start_sampler(counters)
    time.sleep(3.5)
    s = m.snapshot(60)
    vals = [r.get("chat_tx") for r in s["rows"] if r.get("chat_tx") is not None]
    check("no logs: state 'waiting', reading this session's folder", s["state"] == "waiting" and s["dir"] == str(home),
          s["state"])
    check("no logs: the note says no flowgraph is writing", "No flowgraph" in s["note"])
    check("no logs: chat app traffic charted every second (≈2 kB/s)",
          len(vals) >= 2 and all(1.5 < v < 2.5 for v in vals[-2:]), vals[-3:])
    hi1 = s["hi"]
    time.sleep(1.2)
    check("no logs: the time axis still moves", m.snapshot(60)["hi"] > hi1)

    # ---- 2: only another node's flowgraph is writing -> not taken
    p_other = fake(other, 1)
    time.sleep(4.0)
    s = m.snapshot(60)
    check("another node's logs are ignored", s["dir"] == str(home), s["dir"])

    # ---- 1: the old session's flowgraph (node 0) writes -> followed
    p_old = fake(old, 0)
    s = wait_for(lambda: (lambda x: x if x["dir"] == str(old) and x["rows"] else None)(m.snapshot(60)), 8)
    check("finds the flowgraph writing into an older session folder", s is not None)
    if s:
        check("  ... and says so", "not to this chat session's folder" in s["note"], s["note"][:60])
        check("  ... state live", s["state"] in ("live", "idle"), s["state"])
        check("  ... link data present", any((r.get("tx_kBps") or 0) > 0 for r in s["rows"]))
        check("  ... chat app series still there", any(r.get("chat_tx") for r in s["rows"]))
        h1 = s["hi"]
        time.sleep(2.2)
        s2 = m.snapshot(60)
        check("  ... updates (hi advances)", s2["hi"] >= h1 + 2, f"{h1} -> {s2['hi']}")

    # ---- 4: logs appear in this session's own folder -> back home
    p_home = fake(home, 0)
    s = wait_for(lambda: (lambda x: x if x["dir"] == str(home) and x["rows"] and x["state"] != "waiting" else None)(
        m.snapshot(60)), 8)
    check("switches back when this session's folder gets the logs", s is not None)
    if s:
        check("  ... no note then", s["note"] == "", s["note"][:60])

    # ---- 5: what the dashboard window shows (cdp_dashboard: the parts without Qt)
    import cdp_dashboard as D
    s = m.snapshot(60)
    t = D.tiles(s)
    check("tiles: Link + 8 key figures", len(t) == 9 and t[0][0] == "Link", [x[0] for x in t])
    check("tiles: every status is known", all(x[3] in D.STATUS for x in t), [x[3] for x in t])
    thr = next(c for c in D.CHARTS if c[0] == "thr")
    xs, ser = D.series(s, thr)
    raw = [r.get("tx_kBps") for r in s["rows"]]
    check("charts: one point per row, kB/s shown as kbit/s",
          len(xs) == len(raw) and all((a is None and b is None) or abs(a * 8 - b) < 1e-9 for a, b in zip(raw, ser[0][1])))
    check("axis maximum rounds up", D.nice_max(0.7) == 1 and D.nice_max(13) == 20 and D.nice_max(260) == 500)
    col, txt, sub = D.state_line(s)
    check("state line", "live" in txt or "idle" in txt, txt)

    for p in (p_old, p_other, p_home):
        p.kill()
    m.stop()
    print("ALL PASS" if not FAILS else f"FAILED: {FAILS}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
