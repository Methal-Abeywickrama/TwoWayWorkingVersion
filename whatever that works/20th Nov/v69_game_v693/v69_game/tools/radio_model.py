"""
radio_model.py - millisecond-step model of the CDP v6.8 CSMA/CA DCF + selective-repeat ARQ MAC and a
shared half-duplex channel, used by worstcase_sim.py. Same rules and default timing as epy_csma.py:

  p-persistence 0.04 per 2 ms slot -> DIFS 30 ms -> backoff CW 15..1023 (doubles per failed TXOP, k <= 5)
  -> 20 ms framer + TX latency (the channel still looks idle to others, so collisions happen)
  -> frame on air for its exact airtime (Hamming 31, 128-byte preamble, 4096-sample buffers, 1 Msps)
  -> unicast: Block ACK on air 26 ms after the burst, timeout 70 ms, 7 retries, window N 16, burst M 4
  -> broadcast: one frame per TXOP, no ACK.  Queue limit 256.  5 ms decode latency.
Overheard unicast bursts set the NAV. Overlapping transmissions are lost at every receiver (no capture).

newest_only=True models the Game Link patch of the MAC:
  - a game frame still waiting for the same destination and of the same kind (BEACON / INPUT / SNAPSHOT, or
    the old state packet) gets the new payload instead of queueing;
  - game frames whose payload is older than GAME_MAX_AGE_MS are given up (useless for a real-time game);
  - the contention window goes back to CWmin after a broadcast and after a frame was given up at the retry
    limit (as 802.11 does). Without this a node that once failed unicasts (e.g. to a host that switched off)
    keeps CW 511-1023 for every later broadcast, because only a received Block ACK reset it.
"""
import collections
import math

BCAST = 255
GAME_MAX_AGE_MS = 2000


def air_ms(app_len: int, hdr: int = 2) -> int:
    """Airtime in ms of a frame carrying app_len bytes of game payload (+ hdr bytes link tag / bridge header
    + 13 MAC header): hdr 2 = v6.8 Game Link tag, 11 = v6.9 bridge datagram header."""
    plen = app_len + hdr + 13
    bits = math.ceil(56 / 26) * 31 + math.ceil((plen + 4) * 8 / 26) * 31
    raw = 128 + 4 + 4 + math.ceil(bits / 8)
    unit = 4096 // 32
    nbytes = math.ceil((raw + unit) / unit) * unit
    return int(round(nbytes * 32 / 1e6 * 1000))


# ------------------------------------------------------------------ v6.8 timing (ms)
SLOT, P_PERSIST, DIFS = 2, 0.04, 30
CWMIN, CWMAX, KMAX = 15, 1023, 5
RETRY, BA_TO, SIFS, TURN = 7, 70, 1, 25
BA_AIR, LAT, DEC = 12, 20, 5
M, N, MAXQ = 4, 16, 256
MASK = 0xFFFF


def sdiff(a, b):
    d = (a - b) & MASK
    return d - 0x10000 if d >= 0x8000 else d


def cw_for(k):
    return min(CWMAX, (CWMIN + 1) * (2 ** min(k, KMAX)) - 1)


# ------------------------------------------------------------------ ARQ sessions (same rules as epy_csma.py)
class TxS:
    def __init__(self):
        self.ws = self.next_sn = 0
        self.frames = {}
        self.backlog = collections.deque()

    def admit(self):
        while self.backlog and sdiff(self.next_sn, self.ws) < N:
            payload, app, t = self.backlog.popleft()
            self.frames[self.next_sn] = dict(sn=self.next_sn, payload=payload, app=app, t_enq=t, retries=0,
                                             state="new")
            self.next_sn = (self.next_sn + 1) & MASK

    def in_window(self):
        return [self.frames[(self.ws + i) & MASK] for i in range(sdiff(self.next_sn, self.ws))
                if (self.ws + i) & MASK in self.frames]

    def eligible(self):
        return [r for r in self.in_window() if r["state"] in ("new", "retx")]

    def slide(self):
        while self.ws != self.next_sn:
            r = self.frames.get(self.ws)
            if r is not None and r["state"] not in ("acked", "dropped"):
                break
            self.frames.pop(self.ws, None)
            self.ws = (self.ws + 1) & MASK

    def fail(self, r, st):
        r["retries"] += 1
        if r["retries"] > RETRY:
            r["state"] = "dropped"
            st["retry_drop"] += 1
        else:
            r["state"] = "retx"
            st["retx"] += 1

    def on_ba(self, recs, bits, rws, st):
        for i, r in enumerate(recs):
            if r["state"] in ("acked", "dropped"):
                continue
            if bits >> i & 1 or sdiff(r["sn"], rws) < 0:
                r["state"] = "acked"
            else:
                self.fail(r, st)
        for r in self.in_window():
            if r["state"] not in ("acked", "dropped") and sdiff(r["sn"], rws) < 0:
                r["state"] = "acked"

    def qlen(self):
        return len(self.backlog) + sum(1 for r in self.frames.values() if r["state"] not in ("acked", "dropped"))


