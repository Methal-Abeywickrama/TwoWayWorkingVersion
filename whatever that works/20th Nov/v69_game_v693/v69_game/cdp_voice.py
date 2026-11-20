#!/usr/bin/env python3
"""
cdp_voice.py - voice for the CDP chat: push-to-talk over the radio, and voice messages. Python standard library only
(sounddevice is used if it is installed).

Live voice - click "Talk" in the chat window; it stays on until "Stop talking" is clicked
  microphone, 8 kHz mono 16 bit -> IMA ADPCM, 4 bit per sample (32 kbit/s) -> one datagram every CDP_VOICE_FRAME_MS
  (100 ms = 800 samples = 400 B + 18 B header) -> bridge datagram service (no ACK, no retransmission: a late
  voice frame is useless) -> receiver: jitter buffer (playback starts after 2 frames), a lost frame is replaced by
  the previous one, faded, then silence -> loudspeaker. About 4.2 kB/s on the air while someone talks.
  With encryption on, each frame is encrypted and authenticated (cdp_crypto.seal_frame).

Voice messages - "Voice msg" records up to 60 s, sends it as an ordinary (reliable, encrypted) chat message:
  arrives complete even over a bad link; shows up as "voice message 4.2 s  > play".

Datagram routing on this PC: the bridge sends every received datagram to ONE local port (CDP_UDP_OUT_PORT, 5201).
The daemon owns it (UdpHub): voice frames (they start with b"CV") go to the voice engine, everything else is
forwarded unchanged to CDP_GAME_UDP_PORT (5202), where the daemon tells the game to listen.

Audio in / out (CDP_AUDIO = auto | sounddevice | pulse | alsa | off):
  sounddevice (pip install sounddevice) | pulse: parec / pacat (PipeWire and PulseAudio) | alsa: arecord / aplay
  or any commands: CDP_AUDIO_IN_CMD="..." (writes raw s16le 8 kHz mono to stdout), CDP_AUDIO_OUT_CMD="..." (reads it)

Voice frame (datagram payload):
  b"CV" | flags u8 (bit0 encrypted) | sid u32 (talk spurt) | seq u16 | body
  body = codec u8 (1 = IMA ADPCM 8 kHz) | n u16 (samples) | predictor i16 | index u8 | n/2 bytes
  encrypted: body -> seal_frame(body) = ciphertext + 8 B tag, nonce = sender node | sid | seq

    python3 cdp_voice.py            # self-test (codec, framing, jitter buffer)
"""
import os
import queue
import random
import select
import shlex
import shutil
import socket
import struct
import subprocess
import threading
import time
import wave

RATE = 8000
FRAME_MS = int(os.environ.get("CDP_VOICE_FRAME_MS", "100"))
FRAME_N = RATE * FRAME_MS // 1000
VMAGIC = b"CV"
F_ENC = 0x01
CODEC_ADPCM = 1
HDR = struct.Struct(">2sBIH")                 # magic, flags, sid, seq
BODY = struct.Struct(">BHhB")                 # codec, n, predictor, index
PREBUF = 2                                    # frames buffered before playback starts
MAXBUF = 8                                    # more frames waiting than this (0.8 s): skip ahead to PREBUF + 1
                                              # (keeps the delay bounded on a long talk: the two PCs' clocks drift)
END_S = 1.0                                   # talk spurt over after this long without frames
MAX_MSG_S = 60.0

# ------------------------------------------------------------------ IMA ADPCM (4 bit/sample)
_STEPS = [7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97,
          107, 118, 130, 143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796,
          876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428,
          4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350,
          22385, 24623, 27086, 29794, 32767]
_IDX = [-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8]


