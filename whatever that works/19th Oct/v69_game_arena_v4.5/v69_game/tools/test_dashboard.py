#!/usr/bin/env python3
"""
test_dashboard.py - the dashboard (cdp_metrics.py) with logs written in real time by tools/fake_session.py:
  1. the flowgraph writes into an OLDER session's folder (GRC left open from an earlier start): the dashboard finds it
  2. a flowgraph of another node on the same PC is not taken
  3. the chat app's own traffic is charted every second even with no flowgraph logs at all
  4. when logs appear in this session's own folder, the dashboard goes back to it
  5. the web server answers, and reports an error as text instead of going silent
Run: python3 tools/test_dashboard.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
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

    # ---- 5: the web server
    srv = cdp_metrics.DashboardServer(m, 5390)
    check("server starts", srv.ok, getattr(srv, "error", ""))
    d = json.loads(urllib.request.urlopen(srv.url + "api/metrics?window=60", timeout=5).read())
    check("api answers with rows", bool(d.get("rows")) and "note" in d)
    page = urllib.request.urlopen(srv.url, timeout=5).read().decode()
    check("page served", "CDP link" in page and "showNotice" in page)
    orig = m.snapshot
    m.snapshot = lambda w: 1 / 0
    try:
        urllib.request.urlopen(srv.url + "api/metrics?window=60", timeout=5)
        check("server error reported as text", False, "no error")
    except urllib.error.HTTPError as e:
        body = json.loads(e.read())
        check("server error reported as text", e.code == 500 and "ZeroDivisionError" in body.get("error", ""))
    m.snapshot = orig

    shot = os.environ.get("DASH_SHOT")
    if shot:                                    # screenshot of the page following the older folder
        for p in (p_home,):
            p.kill()
        import shutil
        for f in home.iterdir():
            if f.name != "daemon.log":
                f.unlink()
        m2 = cdp_metrics.Metrics(home, node=0, search=[debug])
        m2.start_sampler(counters)
        srv2 = cdp_metrics.DashboardServer(m2, 5395)
        time.sleep(4)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            pg = b.new_page(viewport={"width": 1400, "height": 1000})
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(srv2.url)
            pg.wait_for_timeout(3500)
            t1 = pg.inner_text("#livetxt")
            pg.wait_for_timeout(2200)
            t2 = pg.inner_text("#livetxt")
            check("page: no script errors", not errs, errs[:1])
            check("page: notice shown", pg.is_visible("#notice"))
            check("page: 'updated' time moves", t1 != t2, f"{t1} | {t2}")
            pg.screenshot(path=shot, full_page=True)
            b.close()
        srv2.close()
        shutil.rmtree(root, ignore_errors=True) if False else None

    for p in (p_old, p_other, p_home):
        p.kill()
    srv.close()
    m.stop()
    print("ALL PASS" if not FAILS else f"FAILED: {FAILS}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
