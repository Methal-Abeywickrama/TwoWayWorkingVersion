#!/usr/bin/env python3
"""Loopback test of game_core.BridgeLink against a fake v6.9 App Socket Bridge (no GNU Radio needed).

The fake bridge speaks the bridge's socket protocol exactly as cdp_sock.py documents it:
  datagrams  in: dest 1 B | data   on udp_port      out: src 1 B | dest 1 B | data  on udp_out_port
  reliable   TCP: 4 B big-endian length + body;  HELLO 80, SEND 01 -> RECV 81 at the receiver + SENT 82
Each node gets its own ports (one PC per node in reality) and the fake 'air' relays between them,
dropping `loss` of the datagrams. It also injects chat messages (MAGIC b'CDPC2\\0' ...) to check the game
ignores them.

    python3 tools/test_bridge_link.py              # 3 bots, datagrams, then reliable inputs, ~2 x 20 s
"""
import os
import random
import select
import socket
import struct
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "game"))
import game_core as gc                                     # noqa: E402

BASE = 46000


class FakeBridge:
    def __init__(self, nodes, loss=0.05, seed=1):
        self.nodes = nodes
        self.loss = loss
        self.rng = random.Random(seed)
        self.stop = threading.Event()
        self.udp_in, self.tcp_srv, self.tcp_cli = {}, {}, {n: [] for n in nodes}
        self.out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.stats = {"udp": 0, "udp_lost": 0, "rel": 0, "chat": 0}
        self.lock = threading.Lock()
        for n in nodes:
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            u.bind(("127.0.0.1", self.ports(n)[0]))
            self.udp_in[u] = n
            t = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            t.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            t.bind(("127.0.0.1", self.ports(n)[2]))
            t.listen(4)
            self.tcp_srv[t] = n
        self.bufs = {}
        threading.Thread(target=self.loop, daemon=True).start()

    @staticmethod
    def ports(n):
        return BASE + 10 * n, BASE + 10 * n + 1, BASE + 10 * n + 2          # udp in, udp out, arq tcp

    def _frame(self, body):
        return len(body).to_bytes(4, "big") + body

    def deliver_rel(self, src, dest, data):
        for c in self.tcp_cli.get(dest, []):
            try:
                c.sendall(self._frame(bytes([0x81, src]) + data))
            except OSError:
                pass

    def loop(self):
        while not self.stop.is_set():
            socks = list(self.udp_in) + list(self.tcp_srv) + list(self.bufs)
            r, _, _ = select.select(socks, [], [], 0.1)
            for s in r:
                if s in self.udp_in:
                    src = self.udp_in[s]
                    d, _ = s.recvfrom(65535)
                    dest, data = d[0], d[1:]
                    for n in self.nodes:
                        if n == src or (dest != 255 and dest != n):
                            continue
                        self.stats["udp"] += 1
                        if self.rng.random() < self.loss:
                            self.stats["udp_lost"] += 1
                            continue
                        self.out.sendto(bytes([src, dest]) + data, ("127.0.0.1", self.ports(n)[1]))
                elif s in self.tcp_srv:
                    n = self.tcp_srv[s]
                    c, _ = s.accept()
                    self.tcp_cli[n].append(c)
                    self.bufs[c] = (n, bytearray())
                    c.sendall(self._frame(struct.pack(">BBHBI", 0x80, n, 1000, 16, 16000000)))
                else:
                    n, buf = self.bufs[s]
                    try:
                        chunk = s.recv(65536)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        del self.bufs[s]
                        self.tcp_cli[n].remove(s)
                        continue
                    buf += chunk
                    while len(buf) >= 4 and len(buf) >= 4 + int.from_bytes(buf[:4], "big"):
                        k = int.from_bytes(buf[:4], "big")
                        body = bytes(buf[4:4 + k])
                        del buf[:4 + k]
                        if body[0] == 0x01:
                            ref, dest = struct.unpack(">IB", body[1:6])
                            self.stats["rel"] += 1
                            self.deliver_rel(n, dest, body[6:])
                            s.sendall(self._frame(struct.pack(">BIB", 0x82, ref, dest)))

    def chat_noise(self):
        """A chat message from node 1 to every other node (as the daemon would send it)."""
        raw = b"CDPC2\0" + (12).to_bytes(2, "big") + b'{"kind":"x"}' + b"hello from the chat"
        for n in self.nodes:
            if n != 1:
                self.deliver_rel(1, n, raw)
                self.deliver_rel(1, n, "plain text message".encode())
        self.stats["chat"] += 1

    def close(self):
        self.stop.set()
        time.sleep(0.15)
        for s in list(self.udp_in) + list(self.tcp_srv) + list(self.bufs):
            s.close()