class RxS:
    def __init__(self, ws):
        self.rws = ws
        self.buf = {}

    def on_frame(self, sn, ws_sender, item):
        out = []
        if sdiff(self.rws, ws_sender) > N:
            self.buf.clear()
            self.rws = ws_sender
        if sdiff(ws_sender, self.rws) > 0:
            self._flush_to(ws_sender, out)
        d = sdiff(sn, self.rws)
        if d < 0:
            return out
        if d >= N:
            self._flush_to((sn - N + 1) & MASK, out)
        if sn not in self.buf:
            self.buf[sn] = item
        while self.rws in self.buf:
            out.append(self.buf.pop(self.rws))
            self.rws = (self.rws + 1) & MASK
        return out

    def _flush_to(self, new_ws, out):
        while sdiff(new_ws, self.rws) > 0:
            it = self.buf.pop(self.rws, None)
            if it is not None:
                out.append(it)
            self.rws = (self.rws + 1) & MASK
        while self.rws in self.buf:
            out.append(self.buf.pop(self.rws))
            self.rws = (self.rws + 1) & MASK


# ------------------------------------------------------------------ the channel
class Channel:
    def __init__(self, rng, loss=0.0, interference=(), stalls=None, deaf=None):
        self.rng = rng
        self.loss = loss
        self.interference = list(interference)          # [(t0_ms, t1_ms)]
        self.stalls = stalls or {}                       # node -> [(t0, t1)]  TX side never gets the channel
        self.deaf = deaf or {}                           # node -> [(t0, t1)]  receives nothing
        self.on_air = []                                 # active transmissions
        self.nodes = {}
        self.st = collections.Counter()

    @staticmethod
    def _in(windows, t):
        return any(a <= t < b for a, b in windows)

    def interfered(self, t):
        return self._in(self.interference, t)

    def busy_for(self, node, t):
        if self.interfered(t) or self._in(self.stalls.get(node, ()), t):
            return True
        return any(tx["node"] != node for tx in self.on_air)

    def start(self, tx):
        for o in self.on_air:
            o["collided"] = tx["collided"] = True
        self.on_air.append(tx)
        self.st["tx_" + tx["kind"]] += 1

    def tick(self, t):
        done = [tx for tx in self.on_air if tx["end"] <= t]
        if not done:
            return
        self.on_air = [tx for tx in self.on_air if tx["end"] > t]
        for tx in done:
            hit = any(a < tx["end"] and tx["start"] < b for a, b in self.interference)
            if tx["collided"]:
                self.st["collided_" + tx["kind"]] += 1
            for n, node in self.nodes.items():
                if n == tx["node"]:
                    continue
                if tx["collided"] or hit or node.tx_overlap(tx["start"], tx["end"]) \
                        or self._in(self.deaf.get(n, ()), tx["start"]):
                    continue
                ok = [self.rng.random() >= self.loss for _ in tx["frames"]]
                node.rx_due.append((t + DEC, tx, ok))