def adpcm_encode(pcm: bytes, pred=0, index=0):
    """s16le PCM -> (bytes, (pred, index) at the start). Two samples per byte, low nibble first."""
    n = len(pcm) // 2
    samples = struct.unpack(f"<{n}h", pcm[:2 * n])
    start = (pred, index)
    out = bytearray((n + 1) // 2)
    steps, idxt = _STEPS, _IDX
    for i, s in enumerate(samples):
        step = steps[index]
        diff = s - pred
        code = 0
        if diff < 0:
            code = 8
            diff = -diff
        delta = step >> 3
        if diff >= step:
            code |= 4
            diff -= step
            delta += step
        step >>= 1
        if diff >= step:
            code |= 2
            diff -= step
            delta += step
        step >>= 1
        if diff >= step:
            code |= 1
            delta += step
        pred = pred - delta if code & 8 else pred + delta
        pred = -32768 if pred < -32768 else 32767 if pred > 32767 else pred
        index += idxt[code]
        index = 0 if index < 0 else 88 if index > 88 else index
        if i & 1:
            out[i >> 1] |= code << 4
        else:
            out[i >> 1] = code
    return bytes(out), start, (pred, index)


def adpcm_decode(data: bytes, n: int, pred=0, index=0) -> bytes:
    out = [0] * n
    steps, idxt = _STEPS, _IDX
    for i in range(n):
        b = data[i >> 1] if (i >> 1) < len(data) else 0
        code = (b >> 4) if i & 1 else (b & 15)
        step = steps[index]
        delta = step >> 3
        if code & 4:
            delta += step
        if code & 2:
            delta += step >> 1
        if code & 1:
            delta += step >> 2
        pred = pred - delta if code & 8 else pred + delta
        pred = -32768 if pred < -32768 else 32767 if pred > 32767 else pred
        index += idxt[code]
        index = 0 if index < 0 else 88 if index > 88 else index
        out[i] = pred
    return struct.pack(f"<{n}h", *out)


def encode_body(pcm: bytes, state=(0, 0)):
    """One frame -> (body, coder state after it). The body carries the state at its start, so each frame can be
    decoded on its own (a lost frame doesn't break the next one), while the coder runs on without restarting."""
    n = len(pcm) // 2
    data, (pred, idx), end = adpcm_encode(pcm, *state)
    return BODY.pack(CODEC_ADPCM, n, pred, idx) + data, end


def decode_body(body: bytes) -> bytes:
    codec, n, pred, idx = BODY.unpack_from(body)
    if codec != CODEC_ADPCM or n > 8 * RATE:
        raise ValueError("unknown voice codec")
    return adpcm_decode(body[BODY.size:BODY.size + (n + 1) // 2], n, pred, idx)


def encode_message(pcm: bytes) -> bytes:
    """A voice message: frames of FRAME_N samples back to back (each with its own coder state)."""
    out = bytearray(b"CDPVM1")
    step = FRAME_N * 2
    state = (0, 0)
    for i in range(0, len(pcm), step):
        body, state = encode_body(pcm[i:i + step], state)
        out += body
    return bytes(out)


def decode_message(blob: bytes) -> bytes:
    if not blob.startswith(b"CDPVM1"):
        raise ValueError("not a voice message")
    o, pcm = 6, bytearray()
    while o + BODY.size <= len(blob):
        _, n, _, _ = BODY.unpack_from(blob, o)
        size = BODY.size + (n + 1) // 2
        pcm += decode_body(blob[o:o + size])
        o += size
    return bytes(pcm)


def write_wav(path, pcm: bytes):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)


def read_wav(path) -> bytes:
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes())


def level(pcm: bytes) -> float:
    """0..1 loudness of a chunk (for the talk meter)."""
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    s = struct.unpack(f"<{n}h", pcm[:2 * n])
    return min(1.0, (sum(x * x for x in s[::4]) / max(1, len(s[::4]))) ** 0.5 / 8000.0)


def mix(chunks):
    if len(chunks) == 1:
        return chunks[0]
    n = min(len(c) for c in chunks) // 2
    acc = [0] * n
    for c in chunks:
        for i, v in enumerate(struct.unpack(f"<{n}h", c[:2 * n])):
            acc[i] += v
    return struct.pack(f"<{n}h", *(max(-32768, min(32767, v)) for v in acc))


def fade(pcm: bytes, k: float) -> bytes:
    n = len(pcm) // 2
    return struct.pack(f"<{n}h", *(int(v * k) for v in struct.unpack(f"<{n}h", pcm[:2 * n])))


SILENCE = b"\0\0" * FRAME_N


# ------------------------------------------------------------------ audio in / out
class _Proc:
    def __init__(self, argv, read):
        self.p = subprocess.Popen(argv, stdin=None if read else subprocess.PIPE,
                                  stdout=subprocess.PIPE if read else subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, start_new_session=True)

    def alive(self):
        return self.p.poll() is None

    def close(self):
        try:
            if self.p.stdin:
                self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.terminate()
            self.p.wait(1.0)
        except Exception:
            try:
                self.p.kill()
            except Exception:
                pass


class Capture:
    """Microphone -> on_pcm(bytes) from a thread, until stop()."""

    def __init__(self, backend, on_pcm):
        self.b, self.on_pcm = backend, on_pcm
        self._stop = threading.Event()
        self.error = None
        self.sd_stream = None
        if backend.kind == "sounddevice":
            import sounddevice as sd
            self.sd_stream = sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME_N // 4,
                                               callback=lambda data, frames, t, st: self.on_pcm(bytes(data)))
            self.sd_stream.start()
        else:
            self.proc = _Proc(backend.in_cmd, read=True)
            threading.Thread(target=self._run, name="voice-capture", daemon=True).start()

    def _run(self):
        fd = self.proc.p.stdout
        while not self._stop.is_set():
            r, _, _ = select.select([fd], [], [], 0.2)
            if not r:
                if not self.proc.alive():
                    self.error = "microphone command stopped"
                    return
                continue
            chunk = os.read(fd.fileno(), 4096)
            if not chunk:
                self.error = "microphone command closed"
                return
            self.on_pcm(chunk)

    def stop(self):
        self._stop.set()
        if self.sd_stream is not None:
            try:
                self.sd_stream.stop()
                self.sd_stream.close()
            except Exception:
                pass
        else:
            self.proc.close()