def run(arq, seconds=20.0, nodes=(0, 1, 3)):
    fb = FakeBridge(nodes)
    links, players, bots = {}, {}, {}
    for n in nodes:
        up, uo, ap = FakeBridge.ports(n)
        links[n] = gc.BridgeLink(n, up, uo, ap)
        players[n] = gc.Node(n, links[n], name=f"N{n}", rate=5, max_players=4,
                             arq=arq[n] if isinstance(arq, dict) else arq,
                             clock_offset=random.uniform(0, 60))
        bots[n] = gc.BotBrain(players[n], seed=n, skill=0.7)
    t0 = last = time.monotonic()
    joined_at = None
    while time.monotonic() - t0 < seconds:
        now = time.monotonic()
        dt, last = now - last, now
        for n in nodes:
            p = players[n]
            if p.mode != "arena" and now - t0 > 0.5:
                p.join_game()
            bots[n].step(dt)
        if joined_at is None and all(p.mode == "arena" and p.admitted for p in players.values()):
            joined_at = now - t0
        if int((now - t0) * 2) == 10 and fb.stats["chat"] == 0:
            fb.chat_noise()
        time.sleep(1 / 60)
    roles = {n: p.role for n, p in players.items()}
    hosts = {p.host_id for p in players.values()}
    kills = sum(p.me.kills for p in players.values())
    host = players[next(iter(hosts))] if len(hosts) == 1 else None
    world_kills = sum(q.kills for q in host.world.players.values()) if host and host.world else -1
    rel = {n: dict(links[n].rel) for n in nodes}
    ign = {n: links[n].ignored for n in nodes}
    for lk in links.values():
        lk.close()
    fb.close()
    return dict(roles=roles, hosts=hosts, joined_at=joined_at, kills=kills, world_kills=world_kills, rel=rel,
                bridge=fb.stats, ignored=ign, rates={n: p.rate_eff for n, p in players.items()})


def main():
    ok = True
    for arq in (False, True, {0: False, 1: True, 3: False}):
        r = run(arq)
        label = f"CDP_GAME_ARQ={int(arq)}" if not isinstance(arq, dict) else "mixed: only node 1 has CDP_GAME_ARQ=1"
        print(f"\n{label}: {r}")
        checks = [
            ("all three joined one arena", r["joined_at"] is not None and len(r["hosts"]) == 1),
            ("one host, the rest clients", sorted(r["roles"].values()) == ["client", "client", "host"]),
            ("kills happened on the host", r["world_kills"] > 0),
        ]
        if arq:
            checks.append(("inputs went over the reliable path", sum(v.get("sent", 0) for v in r["rel"].values()) > 20
                           and sum(v.get("recv", 0) for v in r["rel"].values()) > 20))
            checks.append(("reliable sends confirmed (SENT)", sum(v.get("sent_ok", 0) for v in r["rel"].values()) > 20))
            checks.append(("chat messages reached the game's socket and were ignored",
                           all(r["rel"][n].get("other", 0) >= 2 for n in (0, 3))))
        else:
            checks.append(("the game never opened the reliable port",
                           sum(v.get("connects", 0) for v in r["rel"].values()) == 0))
        for name, c in checks:
            print(f"  {'PASS' if c else 'FAIL'}  {name}")
            ok &= bool(c)
    print("\nall passed" if ok else "\nSOME CHECKS FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
