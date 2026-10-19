#!/usr/bin/env python3
"""
cdp_crypto.py - basic end-to-end encryption for the CDP chat (text, files, voice). Python standard library only.

Keys: modular arithmetic over a prime (Diffie-Hellman)
  p = the 2048-bit prime of RFC 3526 group 14 (a "safe" prime: (p - 1) / 2 is prime too), g = 2
  private key  x   = a random 256-bit number, never leaves this PC (~/.config/cdp_link/identity.json, mode 600)
  public key   y   = g^x mod p, sent to everyone in the daemon's presence broadcast
  shared key       = y_other^x mod p  =  g^(x_me * x_other) mod p   - the same number on both PCs,
                     while somebody who only heard y_me and y_other can't compute it (discrete logarithm)

Messages: encrypt-then-MAC with keys derived from the shared number
  K_enc, K_mac = SHA-256("cdp-enc" | shared), SHA-256("cdp-mac" | shared)   (and the two node numbers)
  keystream    = SHA-256(K_enc | nonce | counter) for counter = 0, 1, 2, ...   (XOR with the message)
  tag          = HMAC-SHA256(K_mac, header | nonce | ciphertext), first 16 bytes (8 for voice frames)
  A changed or forged message fails the tag check and is dropped; a new random nonce per message means the same
  text never encrypts the same way twice.

Envelope of an encrypted chat message (inside the reliable message, instead of the plain chat payload):
  b"CDPE1\\0" | from_fp 4 B | to_fp 4 B | nonce 12 B | ciphertext | tag 16 B
  from_fp / to_fp = the first 4 bytes of the SHA-256 fingerprints of the sender's and the receiver's public key,
  so a message encrypted for an older key (the other PC restarted with a new identity) is recognised.

Fingerprints: the chat shows each node's fingerprint (e.g. 3F2A-91C0-77B1-0E5D). Compare them by phone or in
person once: if they match, nobody sits in the middle swapping public keys (the one thing DH alone can't stop).

    python3 cdp_crypto.py          # self-test
"""
import hashlib
import hmac
import json
import os
import secrets
import stat
from pathlib import Path

# RFC 3526, 2048-bit MODP group (group 14)
P = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AACAA68FFFFFFFFFFFFFFFF", 16)
G = 2
KEY_BYTES = 256
ENV_MAGIC = b"CDPE1\0"
TAG_LEN = 16
NONCE_LEN = 12


def fingerprint(pub: int) -> bytes:
    return hashlib.sha256(pub.to_bytes(KEY_BYTES, "big")).digest()


def fp_text(pub: int) -> str:
    h = fingerprint(pub).hex().upper()
    return "-".join(h[i:i + 4] for i in range(0, 16, 4))


def valid_public(y: int) -> bool:
    """Reject the weak values 0, 1, p-1 (and anything outside 2..p-2) and keys outside the prime-order subgroup."""
    return 2 <= y <= P - 2 and pow(y, (P - 1) // 2, P) == 1


class Identity:
    """This PC's key pair (kept in a file so the fingerprint stays the same between runs)."""

    def __init__(self, path=None):
        self.path = Path(path or os.environ.get("CDP_IDENTITY") or
                         os.path.expanduser("~/.config/cdp_link/identity.json"))
        self.x = None
        try:
            d = json.loads(self.path.read_text())
            self.x = int(d["x"], 16)
        except Exception:
            pass
        if not self.x or not 2 <= self.x < P - 1:
            self.x = secrets.randbits(256) | (1 << 255)       # g = 2 lies in the prime-order subgroup of this p
            self._save()
        self.y = pow(G, self.x, P)
        self.fp = fingerprint(self.y)
        self.fp_text = fp_text(self.y)

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"x": format(self.x, "x"), "note": "CDP link private key - keep it secret"}))
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
            tmp.replace(self.path)
        except OSError:
            pass                                   # a key for this run only

    def public_hex(self) -> str:
        return format(self.y, "x")


class PeerKey:
    """Shared keys with one other node, from its public key."""

    def __init__(self, me: Identity, my_node: int, their_pub: int, their_node: int):
        if not valid_public(their_pub):
            raise ValueError("invalid public key")
        self.pub = their_pub
        self.fp = fingerprint(their_pub)
        self.fp_text = fp_text(their_pub)
        s = pow(their_pub, me.x, P).to_bytes(KEY_BYTES, "big")
        ids = bytes(sorted((my_node & 0xFF, their_node & 0xFF)))
        self.k_enc = hashlib.sha256(b"cdp-enc" + ids + s).digest()
        self.k_mac = hashlib.sha256(b"cdp-mac" + ids + s).digest()
        self.my_fp4 = me.fp[:4]
        self.their_fp4 = self.fp[:4]


