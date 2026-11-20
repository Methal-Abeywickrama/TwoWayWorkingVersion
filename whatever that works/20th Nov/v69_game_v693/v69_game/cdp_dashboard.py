#!/usr/bin/env python3
"""
cdp_dashboard.py - the performance dashboard as a window of the app (PyQt5; replaces the web page of v6.9).

From the chat window: "📊 Performance dashboard". For an old session, or the other PC's logs:
    python3 cdp_dashboard.py debug/<session>

It reads the session logs the flowgraph writes (cdp_metrics.Metrics: csma_1s.csv, csma_txop.csv, phy_1s.csv,
udp.csv, arq_*.csv, msgs.csv, events.log) once a second and shows
  - status tiles for the last 10 s (ok / high / critical): link, delivered and on-air throughput, channel busy,
    Block ACK timeouts, frame errors, queueing delay, datagram loss, PC load
  - charts over the last 1 / 5 / 15 min or the whole session, one point per second, with a hover read-out
  - the nodes heard, the last reliable messages, the last events, encryption and voice counters
Throughput is in kbit/s (the logs count kB/s: x 8).
"""
import html
import math
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cdp_metrics  # noqa: E402

# ======================================================================================== pure part (no Qt)
SURFACE, TEXT, TEXT2, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#8a8984", "#e4e3df"
SERIES = ("#2a78d6", "#eb6834", "#1baf7a")                 # categorical slots 1-3 (validated palette)
STATUS = {"good": ("#006300", "✓", "ok"), "warn": ("#8a5a00", "!", "high"), "crit": ("#b42828", "✕", "critical"),
          "none": (MUTED, "·", "no data")}
WINDOWS = (("last 1 min", 60), ("last 5 min", 300), ("last 15 min", 900), ("whole session", 0))


def fmt(v, d=1):
    if v is None:
        return "–"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if not math.isfinite(v):
        return "–"
    return f"{v:,.{d}f}"


def kbit(v):
    return None if v is None else v * 8.0


# key, label, unit, decimals, warn, crit, note(kpi) -> text
TILES = (
    ("goodput", "Delivered (chat app)", "kbit/s", 1, None, None,
     lambda r: f"received {fmt(kbit(r.get('chat_rx')))} kbit/s"),
    ("tx_kbit", "Sent on air", "kbit/s", 1, None, None,
     lambda r: f"incl. retransmissions · datagrams in {fmt(kbit(r.get('udp_rx_kBps')))} kbit/s"),
    ("busy_pct", "Channel busy", "%", 1, 70, 90, lambda r: f"own airtime {fmt(r.get('tx_pct'))} %"),
    ("ba_timeouts", "Block ACK timeouts", "/10 s", 0, 5, 30, lambda r: f"gave up {fmt(r.get('retry_drops'), 0)} frames"),
    ("crc_pct", "Frame errors (CRC)", "%", 1, 5, 20, lambda r: "of the frames received"),
    ("wait_p95", "Queueing delay p95", "ms", 0, 500, 2000, lambda r: f"channel access {fmt(r.get('att_p50'), 0)} ms median"),
    ("loss_pct", "Datagram loss", "%", 1, 5, 20, lambda r: f"jitter p95 {fmt(r.get('jitter_p95'), 0)} ms"),
    ("cpu", "PC load", "% CPU", 0, 85, 98, lambda r: f"samples {fmt(r.get('rate_pct'))} % of the rate"),
)

