#!/usr/bin/env python3
"""
test_dashboard_qt.py - runs the dashboard window (cdp_dashboard.DashboardWindow) and the login screen's new
modulation choice through every code path, on logs that tools/fake_session.py writes in real time.

With PyQt5 installed it uses the real Qt (offscreen); without it, tools/qtstub (a stand-in that only checks the
Python side: names, arithmetic, data flow - not how Qt draws).
    python3 tools/test_dashboard_qt.py
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
try:
    import PyQt5  # noqa: F401
    REAL = True
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
except ImportError:
    REAL = False
    sys.path.insert(0, str(ROOT / "tools" / "qtstub"))
sys.path.insert(0, str(ROOT))
import cdp_metrics  # noqa: E402
import cdp_dashboard as D  # noqa: E402

FAILS = []


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({info})" if info else ""), flush=True)
    if not ok:
        FAILS.append(name)


def main():
    print("Qt:", "PyQt5 (offscreen)" if REAL else "tools/qtstub stand-in")
    check("cdp_dashboard sees Qt", D.HAVE_QT)
    if REAL:
        from PyQt5.QtWidgets import QApplication
        app = QApplication(sys.argv)  # noqa: F841
    d = Path(tempfile.mkdtemp(prefix="dashqt_")) / "20261120_100000_node1"
    d.mkdir(parents=True)
    p = subprocess.Popen([sys.executable, str(ROOT / "tools" / "fake_session.py"), str(d), "--seconds", "30",
                          "--node", "1", "--backfill", "20"], stdout=subprocess.DEVNULL)
    try:
        m = cdp_metrics.Metrics(d, node=1, search=[])
        m.start_sampler(lambda: {"chat_tx": 3000 * time.time() % 1e9, "chat_rx": 0, "voice_tx": 0, "voice_rx": 0})
        w = D.DashboardWindow(m, "test")
        w.show()
        err = None
        for i in range(6):
            time.sleep(0.6)
            try:
                w.refresh()
                for idx in range(w.win.count() if REAL else 4):
                    w.win.setCurrentIndex(idx)
                    w.refresh()
                for c in w.charts:
                    c.hover = 300 if not REAL else c.width() // 2
                    c.paintEvent(None) if not REAL else c.repaint()
                    c.hover = None
                    c.paintEvent(None) if not REAL else c.repaint()
            except Exception as e:                           # noqa: BLE001
                import traceback
                err = traceback.format_exc()
                break
        check("window: refresh, every time window, every chart painted (with and without hover)", err is None,
              (err or "")[-400:])
        s = m.snapshot(300)
        check("logs were read", bool(s["rows"]) and s["state"] in ("live", "idle"), s["state"])
        check("charts got data", any(any(v is not None for _, ys in D.series(s, c.spec)[1] for v in ys)
                                     for c in w.charts))
        if not REAL:
            check("tiles were filled", all(t.k.text() for t in w.tiles), [t.k.text() for t in w.tiles][:3])
            # an error in the data is shown in the window instead of going silent
            orig = m.snapshot
            m.snapshot = lambda win: 1 / 0
            w.refresh()
            check("a data error is shown in the window", "ZeroDivisionError" in w.notice.text())
            m.snapshot = orig
        m.stop()
    finally:
        p.kill()
    # login screen: the modulation choice
    import cdp_daemon
    if cdp_daemon.HAVE_QT:
        dlg = cdp_daemon.LoginDialog(7, "test", Path("v6.9_cdp_link.grc"), True, False, "qpsk")
        check("login: QPSK preselected and returned", dlg.values() == (7, False, "qpsk"), dlg.values())
        dlg = cdp_daemon.LoginDialog(7, "test", Path("v6.9_cdp_link.grc"), True, False, "bpsk")
        check("login: BPSK preselected and returned", dlg.values()[2] == "bpsk", dlg.values())
    st = cdp_daemon.AppState.__new__(cdp_daemon.AppState)
    st.mod, st.me, st.core, st.session = "qpsk", 7, None, type("S", (), {"dir": d})()
    e = st.env()
    check("the flowgraph gets CDP_MOD", e.get("CDP_MOD") == "qpsk", e.get("CDP_MOD"))
    print("ALL PASS" if not FAILS else f"FAILED: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
