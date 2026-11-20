#!/usr/bin/env python3
"""
test_voice_crypto.py - encryption and voice end to end, without radio or sound card:

  * two daemons' chat cores (cdp_daemon.ChatCore) on a fake App Socket Bridge: they find each other, learn each
    other's public keys from the presence messages, and a text and a file cross the "air" encrypted (the bridge only
    ever sees ciphertext); a message typed before the key arrived waits and then goes out encrypted; a tampered
    message is rejected
  * live voice: node 0 "talks" (a fake microphone plays a 440 Hz tone), the frames go as encrypted datagrams through
    a fake bridge to node 1, whose UdpHub hands them to its voice engine, which plays them into a fake loudspeaker
    (a file). Checks: the tone arrives (frequency and level), the frames were encrypted, and the hub still forwards
    the game's datagrams to the game port
  * a voice message is encoded, sent as a reliable chat item and decoded back to a WAV file

    python3 tools/test_voice_crypto.py
"""
import math
import os
import queue
import select
import socket
import struct
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, ROOT)
os.environ["CDP_PRESENCE_S"] = "1.5"
import cdp_crypto  # noqa: E402
import cdp_daemon as D  # noqa: E402
import cdp_voice  # noqa: E402

OK = []


def check(name, cond, info=""):
    OK.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f"   ({info})" if info else ""), flush=True)


class Bridge:
    """Reliable messages over TCP (one port per node) and datagrams over UDP (in / out port per node)."""

    def __init__(self, nodes):
        self.nodes = nodes                   # node -> dict(tcp, udp_in, udp_out)
        self.cli = {n: [] for n in nodes}
        self.srv, self.uin, self.bufs = {}, {}, {}
        self.seen_rel, self.seen_udp = [], []
        self.out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for n, p in nodes.items():
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", p["tcp"]))
            s.listen(4)
            self.srv[s] = n
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            u.bind(("127.0.0.1", p["udp_in"]))
            self.uin[u] = n
        threading.Thread(target=self.loop, daemon=True).start()

    @staticmethod
    def fr(b):
        return len(b).to_bytes(4, "big") + b

    def loop(self):
        while True:
            r, _, _ = select.select(list(self.srv) + list(self.uin) + list(self.bufs), [], [], 0.1)
            for s in r:
                if s in self.srv:
                    n = self.srv[s]
                    c, _ = s.accept()
                    self.cli[n].append(c)
                    self.bufs[c] = (n, bytearray())
                    c.sendall(self.fr(struct.pack(">BBHBI", 0x80, n, 1000, 16, 16000000)))
                elif s in self.uin:
                    n = self.uin[s]
                    d, _ = s.recvfrom(65535)
                    dest, data = d[0], d[1:]
                    self.seen_udp.append(data)
                    for m, p in self.nodes.items():
                        if m != n and dest in (m, 255):
                            self.out.sendto(bytes([n, dest]) + data, ("127.0.0.1", p["udp_out"]))
                else:
                    n, buf = self.bufs[s]
                    try:
                        ch = s.recv(1 << 20)
                    except OSError:
                        ch = b""
                    if not ch:
                        del self.bufs[s]
                        self.cli[n].remove(s)
                        continue
                    buf += ch
                    while len(buf) >= 4 and len(buf) >= 4 + int.from_bytes(buf[:4], "big"):
                        k = int.from_bytes(buf[:4], "big")
                        body = bytes(buf[4:4 + k])
                        del buf[:4 + k]
                        if body[0] != 1:
                            continue
                        ref, dest = struct.unpack(">IB", body[1:6])
                        data = body[6:]
                        self.seen_rel.append(data)
                        for m in self.nodes:
                            if m != n and dest in (m, 255):
                                for c in self.cli[m]:
                                    c.sendall(self.fr(bytes([0x81, n]) + data))
                        s.sendall(self.fr(struct.pack(">BIB", 0x82, ref, dest)))