# id, title, note, unit, scale, fixed max, [(key, label)]
CHARTS = (
    ("app", "Chat app", "messages and files: delivered (acknowledged) and received", "kbit/s", 8.0, None,
     (("chat_tx", "sent, delivered"), ("chat_rx", "received"))),
    ("thr", "On the air", "payload handed to the radio (with retransmissions), datagrams received", "kbit/s", 8.0, None,
     (("tx_kBps", "sent"), ("udp_rx_kBps", "datagrams received"))),
    ("chan", "Channel use", "share of each second", "%", 1.0, 100.0,
     (("busy_pct", "medium busy"), ("tx_pct", "own transmissions"))),
    ("frm", "Frames per second", "data frames sent and received", "/s", 1.0, None,
     (("tx_frames", "sent"), ("rx_frames", "received"), ("rx_rel", "reliable, new"))),
    ("rty", "Retries & drops", "per second", "/s", 1.0, None,
     (("ba_timeouts", "Block ACK timeouts"), ("retry_drops", "given up"), ("udp_drops", "datagram queue full"))),
    ("err", "Errors", "frames with a CRC error, datagrams lost", "%", 1.0, None,
     (("crc_pct", "CRC errors"), ("loss_pct", "datagrams lost"))),
    ("del", "Delays", "queueing p95, channel access median, datagram jitter p95", "ms", 1.0, None,
     (("wait_p95", "queueing p95"), ("att_p50", "access median"), ("jitter_p95", "jitter p95"))),
    ("que", "Queues", "frames waiting in the MAC", "frames", 1.0, None,
     (("queue", "all"), ("udp_q", "datagrams"))),
    ("pc", "PC load", "flowgraph CPU (100 = one core), samples delivered (% of the rate)", "%", 1.0, None,
     (("cpu", "CPU"), ("rate_pct", "samples"))),
    ("voice", "Voice", "live voice on the air", "kbit/s", 8.0, None,
     (("voice_tx", "sent"), ("voice_rx", "received"))),
)


def status(v, warn, crit):
    if v is None:
        return "none"
    if crit is not None and v >= crit:
        return "crit"
    if warn is not None and v >= warn:
        return "warn"
    return "good"


def link_tile(s):
    """-> (status, value text, note) of the 'Link' tile"""
    st = s.get("state")
    if s.get("link_connected") is False:
        return "crit", "no bridge", "the chat app is not connected to the flowgraph"
    if st in ("live", "idle"):
        return "good", "live" if st == "live" else "idle", f"node {s.get('node', '?')} · logs {fmt(s.get('age_s'), 0)} s old"
    if st == "stopped":
        return "warn", "stopped?", f"no log rows for {fmt(s.get('age_s'), 0)} s"
    if st == "ended":
        return "none", "ended", "an old session"
    return "warn", "waiting", "press ▶ Run in GRC"


def tiles(s):
    """snapshot -> [(label, value text, unit, status, note)] for the last 10 s"""
    r = dict(s.get("kpi") or {})
    r["goodput"] = kbit(r.get("chat_tx"))
    r["tx_kbit"] = kbit(r.get("tx_kBps"))
    st, val, note = link_tile(s)
    out = [("Link", val, "", st, note)]
    idle = s.get("state") == "idle"
    for key, label, unit, d, warn, crit, nf in TILES:
        v = r.get(key)
        stt = status(v, warn, crit)
        if key == "cpu" and r.get("rate_pct") is not None:          # samples missing is worse than CPU load
            rp = r["rate_pct"]
            stt = "crit" if rp < 95 else ("warn" if rp < 99 and stt == "good" else stt)
        if warn is None and v is not None:
            stt = "good"
        if idle and v in (None, 0, 0.0) and key in ("goodput", "tx_kbit"):
            stt = "none"
        out.append((label, fmt(v, d), unit, stt, nf(r)))
    return out


def series(s, chart):
    """snapshot + chart spec -> (xs, [(label, ys)]) with None for gaps; values in the chart's unit"""
    rows = s.get("rows") or []
    scale = chart[4]
    xs = [r.get("t") for r in rows]
    out = []
    for key, label in chart[6]:
        ys = []
        for r in rows:
            v = r.get(key)
            ys.append(None if v is None else float(v) * scale)
        out.append((label, ys))
    return xs, out


def nice_max(v):
    """axis maximum: 1, 2, 2.5, 5 x 10^n above v"""
    if v is None or v <= 0:
        return 1.0
    e = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * e:
            return m * e
    return 10 * e


def clock(t_rel, t0):
    """seconds since the flowgraph started -> local clock time"""
    try:
        return time.strftime("%H:%M:%S", time.localtime(t0 + t_rel))
    except (TypeError, ValueError, OverflowError):
        return "–"