class Player:
    """write(pcm) -> loudspeaker (blocking only for the pipe / device buffer)."""

    def __init__(self, backend):
        self.b = backend
        self.sd_stream = None
        self.proc = None
        self._open()

    def _open(self):
        if self.b.kind == "sounddevice":
            import sounddevice as sd
            self.sd_stream = sd.RawOutputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME_N // 2)
            self.sd_stream.start()
        else:
            self.proc = _Proc(self.b.out_cmd, read=False)

    def write(self, pcm: bytes):
        try:
            if self.sd_stream is not None:
                self.sd_stream.write(pcm)
            else:
                if not self.proc.alive():
                    self.proc = _Proc(self.b.out_cmd, read=False)
                self.proc.p.stdin.write(pcm)
                self.proc.p.stdin.flush()
        except Exception:
            self.proc = None if self.sd_stream is None and self.proc is None else self.proc
            try:
                if self.sd_stream is None:
                    self.proc = _Proc(self.b.out_cmd, read=False)
            except Exception:
                pass

    def close(self):
        if self.sd_stream is not None:
            try:
                self.sd_stream.stop()
                self.sd_stream.close()
            except Exception:
                pass
        elif self.proc is not None:
            self.proc.close()


class AudioBackend:
    """Picks how to reach the microphone and the loudspeaker."""

    def __init__(self, choice=None):
        choice = (choice or os.environ.get("CDP_AUDIO", "auto")).strip().lower()
        self.kind, self.in_cmd, self.out_cmd, self.why = None, None, None, ""
        icmd, ocmd = os.environ.get("CDP_AUDIO_IN_CMD"), os.environ.get("CDP_AUDIO_OUT_CMD")
        if choice == "off":
            self.why = "voice is off (CDP_AUDIO=off)"
            return
        if icmd and ocmd:
            self.kind, self.in_cmd, self.out_cmd = "cmd", shlex.split(icmd), shlex.split(ocmd)
            return
        if choice in ("auto", "sounddevice"):
            try:
                import sounddevice as sd
                sd.check_input_settings(samplerate=RATE, channels=1, dtype="int16")
                sd.check_output_settings(samplerate=RATE, channels=1, dtype="int16")
                self.kind = "sounddevice"
                return
            except Exception as e:
                if choice == "sounddevice":
                    self.why = f"sounddevice: {e}"
                    return
        if choice in ("auto", "pulse") and shutil.which("parec") and shutil.which("pacat"):
            self.kind = "pulse"
            self.in_cmd = ["parec", "--raw", "--format=s16le", f"--rate={RATE}", "--channels=1", "--latency-msec=40"]
            self.out_cmd = ["pacat", "--raw", "--playback", "--format=s16le", f"--rate={RATE}", "--channels=1",
                            "--latency-msec=80"]
            return
        if choice in ("auto", "alsa") and shutil.which("arecord") and shutil.which("aplay"):
            self.kind = "alsa"
            self.in_cmd = ["arecord", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(RATE), "-c", "1"]
            self.out_cmd = ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(RATE), "-c", "1"]
            return
        self.why = ("no audio tool found: install sounddevice (pip install sounddevice), or pulseaudio-utils "
                    "(parec/pacat), or alsa-utils (arecord/aplay)")

    @property
    def ok(self):
        return self.kind is not None

    def describe(self):
        return {"sounddevice": "sounddevice", "pulse": "PipeWire/PulseAudio (parec, pacat)",
                "alsa": "ALSA (arecord, aplay)", "cmd": "custom commands"}.get(self.kind, self.why)