def main():
    tmp = tempfile.mkdtemp(prefix="cdp_vc_")
    ports = {0: dict(tcp=46300, udp_in=46310, udp_out=46311, game=46312),
             1: dict(tcp=46301, udp_in=46320, udp_out=46321, game=46322)}
    br = Bridge(ports)
    # fake audio: a 440 Hz tone at real-time pace as the microphone, a file as the loudspeaker
    mic = os.path.join(tmp, "mic.py")
    open(mic, "w").write(
        "import sys, math, struct, time\n"
        "t0 = time.time(); i = 0\n"
        "while True:\n"
        "    blk = struct.pack('<400h', *(int(8000 * math.sin(2 * math.pi * 440 * (i + k) / 8000)) for k in range(400)))\n"
        "    sys.stdout.buffer.write(blk); sys.stdout.buffer.flush(); i += 400\n"
        "    time.sleep(max(0, t0 + i / 8000 - time.time()))\n")
    spk = os.path.join(tmp, "spk.py")
    open(spk, "w").write("import sys\nf = open(sys.argv[1], 'ab')\n"
                         "while True:\n    b = sys.stdin.buffer.read(1600)\n    if not b: break\n    f.write(b); f.flush()\n")
    out1 = os.path.join(tmp, "speaker1.raw")
    nodes = {}
    for n in (0, 1):
        os.makedirs(os.path.join(tmp, f"pc{n}"), exist_ok=True)
        sess = D.Session(root=os.path.join(tmp, f"pc{n}"))
        sess.login(n, [], {})
        lk = D.Link(sess, port=ports[n]["tcp"])
        lk.start()
        ident = cdp_crypto.Identity(os.path.join(tmp, f"id{n}.json"))
        core = D.ChatCore(sess, lk, n, identity=ident)
        os.environ["CDP_AUDIO_IN_CMD"] = f"{sys.executable} {mic}"
        os.environ["CDP_AUDIO_OUT_CMD"] = f"{sys.executable} {spk} {out1 if n == 1 else os.devnull}"
        holder = {}
        hub = cdp_voice.UdpHub(ports[n]["udp_out"], ports[n]["game"], ports[n]["udp_in"],
                               on_voice=lambda s, d, p, h=holder: h["v"].on_datagram(s, d, p))
        voice = cdp_voice.VoiceEngine(n, hub.send, key_for=core.key_for, encrypt=lambda c=core: c.encrypt_on,
                                      log=sess.log)
        holder["v"] = voice
        nodes[n] = dict(core=core, link=lk, hub=hub, voice=voice, sess=sess, ident=ident)

    def run(secs):
        end = time.time() + secs
        while time.time() < end:
            for n, x in nodes.items():
                while True:
                    try:
                        ev = x["link"].events.get_nowait()
                    except queue.Empty:
                        break
                    if ev[0] == "hello":
                        x["core"].on_connected()
                    elif ev[0] != "state":
                        x["core"].on_event(ev)
                x["core"].tick(time.time())
            time.sleep(0.03)

    c0, c1 = nodes[0]["core"], nodes[1]["core"]
    # a message typed at once (before node 0 has heard node 1's key) waits for the key
    run(0.3)
    c0.add_peer(1, time.time())
    c0.keys.pop(1, None)                     # (if node 1's first presence was already in, forget its key again)
    early = c0.send_text(1, "typed before the key arrived")
    st_before = early.status
    run(4.0)
    check("both nodes learned each other's public key (fingerprints match)",
          c0.keys.get(1) is not None and c1.keys.get(0) is not None and
          c0.keys[1].fp_text == nodes[1]["ident"].fp_text and c1.keys[0].fp_text == nodes[0]["ident"].fp_text,
          f"{nodes[0]['ident'].fp_text} / {nodes[1]['ident'].fp_text}")
    check("a message typed before the key waited, then went out encrypted",
          st_before == "key" and early.status == "sent" and early.extra.get("enc"), f"{st_before} -> {early.status}")
    secret = "meet at the lab at 6 - the password is swordfish"
    m = c0.send_text(1, secret)
    blob = os.urandom(30000)
    f = c1.send_bytes(0, "data.bin", blob)
    run(2.0)
    got = [x.text for x in c1.chats[0]]
    check("encrypted text arrives and is shown", secret in got and m.status == "sent", str(got[-2:]))
    check("the radio only saw ciphertext", not any(secret.encode() in d for d in br.seen_rel) and
          any(cdp_crypto.is_sealed(d) for d in br.seen_rel))
    rx_file = [x for x in c0.chats[1] if x.name == "data.bin"]
    check("an encrypted file arrives intact", rx_file and open(rx_file[0].path, "rb").read() == blob and
          rx_file[0].extra.get("enc"))
    sealed = cdp_crypto.seal(c0.keys[1], D.encode({"id": "x1", "kind": "text", "text": "hello"}))
    bad = bytearray(sealed)
    bad[-20] ^= 0x40
    n_before = len(c1.chats[0])
    c1.on_event(("recv", 0, bytes(bad), time.time()))
    check("a tampered message is rejected (and said so in the chat)", c1.cstats["failed"] == 1 and
          "could not be opened" in c1.chats[0][-1].text and len(c1.chats[0]) == n_before + 1)
    # ---- live voice 0 -> 1
    g = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    g.bind(("127.0.0.1", ports[1]["game"]))
    g.settimeout(2.0)
    why = nodes[0]["voice"].start_talk(1)
    run(2.6)
    nodes[0]["voice"].stop_talk()
    talkers_during = nodes[1]["voice"].stats["spurts_rx"]
    run(1.8)
    v0, v1 = nodes[0]["voice"].stats, nodes[1]["voice"].stats
    check("push-to-talk started", why is None, str(why))
    check("voice frames crossed the link (about 10 per second)", v0["tx_frames"] >= 20 and v1["rx_frames"] >= 18,
          f"sent {v0['tx_frames']}, received {v1['rx_frames']}, spurts {talkers_during}")
    vf = [d for d in br.seen_udp if d[:2] == cdp_voice.VMAGIC]
    check("every voice frame on the air was encrypted", vf and all(d[2] & cdp_voice.F_ENC for d in vf),
          f"{len(vf)} frames")
    raw = open(out1, "rb").read() if os.path.exists(out1) else b""
    n = len(raw) // 2
    s = struct.unpack(f"<{n}h", raw[:2 * n]) if n else ()
    loud = [x for x in s if abs(x) > 2000]
    zc = sum(1 for a, b in zip(s, s[1:]) if (a < 0) != (b < 0) and abs(a - b) > 1000)
    secs_loud = len(loud) / 8000 / 0.64
    freq = zc / 2 / max(1e-9, sum(1 for x in s if abs(x) > 300) / 8000)
    check("the loudspeaker played the 440 Hz tone", secs_loud > 1.5 and 400 < freq < 480,
          f"{len(s) / 8000:.1f} s written, ~{secs_loud:.1f} s loud, ~{freq:.0f} Hz")
    # game datagrams still reach the game through the hub
    br.out.sendto(bytes([0, 1]) + b"\x05\x00game", ("127.0.0.1", ports[1]["udp_out"]))
    try:
        d, _ = g.recvfrom(100)
    except socket.timeout:
        d = b""
    check("the hub forwards the game's datagrams to the game port", d == bytes([0, 1]) + b"\x05\x00game")
    # ---- voice message
    pcm = struct.pack("<16000h", *(int(6000 * math.sin(2 * math.pi * 300 * i / 8000)) for i in range(16000)))
    vm = c0.send_bytes(1, "voice_test.cdpv", cdp_voice.encode_message(pcm), extra={"dur": 2.0}, kind="voice")
    run(1.5)
    rv = [x for x in c1.chats[0] if x.kind == "voice"]
    ok = bool(rv and rv[0].extra.get("wav") and os.path.exists(rv[0].extra["wav"]))
    back = cdp_voice.read_wav(rv[0].extra["wav"]) if ok else b""
    check("a voice message arrives encrypted and is saved as a playable WAV",
          ok and rv[0].extra.get("enc") and len(back) == len(pcm) and vm.status == "sent",
          f"{len(back)} B of PCM, {vm.size} B sent")
    for x in nodes.values():
        x["voice"].close()
        x["hub"].close()
    print("ALL PASS" if all(OK) else "SOME CHECKS FAILED", flush=True)
    os._exit(0 if all(OK) else 1)


if __name__ == "__main__":
    main()