# ------------------------------------------------------------------ one node's MAC (v6.8 logic)
class Mac:
    def __init__(self, node, ch: Channel, rng, newest_only: bool):
        self.node = node
        self.ch = ch
        self.rng = rng
        self.newest_only = newest_only
        self.bq = collections.deque()
        self.txs = {}
        self.rxs = {}
        self.ph = "persist"
        self.p_first, self.p_acc, self.idle_d, self.bo, self.bo_acc = True, 0, 0, None, 0
        self.k = 0
        self.tx = None
        self.awaiting = None
        self.ba_due = []
        self.nav_until = 0
        self.rr = 0
        self.bsn = 0
        self.bc_sn = 0
        self.rx_due = []
        self.app_in = []                     # delivered to the game: (src, payload)
        self.my_tx = collections.deque(maxlen=8)
        self.st = collections.Counter()
        self.qmax = 0
        self.qdelay = []
        self.p_persist, self.cw_max, self.hdr = P_PERSIST, CWMAX, 2
        ch.nodes[node] = self

    def cw(self):
        return min(self.cw_max, (CWMIN + 1) * (2 ** min(self.k, KMAX)) - 1)

    # ---------------------------------------------------------------- app side
    def qlen(self):
        return len(self.bq) + sum(s.qlen() for s in self.txs.values())

    def enqueue(self, payload, dest, t, app="game"):
        """app: 'text' = never replaced; anything else = a game frame kind (replaced by the same kind)."""
        self.now_ms = t
        if self.newest_only and app != "text" and self._replace(payload, dest, app):
            self.st["replaced"] += 1
            return
        if self.qlen() >= MAXQ:
            self.st["qdrop"] += 1
            return
        if dest == BCAST:
            self.bq.append(dict(payload=payload, app=app, t_enq=t, sn=self.bc_sn))
            self.bc_sn = (self.bc_sn + 1) & MASK
        else:
            s = self.txs.setdefault(dest, TxS())
            s.backlog.append((payload, app, t))
            s.admit()
        self.qmax = max(self.qmax, self.qlen())

    def _replace(self, payload, dest, app):
        """newest-only: the game frame of the same kind still waiting for this destination gets the payload."""
        if dest == BCAST:
            q = [e for e in self.bq if e["app"] == app]
            if not q:
                return False
            q[0]["payload"], q[0]["t_pay"] = payload, self.now_ms
            for e in q[1:]:
                self.bq.remove(e)
            return True
        s = self.txs.get(dest)
        if s is None:
            return False
        pend = [r for r in s.in_window() if r["app"] == app and r["state"] in ("new", "retx")]
        blog = [i for i, b in enumerate(s.backlog) if b[1] == app]
        if not pend and not blog:
            return False
        if pend:
            pend[0]["payload"], pend[0]["t_pay"] = payload, self.now_ms
            for r in pend[1:]:
                r["state"] = "dropped"
            if blog:
                s.backlog = collections.deque(b for b in s.backlog if b[1] != app)
            s.slide()
            s.admit()
        else:
            b = s.backlog[blog[0]]
            s.backlog[blog[0]] = (payload, b[1], self.now_ms)
        return True

    # ---------------------------------------------------------------- helpers
    def tx_overlap(self, a, b):
        return any(s < b and a < e for s, e in self.my_tx)

    def has_work(self):
        return bool(self.bq) or any(s.eligible() for s in self.txs.values())

    def reset(self):
        self.ph, self.p_first, self.p_acc, self.idle_d, self.bo, self.bo_acc = "persist", True, 0, 0, None, 0

    # ---------------------------------------------------------------- receive
    def _rx(self, t):
        due = [d for d in self.rx_due if d[0] <= t]
        if not due:
            return
        self.rx_due = [d for d in self.rx_due if d[0] > t]
        for _, tx, ok in due:
            src = tx["node"]
            if tx["kind"] == "BA":
                if tx["dst"] != self.node or not ok[0]:
                    continue
                a = self.awaiting
                if a is not None and a["dst"] == src and a["bsn"] == tx["bsn"]:
                    self.awaiting = None
                    self.k = 0
                    s = self.txs[src]
                    s.on_ba(a["recs"], tx["bits"], tx["rws"], self.st)
                    s.slide()
                    s.admit()
                    self.reset()
                continue
            if tx["dst"] == BCAST or (tx.get("noack") and tx["dst"] == self.node):
                for f, good in zip(tx["frames"], ok):
                    if good:
                        self.app_in.append((src, f["payload"]))
                continue
            if tx.get("noack"):
                continue
            if tx["dst"] != self.node:
                if any(ok):
                    self.nav_until = max(self.nav_until, tx["end"] + SIFS + TURN + BA_AIR)
                continue
            rs = self.rxs.setdefault(src, RxS(tx["ws"]))
            bits = 0
            for i, (f, good) in enumerate(zip(tx["frames"], ok)):
                if not good:
                    continue
                bits |= 1 << i
                for item in rs.on_frame(f["sn"], tx["ws"], f["payload"]):
                    self.app_in.append((src, item))
            if bits:
                self.ba_due.append(dict(dst=src, bsn=tx["bsn"], bits=bits, rws=rs.rws,
                                        earliest=tx["end"] + SIFS + TURN - LAT))

    # ---------------------------------------------------------------- one ms
    def tick(self, t):
        self._rx(t)
        if self.tx is not None:                     # latency before air, or on air
            tx = self.tx
            if tx["phase"] == "lat" and t >= tx["t_air"]:
                tx["phase"] = "air"
                tx["start"], tx["end"] = t, t + tx["air"]
                self.my_tx.append((tx["start"], tx["end"]))
                self.ch.start(tx)
            elif tx["phase"] == "air" and t >= tx["end"]:
                self.tx = None
                if tx["kind"] == "BA":                 # as _after_own_tx(): contention starts over at DIFS
                    if self.ph == "backoff":
                        self.ph = "difs"
                    self.idle_d, self.p_acc, self.p_first = 0, 0, True
                elif tx["dst"] == BCAST or tx.get("noack"):
                    if self.newest_only:
                        self.k = 0
                    self.reset()
                else:
                    self.awaiting = dict(dst=tx["dst"], bsn=tx["bsn"], recs=tx["recs"], deadline=tx["end"] + BA_TO)
            return
        if self.awaiting is not None and t >= self.awaiting["deadline"]:
            a, self.awaiting = self.awaiting, None
            self.k = min(self.k + 1, KMAX)
            self.st["ba_timeout"] += 1
            s = self.txs[a["dst"]]
            for r in a["recs"]:
                if r["state"] == "out":
                    s.fail(r, self.st)
                    if self.newest_only and r["state"] == "dropped":
                        self.k = 0
            s.slide()
            s.admit()
            self.reset()
        if self.ba_due:
            ba = min(self.ba_due, key=lambda b: b["earliest"])
            if t >= ba["earliest"]:
                self.ba_due.remove(ba)
                self.tx = dict(kind="BA", phase="lat", t_air=t + LAT, air=BA_AIR, node=self.node, dst=ba["dst"],
                               bsn=ba["bsn"], bits=ba["bits"], rws=ba["rws"], frames=[None], collided=False)
            return
        if self.newest_only and t % 50 == 0:
            self._age_out(t)
        if self.awaiting is not None or not self.has_work():
            if not self.has_work():
                self.reset()
            return
        busy = self.ch.busy_for(self.node, t) or t < self.nav_until
        if self.ph == "persist":
            if busy:
                self.p_first, self.p_acc = True, 0
                return
            draw = False
            if self.p_first:
                self.p_first, draw = False, True
            else:
                self.p_acc += 1
                if self.p_acc >= SLOT:
                    self.p_acc -= SLOT
                    draw = True
            if draw and self.rng.random() < self.p_persist:
                self.ph, self.idle_d = "difs", 0
            return
        if self.ph == "difs":
            if busy:
                self.idle_d = 0
                return
            self.idle_d += 1
            if self.idle_d < DIFS:
                return
            self.ph, self.bo_acc = "backoff", 0
            if self.bo is None:
                self.bo = self.rng.randint(0, self.cw())
        if self.ph == "backoff":
            if busy:
                self.ph, self.idle_d = "difs", 0
                return
            if self.bo > 0:
                self.bo_acc += 1
                while self.bo_acc >= SLOT and self.bo > 0:
                    self.bo_acc -= SLOT
                    self.bo -= 1
            if self.bo == 0:
                self._start_txop(t)

    def _age_out(self, t):
        for e in [e for e in self.bq if e["app"] != "text" and t - e.get("t_pay", e["t_enq"]) > GAME_MAX_AGE_MS]:
            self.bq.remove(e)
            self.st["aged_out"] += 1
        for s in self.txs.values():
            hit = False
            for r in s.in_window():
                if r["app"] != "text" and r["state"] in ("new", "retx") and \
                        t - r.get("t_pay", r["t_enq"]) > GAME_MAX_AGE_MS:
                    r["state"] = "dropped"
                    self.st["aged_out"] += 1
                    hit = True
            if hit:
                s.slide()
                s.admit()

    def _start_txop(self, t):
        dsts = sorted(d for d, s in self.txs.items() if s.eligible())
        bc_first = self.bq and (not dsts or self.bq[0]["t_enq"] <= min(
            min(r["t_enq"] for r in self.txs[d].eligible()) for d in dsts))
        self.bsn = (self.bsn + 1) & MASK
        if bc_first:
            b = self.bq.popleft()
            self.qdelay.append(t - b.get("t_pay", b["t_enq"]))
            frames, dst, recs, ws = [dict(sn=b["sn"], payload=b["payload"])], BCAST, [], 0
            self.st["air_ms"] += air_ms(len(b["payload"]), self.hdr)
        else:
            if not dsts:
                self.reset()
                return
            dst = dsts[self.rr % len(dsts)]
            self.rr += 1
            s = self.txs[dst]
            recs = s.eligible()[:M]
            for r in recs:
                r["state"] = "out"
                self.qdelay.append(t - r.get("t_pay", r["t_enq"]))
            frames = [dict(sn=r["sn"], payload=r["payload"]) for r in recs]
            self.st["air_ms"] += sum(air_ms(len(f["payload"]), self.hdr) for f in frames)
            ws = s.ws
        self.tx = dict(kind="D", phase="lat", t_air=t + LAT, air=sum(air_ms(len(f["payload"]), self.hdr) for f in frames),
                       node=self.node, dst=dst,
                       bsn=self.bsn, recs=recs, frames=frames, ws=ws, collided=False)