def state_line(s):
    st = s.get("state")
    txt = {"live": "live", "idle": "live · link idle", "waiting": "waiting for the flowgraph's logs (press ▶ Run in GRC)",
           "stopped": f"flowgraph stopped? no log rows for {fmt(s.get('age_s'), 0)} s",
           "ended": "session ended"}.get(st, st or "")
    col = {"live": "#0ca30c", "idle": "#0ca30c", "waiting": "#fab219", "stopped": "#fab219"}.get(st, MUTED)
    settings = s.get("settings") or {}
    extra = []
    if settings.get("CDP_MOD"):
        extra.append(settings["CDP_MOD"].upper())
    if settings.get("CDP_SAMP_RATE"):
        extra.append(f"{settings['CDP_SAMP_RATE']} S/s")
    sub = (f"node {s.get('node', '?')} · {s.get('host') or ''} · session {s.get('session', '')}"
           + (" · " + " · ".join(extra) if extra else ""))
    return col, txt, sub


# ======================================================================================== Qt window
try:
    from PyQt5.QtCore import Qt, QTimer, QRectF, QPointF
    from PyQt5.QtGui import QPainter, QPen, QColor, QFont, QFontMetrics, QBrush, QPainterPath
    from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QComboBox,
                                 QFrame, QScrollArea, QTabWidget, QTableWidget, QTableWidgetItem, QPlainTextEdit,
                                 QHeaderView, QSizePolicy, QAbstractItemView)
    HAVE_QT = True
except ImportError:
    HAVE_QT = False