# ------------------------------------------------------------------ datagram hub
class UdpHub:
    """Owns the bridge's datagram output port. Voice frames -> on_voice(src, dst, payload); everything else is
    forwarded unchanged (src | dst | data) to the game's port. send(dest, payload) hands a datagram to the radio."""

    def __init__(self, bind_port, game_port, udp_port, on_voice, host="127.0.0.1"):
        self.on_voice = on_voice
        self.game = (host, int(game_port))
        self.radio = (host, int(udp_port))
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.ok, self.error = True, ""
        self.stats = {"voice_rx": 0, "game_fwd": 0, "other": 0, "tx": 0}
        try:
            self.rx.bind((host, int(bind_port)))
        except OSError as e:
            self.ok, self.error = False, f"udp {host}:{bind_port} busy ({e}) - live voice off"
            self.rx.close()
            return
        self._stop = threading.Event()
        threading.Thread(target=self._run, name="udp-hub", daemon=True).start()

    def _run(self):
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([self.rx], [], [], 0.5)
                if not r:
                    continue
                d, _ = self.rx.recvfrom(65535)
            except OSError:
                return
            if len(d) < 3:
                continue
            if d[2:4] == VMAGIC:
                self.stats["voice_rx"] += 1
                try:
                    self.on_voice(d[0], d[1], d[2:])
                except Exception:
                    pass
            else:
                self.stats["game_fwd"] += 1
                try:
                    self.tx.sendto(d, self.game)
                except OSError:
                    pass

    def send(self, dest, payload):
        try:
            self.tx.sendto(bytes([dest & 0xFF]) + payload, self.radio)
            self.stats["tx"] += 1
        except OSError:
            pass

    def close(self):
        if self.ok:
            self._stop.set()
            try:
                self.rx.close()
            except OSError:
                pass


# ------------------------------------------------------------------ the voice engine
class _Talker:
    def __init__(self, src, sid, now):
        self.src, self.sid = src, sid
        self.frames = {}
        self.next = None
        self.started = False
        self.t_first = self.last_rx = now
        self.last_pcm = None
        self.plc = 0
        self.level = 0.0
        self.encrypted = False