class SimLink:
    """What the game sees: the Game Link block in front of the MAC. kind(payload) -> replacement key."""

    def __init__(self, mac: Mac, clock, kind=lambda data: "game", force_bcast=False):
        self.mac = mac
        self.clock = clock
        self.kind = kind
        self.force_bcast = force_bcast
        self.desc = "sim"

    def send(self, data, dest):
        self.mac.enqueue(bytes(data), BCAST if self.force_bcast else dest, self.clock(), app=self.kind(data))

    def poll(self):
        out, self.mac.app_in = self.mac.app_in, []
        return out




# ------------------------------------------------------------------ v6.9 (cdp_link with the socket bridge)
class Mac69(Mac):
    """The v6.9 MAC as the bridge uses it: every datagram (bridge 'U' header, 11 B) and every broadcast goes into
    a FIFO queue per destination of udp_queue frames (the oldest is dropped when it is full); one TXOP sends up to M
    of them to one destination, without Block ACK. Reliable messages (send_reliable) use the SR-ARQ as before.
    Default timing of v6.9.1: p-persistence 0.2, CWmax 63. No newest-only, no aging: the stack is unchanged."""

    def __init__(self, node, ch, rng, udp_queue=32):
        super().__init__(node, ch, rng, newest_only=False)
        self.p_persist, self.cw_max, self.hdr = 0.2, 63, 11
        self.uq = {}
        self.u_sn = collections.Counter()
        self.udp_queue = udp_queue

    def qlen(self):
        return sum(len(q) for q in self.uq.values()) + sum(s.qlen() for s in self.txs.values())

    def enqueue(self, payload, dest, t, app="game", reliable=False):
        self.now_ms = t
        if self.qlen() >= MAXQ:
            self.st["qdrop"] += 1
            return
        if reliable and dest != BCAST:
            s = self.txs.setdefault(dest, TxS())
            s.backlog.append((payload, app, t))
            s.admit()
        else:
            q = self.uq.setdefault(dest, collections.deque())
            if len(q) >= self.udp_queue:
                q.popleft()
                self.st["udp_drop"] += 1
            q.append(dict(payload=payload, app=app, t_enq=t, sn=self.u_sn[dest]))
            self.u_sn[dest] += 1
        self.qmax = max(self.qmax, self.qlen())

    def has_work(self):
        return any(self.uq.values()) or any(s.eligible() for s in self.txs.values())

    def _start_txop(self, t):
        cands = [(q[0]["t_enq"], 1, d) for d, q in self.uq.items() if q]
        cands += [(min(r["t_enq"] for r in s.eligible()), 0, d) for d, s in self.txs.items() if s.eligible()]
        if not cands:
            self.reset()
            return
        _, unrel, dst = min(cands)
        self.bsn = (self.bsn + 1) & MASK
        if unrel:
            q = self.uq[dst]
            frames = []
            while q and len(frames) < M:
                b = q.popleft()
                self.qdelay.append(t - b["t_enq"])
                frames.append(dict(sn=b["sn"], payload=b["payload"]))
            recs, ws = [], 0
        else:
            s = self.txs[dst]
            recs = s.eligible()[:M]
            for r in recs:
                r["state"] = "out"
                self.qdelay.append(t - r["t_enq"])
            frames = [dict(sn=r["sn"], payload=r["payload"]) for r in recs]
            ws = s.ws
        air = sum(air_ms(len(f["payload"]), self.hdr) for f in frames)
        self.st["air_ms"] += air
        self.tx = dict(kind="D", phase="lat", t_air=t + LAT, air=air, node=self.node, dst=dst, bsn=self.bsn,
                       recs=recs, frames=frames, ws=ws, collided=False, noack=bool(unrel) and dst != BCAST)


class SimLink69:
    """What the game sees on v6.9: the bridge's datagram service (and, for send_reliable, its ARQ messages)."""

    def __init__(self, mac: Mac69, clock):
        self.mac, self.clock, self.desc = mac, clock, "sim v6.9"

    def send(self, data, dest):
        self.mac.enqueue(bytes(data), dest, self.clock())

    def send_reliable(self, data, dest):
        self.mac.enqueue(bytes(data), dest, self.clock(), reliable=True)

    def poll(self):
        out, self.mac.app_in = self.mac.app_in, []
        return out