def keystream(k_enc: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    ctr = 0
    while len(out) < n:
        out += hashlib.sha256(k_enc + nonce + ctr.to_bytes(8, "big")).digest()
        ctr += 1
    return bytes(out[:n])


def xor(data: bytes, ks: bytes) -> bytes:
    if not data:
        return b""
    return (int.from_bytes(data, "big") ^ int.from_bytes(ks[:len(data)], "big")).to_bytes(len(data), "big")


def _tag(k_mac, *parts, n=TAG_LEN):
    return hmac.new(k_mac, b"".join(parts), hashlib.sha256).digest()[:n]


def seal(pk: PeerKey, plaintext: bytes) -> bytes:
    """Chat message -> encrypted envelope."""
    nonce = secrets.token_bytes(NONCE_LEN)
    head = ENV_MAGIC + pk.my_fp4 + pk.their_fp4
    ct = xor(plaintext, keystream(pk.k_enc, nonce, len(plaintext)))
    return head + nonce + ct + _tag(pk.k_mac, head, nonce, ct)


def is_sealed(raw: bytes) -> bool:
    return raw.startswith(ENV_MAGIC) and len(raw) >= len(ENV_MAGIC) + 8 + NONCE_LEN + TAG_LEN


def envelope_fps(raw: bytes):
    """(sender fp4, receiver fp4) of an envelope."""
    o = len(ENV_MAGIC)
    return raw[o:o + 4], raw[o + 4:o + 8]


def open_sealed(pk: PeerKey, raw: bytes) -> bytes:
    """Encrypted envelope -> chat message. Raises ValueError if it was changed or is not for this key."""
    o = len(ENV_MAGIC)
    head, nonce = raw[:o + 8], raw[o + 8:o + 8 + NONCE_LEN]
    ct, tag = raw[o + 8 + NONCE_LEN:-TAG_LEN], raw[-TAG_LEN:]
    if head[o:o + 4] != pk.their_fp4 or head[o + 4:o + 8] != pk.my_fp4:
        raise ValueError("encrypted for another key")
    if not hmac.compare_digest(tag, _tag(pk.k_mac, head, nonce, ct)):
        raise ValueError("authentication failed (changed in transit or wrong key)")
    return xor(ct, keystream(pk.k_enc, nonce, len(ct)))


def seal_frame(pk: PeerKey, nonce: bytes, aad: bytes, payload: bytes, tag_len=8) -> bytes:
    """Voice frame: the caller gives a unique nonce (talk-spurt id + sequence number); short tag."""
    n = nonce.ljust(NONCE_LEN, b"\0")[:NONCE_LEN]
    ct = xor(payload, keystream(pk.k_enc, b"V" + n[1:], len(payload)))
    return ct + _tag(pk.k_mac, b"voice", aad, n, ct, n=tag_len)


def open_frame(pk: PeerKey, nonce: bytes, aad: bytes, data: bytes, tag_len=8) -> bytes:
    n = nonce.ljust(NONCE_LEN, b"\0")[:NONCE_LEN]
    ct, tag = data[:-tag_len], data[-tag_len:]
    if not hmac.compare_digest(tag, _tag(pk.k_mac, b"voice", aad, n, ct, n=tag_len)):
        raise ValueError("voice frame authentication failed")
    return xor(ct, keystream(pk.k_enc, b"V" + n[1:], len(ct)))


def _selftest():
    import tempfile
    import time
    d = tempfile.mkdtemp()
    a, b = Identity(os.path.join(d, "a.json")), Identity(os.path.join(d, "b.json"))
    a2 = Identity(os.path.join(d, "a.json"))
    assert a2.y == a.y, "identity not reloaded"
    t = time.time()
    ka, kb = PeerKey(a, 0, b.y, 1), PeerKey(b, 1, a.y, 0)
    print(f"key agreement: {(time.time() - t) * 500:.1f} ms per side, fingerprints {a.fp_text} / {b.fp_text}")
    assert ka.k_enc == kb.k_enc and ka.k_mac == kb.k_mac
    msg = b"CDPC2\0" + b"hello over the radio" * 5
    env = seal(ka, msg)
    assert env != seal(ka, msg), "nonce not random"
    assert open_sealed(kb, env) == msg
    assert msg[6:20] not in env
    bad = bytearray(env)
    bad[30] ^= 1
    try:
        open_sealed(kb, bytes(bad))
        raise AssertionError("tampering not detected")
    except ValueError:
        pass
    c = Identity(os.path.join(d, "c.json"))
    kc = PeerKey(c, 2, a.y, 0)
    try:
        open_sealed(kc, env)
        raise AssertionError("wrong key accepted")
    except ValueError:
        pass
    big = secrets.token_bytes(2_000_000)
    t = time.time()
    assert open_sealed(kb, seal(ka, big)) == big
    print(f"2 MB file: encrypt + decrypt {time.time() - t:.2f} s")
    fr = seal_frame(ka, b"\x01\x02\x00\x07", b"hdr", b"voice" * 80)
    assert open_frame(kb, b"\x01\x02\x00\x07", b"hdr", fr) == b"voice" * 80
    for y in (0, 1, P - 1, P):
        assert not valid_public(y)
    print("crypto self-test OK")


if __name__ == "__main__":
    _selftest()