class VoiceEngine:
    """Push-to-talk sender + jitter-buffered receiver. send_datagram(dest, payload) goes to the radio;
    key_for(node) -> cdp_crypto.PeerKey or None; encrypt() -> bool (encryption switched on)."""

    def __init__(self, my_node, send_datagram, backend=None, key_for=None, encrypt=None, log=print):
        self.me = my_node
        self.send_datagram = send_datagram
        self.backend = backend or AudioBackend()
        self.key_for = key_for or (lambda n: None)
        self.encrypt = encrypt or (lambda: False)
        self.log = log
        self.lock = threading.Lock()
        self.talkers = {}
        self.player = None
        self.capture = None
        self.dest = None
        self.sid = 0
        self.seq = 0
        self.enc_state = (0, 0)
        self.buf = bytearray()
        self.rec = None                       # bytearray while recording a voice message
        self.rec_cap = None
        self.mic_level = 0.0
        self.stats = {"tx_frames": 0, "tx_bytes": 0, "rx_frames": 0, "rx_bytes": 0, "lost": 0, "late": 0, "plc": 0,
                      "auth_fail": 0, "no_key": 0, "spurts_rx": 0, "skipped": 0}
        self.talk_t0 = None
        self._stop = threading.Event()
        self._play_q = queue.Queue()          # whole clips (voice messages) to play
        threading.Thread(target=self._playout, name="voice-playout", daemon=True).start()

    # ---- sending (push to talk)
    def talking(self):
        return self.capture is not None

    def start_talk(self, dest):
        """Returns None if started, else the reason."""
        if not self.backend.ok:
            return self.backend.why
        if self.capture is not None:
            return None
        if self.encrypt() and self.key_for(dest) is None:
            return f"no encryption key from node {dest} yet (or switch encryption off)"
        self.dest, self.sid, self.seq = dest, random.getrandbits(32), 0
        self.talk_t0 = time.monotonic()
        self.enc_state = (0, 0)
        self.buf = bytearray()
        try:
            self.capture = Capture(self.backend, self._on_mic)
        except Exception as e:
            self.capture = None
            return f"microphone: {e}"
        return None

    def talk_seconds(self):
        return time.monotonic() - self.talk_t0 if self.capture is not None and self.talk_t0 else 0.0

    def capture_error(self):
        return self.capture.error if self.capture is not None else None

    def stop_talk(self):
        self.talk_t0 = None
        cap, self.capture = self.capture, None
        if cap is not None:
            cap.stop()
            with self.lock:
                if len(self.buf) >= FRAME_N:            # the last part of a frame: pad with silence
                    self._send_frame(bytes(self.buf[:FRAME_N * 2]).ljust(FRAME_N * 2, b"\0"))
                self.buf = bytearray()
        self.mic_level = 0.0

    def _on_mic(self, pcm):
        with self.lock:
            self.buf += pcm
            while len(self.buf) >= FRAME_N * 2:
                frame = bytes(self.buf[:FRAME_N * 2])
                del self.buf[:FRAME_N * 2]
                self.mic_level = level(frame)
                self._send_frame(frame)

    def frame_payload(self, frame_pcm, dest, sid, seq):
        body, self.enc_state = encode_body(frame_pcm, self.enc_state)
        flags = 0
        pk = self.key_for(dest) if self.encrypt() else None
        if pk is not None:
            flags |= F_ENC
        hdr = HDR.pack(VMAGIC, flags, sid & 0xFFFFFFFF, seq & 0xFFFF)
        if pk is not None:
            from cdp_crypto import seal_frame
            body = seal_frame(pk, bytes([self.me & 0xFF]) + hdr[3:9], hdr, body)
        return hdr + body

    def _send_frame(self, frame_pcm):
        payload = self.frame_payload(frame_pcm, self.dest, self.sid, self.seq)
        self.seq = (self.seq + 1) & 0xFFFF
        self.send_datagram(self.dest, payload)
        self.stats["tx_frames"] += 1
        self.stats["tx_bytes"] += len(payload)

    # ---- voice messages
    def start_record(self):
        if not self.backend.ok:
            return self.backend.why
        if self.rec_cap is not None:
            return None
        self.rec = bytearray()

        def on(pcm):
            if self.rec is not None and len(self.rec) < MAX_MSG_S * RATE * 2:
                self.rec += pcm
                self.mic_level = level(pcm[-FRAME_N:])
        try:
            self.rec_cap = Capture(self.backend, on)
        except Exception as e:
            self.rec_cap, self.rec = None, None
            return f"microphone: {e}"
        return None

    def stop_record(self):
        """-> recorded PCM (or b"")."""
        cap, self.rec_cap = self.rec_cap, None
        if cap is not None:
            cap.stop()
        pcm, self.rec = bytes(self.rec or b""), None
        self.mic_level = 0.0
        return pcm

    def recording_s(self):
        return len(self.rec) / (2 * RATE) if self.rec is not None else 0.0

    def play_clip(self, pcm: bytes):
        if self.backend.ok:
            self._play_q.put(pcm)

    # ---- receiving
    def on_datagram(self, src, dst, payload):
        if len(payload) < HDR.size + BODY.size or src == self.me:
            return
        magic, flags, sid, seq = HDR.unpack_from(payload)
        body = payload[HDR.size:]
        if flags & F_ENC:
            pk = self.key_for(src)
            if pk is None:
                self.stats["no_key"] += 1
                return
            try:
                from cdp_crypto import open_frame
                body = open_frame(pk, bytes([src & 0xFF]) + payload[3:9], payload[:HDR.size], body)
            except ValueError:
                self.stats["auth_fail"] += 1
                return
        try:
            pcm = decode_body(body)
        except Exception:
            return
        now = time.monotonic()
        self.stats["rx_frames"] += 1
        self.stats["rx_bytes"] += len(payload)
        with self.lock:
            t = self.talkers.get(src)
            if t is None or t.sid != sid:
                t = self.talkers[src] = _Talker(src, sid, now)
                t.next = seq
                self.stats["spurts_rx"] += 1
            t.encrypted = bool(flags & F_ENC)
            if t.started and ((seq - t.next) & 0xFFFF) > 0x8000:
                self.stats["late"] += 1                 # behind the playout point: too late
                return
            t.frames[seq] = pcm
            t.last_rx = now

    def active_talkers(self):
        now = time.monotonic()
        with self.lock:
            return [(t.src, t.level, t.encrypted) for t in self.talkers.values() if now - t.last_rx < END_S]

    def _playout(self):
        frame_s = FRAME_N / RATE
        t_next = time.monotonic()
        idle_since = None
        while not self._stop.is_set():
            now = time.monotonic()
            if now < t_next:
                time.sleep(min(0.02, t_next - now))
                continue
            t_next = max(t_next + frame_s, now - frame_s)
            out = []
            with self.lock:
                for src in list(self.talkers):
                    t = self.talkers[src]
                    if not t.started:
                        if len(t.frames) >= PREBUF or (t.frames and now - t.t_first > 0.3):
                            t.started = True
                        else:
                            if now - t.last_rx > END_S:
                                del self.talkers[src]
                            continue
                    if len(t.frames) > MAXBUF:                        # too much buffered: catch up
                        newest = max(t.frames, key=lambda q: (q - t.next) & 0xFFFF)
                        target = (newest - PREBUF) & 0xFFFF
                        while t.next != target:
                            if t.frames.pop(t.next, None) is not None:
                                self.stats["skipped"] += 1
                            t.next = (t.next + 1) & 0xFFFF
                    pcm = t.frames.pop(t.next, None)
                    if pcm is None:
                        if not t.frames and now - t.last_rx > END_S:
                            del self.talkers[src]                       # talk spurt over
                            continue
                        if t.frames:
                            self.stats["lost"] += 1
                        t.plc += 1
                        self.stats["plc"] += 1
                        pcm = fade(t.last_pcm, 0.5) if (t.last_pcm and t.plc <= 2) else SILENCE
                    else:
                        t.plc = 0
                        t.last_pcm = pcm
                    t.level = level(pcm)
                    t.next = (t.next + 1) & 0xFFFF
                    out.append(pcm)
            if not out:
                try:
                    clip = self._play_q.get_nowait()
                except queue.Empty:
                    clip = None
                if clip:
                    for i in range(0, len(clip), FRAME_N * 2):              # a voice message, paced in real time
                        out = [clip[i:i + FRAME_N * 2].ljust(FRAME_N * 2, b"\0")]
                        self._write(out[0])
                        time.sleep(frame_s * 0.9)
                    t_next = time.monotonic()
                    continue
            if out:
                idle_since = None
                self._write(mix(out))
            elif idle_since is None:
                idle_since = now
            elif now - idle_since > 5.0 and self.player is not None:
                self.player.close()                                       # free the sound device when quiet
                self.player = None

    def _write(self, pcm):
        if not self.backend.ok:
            return
        if self.player is None:
            try:
                self.player = Player(self.backend)
            except Exception as e:
                self.log(f"voice: loudspeaker: {e}")
                return
        self.player.write(pcm)

    def close(self):
        self._stop.set()
        self.stop_talk()
        self.stop_record()
        if self.player is not None:
            self.player.close()