if HAVE_QT:
    class Tile(QFrame):
        def __init__(self):
            super().__init__()
            self.setObjectName("tile")
            self.setStyleSheet(f"#tile {{ background: white; border: 1px solid {GRID}; border-radius: 8px; }}")
            lay = QVBoxLayout(self)
            lay.setContentsMargins(10, 8, 10, 8)
            lay.setSpacing(2)
            top = QHBoxLayout()
            self.k = QLabel("")
            self.k.setStyleSheet(f"color: {TEXT2}; font-size: 12px;")
            self.st = QLabel("")
            self.st.setStyleSheet("font-size: 12px;")
            top.addWidget(self.k, 1)
            top.addWidget(self.st, 0, Qt.AlignRight)
            lay.addLayout(top)
            self.v = QLabel("")
            self.v.setStyleSheet(f"color: {TEXT}; font-size: 22px; font-weight: 600;")
            lay.addWidget(self.v)
            self.n = QLabel("")
            self.n.setWordWrap(True)
            self.n.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
            lay.addWidget(self.n)
            self.setMinimumWidth(150)

        def set(self, label, value, unit, st, note):
            col, icon, word = STATUS.get(st, STATUS["none"])
            self.k.setText(label)
            self.st.setText(f"<span style='color:{col}'>{icon} {word}</span>")
            self.v.setText(f"{html.escape(value)} <span style='font-size:12px; font-weight:400; color:{TEXT2}'>"
                           f"{html.escape(unit)}</span>")
            self.n.setText(html.escape(note))

    class Chart(QWidget):
        """one y axis, 1-3 series, gaps where there is no data, hover read-out"""

        def __init__(self, spec):
            super().__init__()
            self.spec = spec
            self.xs, self.ser, self.lo, self.hi, self.t0 = [], [], 0, 1, 0.0
            self.hover = None
            self.setMouseTracking(True)
            self.setMinimumHeight(200)
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        def set_data(self, xs, ser, lo, hi, t0):
            self.xs, self.ser, self.lo, self.hi, self.t0 = xs, ser, lo, max(hi, lo + 1), t0
            self.update()

        def mouseMoveEvent(self, e):
            self.hover = e.pos().x()
            self.update()

        def leaveEvent(self, e):
            self.hover = None
            self.update()

        def _plot(self):
            return QRectF(52, 56, max(10, self.width() - 64), max(10, self.height() - 56 - 26))

        def paintEvent(self, e):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            p.fillRect(self.rect(), QColor("white"))
            p.setPen(QPen(QColor(GRID), 1))
            p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 8, 8)
            _, title, note, unit, _, fixed, keys = self.spec
            f = QFont(self.font())
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor(TEXT))
            p.drawText(QPointF(12, 19), title)
            f2 = QFont(self.font())
            f2.setPointSizeF(max(7.0, f2.pointSizeF() - 1.5))
            p.setFont(f2)
            fm = QFontMetrics(f2)
            p.setPen(QColor(MUTED))
            p.drawText(QPointF(12 + QFontMetrics(f).horizontalAdvance(title) + 8, 19), f"{note} ({unit})")
            # legend with the latest value
            x = 12.0
            for i, (label, ys) in enumerate(self.ser):
                last = next((v for v in reversed(ys) if v is not None), None)
                txt = f"{label}: {fmt(last, 2 if last is not None and abs(last) < 10 else 1)}"
                p.fillRect(QRectF(x, 31, 10, 10), QColor(SERIES[i % len(SERIES)]))
                p.setPen(QColor(TEXT2))
                p.drawText(QPointF(x + 14, 40), txt)
                x += 14 + fm.horizontalAdvance(txt) + 16
            r = self._plot()
            vals = [v for _, ys in self.ser for v in ys if v is not None]
            top = fixed if fixed else nice_max(max(vals) if vals else None)
            # grid and y labels
            for k in range(5):
                y = r.bottom() - r.height() * k / 4
                p.setPen(QPen(QColor(GRID), 1))
                p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
                p.setPen(QColor(MUTED))
                v = top * k / 4
                lab = fmt(v, 0 if top >= 20 else (1 if top >= 2 else 2))
                p.drawText(QRectF(0, y - 8, r.left() - 6, 16), Qt.AlignRight | Qt.AlignVCenter, lab)
            # x labels: start, middle, end (clock time)
            span = self.hi - self.lo
            for frac, align in ((0.0, Qt.AlignLeft), (0.5, Qt.AlignHCenter), (1.0, Qt.AlignRight)):
                t = self.lo + span * frac
                xx = r.left() + r.width() * frac
                w = 80
                box = QRectF(xx - (0 if align == Qt.AlignLeft else (w if align == Qt.AlignRight else w / 2)),
                             r.bottom() + 4, w, 16)
                p.drawText(box, align | Qt.AlignTop, clock(t, self.t0))
            if not vals:
                p.setPen(QColor(MUTED))
                p.drawText(r, Qt.AlignCenter, "no data in this window")
                p.end()
                return

            def px(t):
                return r.left() + (t - self.lo) / span * r.width()

            def py(v):
                return r.bottom() - min(1.0, max(0.0, v / top)) * r.height()
            for i, (label, ys) in enumerate(self.ser):
                path = QPainterPath()
                pen_down = False
                for t, v in zip(self.xs, ys):
                    if v is None or t is None:
                        pen_down = False
                        continue
                    pt = QPointF(px(t), py(v))
                    if pen_down:
                        path.lineTo(pt)
                    else:
                        path.moveTo(pt)
                        pen_down = True
                pen = QPen(QColor(SERIES[i % len(SERIES)]), 2)
                pen.setJoinStyle(Qt.RoundJoin)
                pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen)
                p.setBrush(Qt.NoBrush)
                p.drawPath(path)
            # hover read-out
            if self.hover is not None and r.left() <= self.hover <= r.right() and self.xs:
                t = self.lo + (self.hover - r.left()) / r.width() * span
                j = min(range(len(self.xs)), key=lambda k: abs((self.xs[k] if self.xs[k] is not None else 1e18) - t))
                xx = px(self.xs[j])
                p.setPen(QPen(QColor(TEXT2), 1, Qt.DashLine))
                p.drawLine(QPointF(xx, r.top()), QPointF(xx, r.bottom()))
                lines = [clock(self.xs[j], self.t0)] + [f"{label}: {fmt(ys[j], 2)} {unit}" for label, ys in self.ser]
                bw = max(fm.horizontalAdvance(s) for s in lines) + 16
                bh = fm.height() * len(lines) + 10
                bx = xx + 10 if xx + 10 + bw < self.width() else xx - 10 - bw
                p.setPen(QPen(QColor(GRID), 1))
                p.setBrush(QBrush(QColor(SURFACE)))
                p.drawRoundedRect(QRectF(bx, r.top() + 4, bw, bh), 6, 6)
                for k, s in enumerate(lines):
                    if k:
                        p.fillRect(QRectF(bx + 6, r.top() + 9 + fm.height() * k + 3, 6, 6),
                                   QColor(SERIES[(k - 1) % len(SERIES)]))
                    p.setPen(QColor(TEXT if k else TEXT2))
                    p.drawText(QPointF(bx + (16 if k else 8), r.top() + 8 + fm.ascent() + fm.height() * k), s)
            p.end()

    def _table(headers):
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setSelectionMode(QAbstractItemView.NoSelection)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        t.setMinimumHeight(180)
        return t

    def _fill(t, rows):
        t.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, v in enumerate(row):
                t.setItem(i, j, QTableWidgetItem("" if v is None else str(v)))

    class DashboardWindow(QWidget):
        def __init__(self, metrics, title="CDP Link - performance"):
            super().__init__()
            self.m = metrics
            self.setWindowTitle(title)
            self.resize(1240, 900)
            self.setStyleSheet(f"DashboardWindow {{ background: {SURFACE}; }}")
            root = QVBoxLayout(self)
            root.setContentsMargins(14, 10, 14, 10)
            top = QHBoxLayout()
            self.dot = QLabel("●")
            self.state = QLabel("")
            self.state.setStyleSheet(f"color: {TEXT}; font-weight: 600;")
            top.addWidget(self.dot)
            top.addWidget(self.state)
            top.addStretch(1)
            top.addWidget(QLabel("Time window"))
            self.win = QComboBox()
            for label, sec in WINDOWS:
                self.win.addItem(label, sec)
            self.win.setCurrentIndex(1)
            self.win.currentIndexChanged.connect(self.refresh)
            top.addWidget(self.win)
            root.addLayout(top)
            self.sub = QLabel("")
            self.sub.setStyleSheet(f"color: {TEXT2};")
            root.addWidget(self.sub)
            self.notice = QLabel("")
            self.notice.setWordWrap(True)
            self.notice.setStyleSheet("background: white; border: 1px solid #fab219; border-left: 4px solid #fab219; "
                                      "border-radius: 6px; padding: 6px 10px;")
            self.notice.hide()
            root.addWidget(self.notice)

            body = QWidget()
            bl = QVBoxLayout(body)
            bl.setContentsMargins(0, 6, 0, 0)
            tg = QGridLayout()
            tg.setSpacing(8)
            self.tiles = []
            for i in range(len(TILES) + 1):
                t = Tile()
                self.tiles.append(t)
                tg.addWidget(t, i // 5, i % 5)
            bl.addLayout(tg)
            cg = QGridLayout()
            cg.setSpacing(10)
            self.charts = []
            for i, spec in enumerate(CHARTS):
                c = Chart(spec)
                self.charts.append(c)
                cg.addWidget(c, i // 2, i % 2)
            bl.addLayout(cg)
            self.tabs = QTabWidget()
            self.t_nodes = _table(["node", "computer", "state", "last heard", "key fingerprint"])
            self.t_msgs = _table(["time", "dir", "node", "size", "took", "kbit/s", "result"])
            self.ev = QPlainTextEdit()
            self.ev.setReadOnly(True)
            self.sec = QLabel("")
            self.sec.setWordWrap(True)
            self.sec.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            self.sec.setTextFormat(Qt.RichText)
            self.tabs.addTab(self.t_msgs, "Reliable messages")
            self.tabs.addTab(self.t_nodes, "Nodes")
            self.tabs.addTab(self.ev, "Events")
            self.tabs.addTab(self.sec, "Encryption & voice")
            self.tabs.setMinimumHeight(240)
            bl.addWidget(self.tabs)
            sc = QScrollArea()
            sc.setWidgetResizable(True)
            sc.setFrameShape(QFrame.NoFrame)
            sc.setWidget(body)
            root.addWidget(sc, 1)
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.refresh)
            self.refresh()

        def showEvent(self, e):
            self.timer.start(1000)
            super().showEvent(e)

        def hideEvent(self, e):
            self.timer.stop()
            super().hideEvent(e)

        def refresh(self):
            try:
                s = self.m.snapshot(self.win.currentData() or 0)
            except Exception as ex:                       # show it instead of going silent
                import traceback
                self.notice.setText(f"<b>Dashboard error</b>: {html.escape(str(ex))}<br><small>"
                                    f"{html.escape(traceback.format_exc()[-600:])}</small>")
                self.notice.show()
                return
            col, txt, sub = state_line(s)
            self.dot.setStyleSheet(f"color: {col}; font-size: 16px;")
            self.state.setText(f"{txt} · updated {time.strftime('%H:%M:%S')}")
            self.sub.setText(html.escape(sub))
            note = s.get("note") or ""
            self.notice.setText(html.escape(note))
            self.notice.setVisible(bool(note))
            for t, v in zip(self.tiles, tiles(s)):
                t.set(*v)
            t0 = s.get("t0") or time.time()
            for c in self.charts:
                xs, ser = series(s, c.spec)
                c.set_data(xs, ser, s.get("lo", 0), s.get("hi", 1), t0)
            self._tables(s, t0)

        def _tables(self, s, t0):
            now = time.time()
            nodes = s.get("nodes") or []
            _fill(self.t_nodes, [(n.get("node"), n.get("host") or "–", "online" if n.get("online") else "away",
                                  "–" if n.get("last_heard_s") is None else f"{fmt(n['last_heard_s'], 0)} s ago",
                                  n.get("fingerprint") or "no key yet") for n in nodes])
            rows = []
            for r in reversed(s.get("msgs") or []):
                t = cdp_metrics._f(r.get("t"))
                kbps = cdp_metrics._f(r.get("kBps"))
                rows.append((clock(t, t0) if t is not None else "–", r.get("dir"), r.get("peer"),
                             f"{fmt((cdp_metrics._f(r.get('bytes')) or 0) / 1000, 1)} kB",
                             f"{fmt(cdp_metrics._f(r.get('dur_s')), 1)} s" if r.get("dur_s") else "–",
                             fmt(kbit(kbps), 1) if kbps is not None else "–", r.get("res")))
            _fill(self.t_msgs, rows)
            lines = []
            for line in reversed(s.get("events") or []):
                head, _, rest = line.partition(" ")
                t = cdp_metrics._f(head)
                lines.append((clock(t, t0) if t is not None else head) + " " + rest)
            text = "\n".join(lines) or "none"
            if text != self.ev.toPlainText():
                self.ev.setPlainText(text)
            c, v = s.get("crypto"), s.get("voice")
            parts = []
            if c:
                parts.append(f"<b>Encryption</b>: {'on' if c.get('on') else 'off'} · {html.escape(str(c.get('method')))}"
                             f"<br>my fingerprint <code>{html.escape(str(c.get('fingerprint')))}</code> · nodes with a "
                             f"key {html.escape(str(c.get('peers_keyed')))} · sent / received encrypted "
                             f"{html.escape(str(c.get('sealed')))} / {html.escape(str(c.get('opened')))}"
                             + (f" · {c.get('failed')} failed" if c.get("failed") else ""))
            if v:
                parts.append(f"<b>Voice</b>: {html.escape(str(v.get('audio')))}<br>frames sent / received "
                             f"{v.get('tx_frames')} / {v.get('rx_frames')} · lost / too late / filled in {v.get('lost')} / "
                             f"{v.get('late')} / {v.get('plc')} · rejected (bad tag / no key) {v.get('auth_fail')} / "
                             f"{v.get('no_key')}")
            self.sec.setText("<br><br>".join(parts) or "<span style='color:#8a8984'>only shown when opened from the "
                                                       "chat window</span>")


def main():
    import argparse
    ap = argparse.ArgumentParser(description="CDP link performance dashboard for a session folder")
    ap.add_argument("session", help="a session folder, e.g. debug/20261019_153000_node0")
    a = ap.parse_args()
    if not HAVE_QT:
        raise SystemExit("PyQt5 not found. Linux: sudo apt install python3-pyqt5 ; macOS (radioconda): conda install pyqt")
    app = QApplication(sys.argv)
    m = cdp_metrics.Metrics(a.session, search=[])
    w = DashboardWindow(m, f"CDP Link - performance - {Path(a.session).name}")
    w.show()
    app.exec_()


if __name__ == "__main__":
    main()
