#!/usr/bin/env python3
"""
test_discovery.py - cdp_daemon.py: the login asks only for this PC's node number; the nodes find each other by presence broadcasts.
Runs the daemon's real Link + ChatCore (no Qt, no radio) against a fake App Socket Bridge (TCP, unicast and
broadcast): two nodes find each other, a third joins later, a chat message goes to a discovered node, and two PCs
that use the same node number notice it.

    python3 tools/test_discovery.py
"""
import os, sys, time, socket, struct, select, threading, tempfile, queue
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["CDP_PRESENCE_S"] = "2"
import cdp_daemon as D

class Bridge:
    def __init__(self, ports):        # node -> tcp port
        self.ports, self.cli, self.srv = ports, {n: [] for n in ports}, {}
        for n, p in ports.items():
            s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", p)); s.listen(4); self.srv[s] = n
        self.bufs = {}; self.sent = 0
        threading.Thread(target=self.loop, daemon=True).start()
    def fr(self, b): return len(b).to_bytes(4, "big") + b
    def loop(self):
        while True:
            r, _, _ = select.select(list(self.srv) + list(self.bufs), [], [], 0.1)
            for s in r:
                if s in self.srv:
                    n = self.srv[s]; c, _ = s.accept(); self.cli[n].append(c); self.bufs[c] = (n, bytearray())
                    c.sendall(self.fr(struct.pack(">BBHBI", 0x80, n % 1000 if n < 255 else 7, 1000, 16, 16000000)))
                    continue
                n, buf = self.bufs[s]
                try: ch = s.recv(65536)
                except OSError: ch = b""
                if not ch:
                    del self.bufs[s]; self.cli[n].remove(s); continue
                buf += ch
                while len(buf) >= 4 and len(buf) >= 4 + int.from_bytes(buf[:4], "big"):
                    k = int.from_bytes(buf[:4], "big"); body = bytes(buf[4:4 + k]); del buf[:4 + k]
                    if body[0] != 1: continue
                    ref, dest = struct.unpack(">IB", body[1:6]); data = body[6:]
                    self.sent += 1
                    src = n if n < 255 else 7
                    for m, cl in self.cli.items():
                        mid = m if m < 255 else 7
                        if m == n or (dest != 255 and dest != mid): continue
                        for c in cl: c.sendall(self.fr(bytes([0x81, src]) + data))
                    s.sendall(self.fr(struct.pack(">BIB", 0x82, ref, dest)))

# nodes 0 and 1, and a late node 3; node "1007" = a second PC that also calls itself node 7 (conflict with 7)
ports = {0: 46200, 1: 46201, 3: 46203, 7: 46207, 1007: 46208}
br = Bridge(ports)
cores = {}
def make(n, me):
    tmp = tempfile.mkdtemp(prefix="cdp_disc_")
    s = D.Session(root=tmp); s.login(me, [], {})
    lk = D.Link(s, port=ports[n]); lk.start()
    c = D.ChatCore(s, lk, me); cores[n] = (c, lk); return c
for n, me in ((0, 0), (1, 1)): make(n, me)
def run(secs):
    end = time.time() + secs
    while time.time() < end:
        for n, (c, lk) in list(cores.items()):
            while True:
                try: ev = lk.events.get_nowait()
                except queue.Empty: break
                if ev[0] == "hello": c.on_connected(); continue
                if ev[0] == "state": continue
                c.on_event(ev)
            c.tick(time.time())
        time.sleep(0.05)
t0 = time.time(); run(3.0)
ok = []
def check(name, cond, info=""):
    ok.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name, info)
check("nodes 0 and 1 find each other", cores[0][0].opponents == [1] and cores[1][0].opponents == [0],
      f"{cores[0][0].opponents} {cores[1][0].opponents}")
make(3, 3); run(4.0)
check("a node that joins later is found by both, and finds both", sorted(cores[0][0].opponents) == [1, 3] and
      sorted(cores[1][0].opponents) == [0, 3] and sorted(cores[3][0].opponents) == [0, 1],
      f"{cores[0][0].opponents} {cores[1][0].opponents} {cores[3][0].opponents}")
c0, c3 = cores[0][0], cores[3][0]
m = c0.send_text(3, "hello node 3"); run(1.0)
got = [x.text for x in c3.chats[0]]
check("a chat message to a discovered node arrives (presence is not shown as a message)", got == ["hello node 3"] and
      m.status == "sent", f"{got} {m.status}")
check("presence period ~2 s", 2 <= c0.presence_sent <= 6, f"sent {c0.presence_sent} in ~8 s")
check("online marks", c0.online(1) and c0.online(3))
make(7, 7); make(1007, 7); run(4.0)
check("two PCs with the same node number see the conflict", cores[7][0].conflict is not None and
      cores[1007][0].conflict is not None, f"{cores[7][0].conflict} {cores[1007][0].conflict}")
info = open(os.path.join(c0.s.dir, "session.json")).read()
check("session.json lists the nodes heard", '"peers"' in info and '"3"' in info)
print("ALL PASS" if all(ok) else "FAILURES")
os._exit(0 if all(ok) else 1)