def _selftest():
    import math
    pcm = struct.pack(f"<{RATE}h", *(int(9000 * math.sin(2 * math.pi * 440 * i / RATE)) for i in range(RATE)))
    t = time.time()
    blob = encode_message(pcm)
    back = decode_message(blob)
    dt = time.time() - t
    a = struct.unpack(f"<{RATE}h", pcm)
    b = struct.unpack(f"<{RATE}h", back)
    err = (sum((x - y) ** 2 for x, y in zip(a, b)) / RATE) ** 0.5
    snr = 20 * math.log10((sum(x * x for x in a) / RATE) ** 0.5 / max(1e-9, err))
    print(f"ADPCM: 1 s of audio {len(pcm)} B -> {len(blob)} B, encode+decode {dt * 1000:.0f} ms, SNR {snr:.1f} dB")
    assert snr > 20 and len(blob) < len(pcm) / 3.5
    sent = []
    eng = VoiceEngine(0, lambda d, p: sent.append(p), backend=AudioBackend("off"))
    eng.dest, eng.sid = 1, 1234
    eng._on_mic(pcm)
    print(f"frames: {len(sent)} x {len(sent[0])} B for 1 s -> {sum(map(len, sent)) / 1000:.1f} kB/s")
    rx = VoiceEngine(1, lambda d, p: None, backend=AudioBackend("off"))
    for i, p in enumerate(sent):
        if i != 4:
            rx.on_datagram(0, 1, p)
    assert rx.stats["rx_frames"] == len(sent) - 1
    print("voice self-test OK")


if __name__ == "__main__":
    _selftest()
