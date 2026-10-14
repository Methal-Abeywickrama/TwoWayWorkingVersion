# Radio Arena

A 2-player 3D arena shooter whose only network traffic is a 31-byte state packet. Normally you start it from the launcher (`python3 cdp_sock.py launch` in the folder above).

## Controls

| Key | Action |
|---|---|
| W A S D | move |
| Mouse | look (full 360°) |
| Arrow keys | turn / look up-down (backup for the mouse) |
| Left mouse | shoot (hold = automatic) |
| R | reload (30 rounds, 1.6 s) |
| Space / Shift | jump / sprint |
| Esc | free the mouse (click to recapture) |
| F10 | quit |

Damage: body hit 20, head hit 50, out of 100 HP. You respawn after 3 s. Your kills are the opponent's deaths.

## Run modes

```bash
python3 fps_game.py --peer 5            # CDP v6.6 radio (UDP block), this node = $CDP_MY_ID or --node N
python3 fps_game.py --bot               # offline vs a bot;  --loss 0.3 simulates packet loss
python3 fps_game.py --id 0              # plain UDP to your own flowgraph: sends :52001, listens :52002
```

Other options:

| Option | Meaning |
|---|---|
| `--rate N` | packets per second (default 5 on the radio, 10 otherwise) |
| `--sens` | mouse sensitivity |
| `--fov` | field of view |
| `--size 1280x720` | window size |
| `--fullscreen` | full screen |
| `--msaa 4` | anti-aliasing |
| `--x11` | use XWayland if mouse turning stops at the screen edge |

Radio settings come from the same environment variables as the flowgraph and `cdp_sock.py`: `CDP_HOST`, `CDP_UDP_PORT` (5200), `CDP_UDP_OUT_PORT` (5201), `CDP_UDP_BIND`.

## Payload (31 bytes, little-endian, no marker or CRC)

| Offset | Size | Field | Meaning |
|---|---|---|---|
| 0 | 1 | pid | player 0/1 (lower node ID = 0) |
| 1 | 2 | seq | +1 per packet (loss stats, old packets dropped) |
| 3 | 6 | x, y, z | i16 each, centimetres |
| 9 | 2 | yaw | u16, 0..65535 = 0..360° |
| 11 | 2 | pitch | i16, centi-degrees |
| 13 | 1 | hp | 0..100 |
| 14 | 1 | ammo | rounds in magazine |
| 15 | 1 | flags | bit0 fired, bit1 reloading, bit2 dead |
| 16 | 1 | deaths | |
| 17 | 1 | aux | spare byte |
| 18 | 1 | n_events | 0..3 |
| 19 | 12 | 3 × hit event | eid u16, target u8, damage u8 |

- **Python:** `payload.py` has `build_payload(state) -> bytes` and `parse_payload(bytes)`.
- **C:** `c/payload.c` has `gp_build()` and `gp_parse()`, and produces byte-identical output.

To test the C version:
```bash
gcc -O2 -Wall c/test_payload.c c/payload.c -o test_payload -lm && ./test_payload
python3 payload.py                      # Python self-test, prints the same hex
```

On the radio, the UDP block puts `C5 01` in front of the payload, and the framer adds the addressing, CRC32 and FEC. The payload itself stays pure game data.
