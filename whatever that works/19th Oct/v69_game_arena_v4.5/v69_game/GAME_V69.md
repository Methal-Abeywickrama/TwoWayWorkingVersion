# Radio Arena v4 on the v6.9.1 cdp_link

Commands are in `V6.9_GAME_COMMANDS.md`. The game itself (host-authoritative, rewind, prediction, migration) is described in `game/GAME.md`. This file covers only what differs on v6.9.

## What is unchanged

Everything up to and including the ARQ, with all its settings:
- the flowgraph `v6.9_cdp_link.grc` (PHY, CSMA/CA MAC with p 0.2 / CWmax 63, SR-ARQ, datagram service, socket bridge, session logger)
- `cdp_sock.py`, `cdp_logsum.py`
- `LOGS.md` and `README.md`

They are byte-identical to the files you sent, with the changes you asked for:
- **TX attenuation while sending starts at 0 dB.** That is the default of `CDP_TX_ATT` in the `.grc` (was 20).
- **C++ kernels for the four per-sample Python blocks** (MAC carrier sense and quiet/contention steps, correlator, deframer,
  noise floor): `cdp_fast.cpp` + `cdp_fast.py`.
  - The same results, tested event for event.
  - About 2.2 to 2.7 times less CPU for those blocks.
  - `CDP_FAST=0`, or no compiler, runs the original code. See README, "Speed".
- **The `cdp_daemon.py` login only asks for this PC's node number.**
  - It's pre-filled from `CDP_MY_ID` / `--node` or the last run.
  - The other nodes are discovered on the link: a presence broadcast every 5 s, and every node heard gets a chat.
  - The GRC text box now defaults to everyone (`CDP_DEST=255`).
  - The chat app also gained encryption (a DH key pair over a 2048-bit prime), voice (live talk, on until clicked again, and voice messages) and a performance dashboard.
  - It owns the datagram port 5201 and forwards the game's datagrams to 5202. A game started from the chat window is told to listen there.
  - `README.md` and `LOGS.md` describe both changes.

The game is just one more program using the bridge, like `cdp_sock.py`. The chat keeps working while the game runs.

## New in v4

- **Elevation:** a 3.0 m and a 2.0 m building with stairs, roofs you can fight from, and crates to jump on. Shots, line of sight, shadows and the host's move checks all work in 3D.
- **Weapons:** rifle (1), sniper with scope (2, right mouse), knife (3).
- **Healing:** pick up medkits and press H for +50 HP over 3 s, then an 8 s cooldown, with the timer on the HUD.

Rules and numbers are in `game/GAME.md`; tests are in `tools/test_features.py` (29 checks). Packets are v4: v3 games and v4 games don't see each other.

## The setting differences from v6.8

| | v6.8 game package | v6.9.1 (this package) |
|---|---|---|
| who starts the game | the flowgraph's Game Link block | `cdp_daemon.py` → **🎮 Start game** → `cdp_game.py` |
| game ↔ radio | UDP 52001 / 52002 via the Game Link block | the bridge's datagram ports: `CDP_UDP_PORT` 5200 (to the radio), `CDP_UDP_OUT_PORT` 5201 (from the radio) |
| header on air | 2 B tag `C5 12` | 11 B bridge header (`'U'`, sequence number, timestamp) |
| payload per frame | 68 B fits a 12.3 ms frame | 59 B for the same frame; snapshots carry fewer kill-feed events when full |
| unicast from the game | SR-ARQ (Block ACK) | a datagram without ACK; dest 255 = everyone |
| "ARQ on" | inputs unicast with SR-ARQ | inputs as reliable messages on TCP `CDP_ARQ_PORT` 5100 (`SEND 01`, received as `RECV 81`) |
| default | ARQ on | **ARQ bypass (datagrams)**: `CDP_GAME_ARQ=0` |
| MAC queue for game frames | newest-only per kind, aged out after 2 s (patched MAC) | FIFO per destination, 32 frames, oldest dropped (`CDP_UDP_QUEUE`). Not patched, so the game controls its own rate. |
| node ID / other nodes | `CDP_MY_ID` | `CDP_MY_ID`; `CDP_PEERS` = the nodes the daemon has heard when the game starts (the game finds the rest by its own beacons) |
| close | quit the game | **Quit game**, or **Close everything** (via `CDP_CTL_PORT` 5300, like `cdp_daemon.py --shutdown`) |
| settings file | `~/.cdp_game.json` | `~/.cdp_game_v69.json` |

## Why datagrams are the default

On v6.9 a reliable message is not dropped or replaced: it waits in the bridge until it is delivered. Game inputs are only useful while they are fresh, so with 3–4 players at 5/s, reliable inputs build a queue of tens of seconds.

The datagram service is what the game needs:
- no ACK traffic
- up to 4 frames per channel access
- a lost input is simply superseded by the next one

The host still judges every shot against what the shooter saw, and rejects anything older than the lag limit.

The menu button is labelled "Datagrams (ARQ bypass)" or "Inputs reliable (ARQ)". A host listens on both paths: if only one PC turns ARQ on, its inputs still arrive. The beacons announce it, and the host then also reads the reliable port.

## Congestion control (new, on by default)

The v6.9 MAC's datagram queue is a plain FIFO of 32. When the channel can't keep up, frames wait in it for seconds before the oldest is dropped. The game now measures this itself:
- **queueing delay** = the round-trip time to the host (or clients) minus the smallest round-trip time of the last 20 s
- **median > 0.25 s over the last 1.5 s:** the packet rate is cut to 0.7 × (minimum 1/s)
- **otherwise:** the rate rises by 0.5/s each second, back up to the menu rate

The HUD shows "radio queue building up (… ms): rate lowered to …/s" while it is active.

## Simulation results (simulated v6.9 MAC, 120 s per run)

Full tables are in `tools/SIM_REPORT_V69.md`; rerun with `python3 tools/worstcase_sim.py --v69`. The numbers below are with the congestion control on, as shipped.

| scenario | inputs as | hit applied > 1 s after the shot | view age the host judged at, p50 / p95 | MAC queue wait p95 |
|---|---|---|---|---|
| 2 players, 2 % loss | datagrams | 0 | 0.5 / 0.7 s | 0.1 s |
| 2 players, 10 % loss, interference, stalls | datagrams | 0 | 0.7 / 2.4 s | 1.8 s |
| 2 players, the same worst case | reliable | 0 | 2.3 / 5.2 s | 3.2 s |
| 3 players, 5 % loss | datagrams | 0 | 0.8 / 1.3 s | 0.1 s |
| 4 players, 2 % loss | datagrams | 0 | 0.9 / 2.1 s | 0.2 s |
| 4 players, worst case | datagrams | 0 | 0.9 / 2.7 s | 1.8 s |
| 4 players, 2 % loss | reliable | 0 | 1.9 / 4.4 s (fixed rate: 31 / 55 s) | 4.3 s (fixed rate: 55 s) |
| 4 players at 10/s | reliable | 0 | 3.1 / 18 s (fixed rate: 22 / 37 s) | 22 s (fixed rate: 83 s) |

What this shows:
- **The instant-death bug stays fixed on v6.9.** In every scenario, no hit was applied more than 1 s after the shot (the max was 0.5 s): the host's lag limit holds, whatever the queue does.
- **Use datagrams.**
  - With them the radio queue stays at 3–4 frames.
  - Shots are judged at about the interpolation delay plus one trip.
  - 2–4 players at 5/s fit.
  - The congestion control only steps in briefly, and the rate comes back.
- **Reliable inputs** only work for 2 players. With 4 players they pile up to the 256-frame cap. The congestion control cuts that from about a minute of queue to a few seconds, but the result is still much worse than datagrams.

## Files

| file | what |
|---|---|
| `cdp_game.py` | launcher the daemon finds; runs `game/fps_game.py --stack v69` with the session's environment (+ `CDP_GAME_ARGS`) |
| `cdp_fast.cpp`, `cdp_fast.py` | C++ kernels for the flowgraph's per-sample Python blocks; built with g++ on first use, NumPy fallback |
| `tools/test_fast_blocks.py`, `tools/bench_blocks.py` | blocks with kernels == without (event by event); CPU with / without |
| `tools/apply_fast_to_grc.py`, `tools/epy_harness.py`, `tools/grstub/` | add the kernel hooks to another v6.9.1 .grc; run embedded blocks outside GNU Radio (for the tests) |
| `game/fps_game.py` | window, menu, lobby, HUD, pause menu (+ Close everything); `--stack v69` (default) or `v68` |
| `game/game_core.py` | game logic. `BridgeLink` is the v6.9 link: datagrams on 5200/5201, reliable inputs on TCP 5100. Also: host, rewind, prediction, election, migration, congestion control. |
| `game/payload.py`, `game/c/` | the packet format (v4: + weapon, healing, medkits, 2 cm heights), Python and C, byte-identical |
| `tools/test_features.py` | elevation, healing, medkits, sniper and knife through the real network code |
| `tools/test_bridge_link.py` | the game against a fake bridge speaking the real socket protocol, with 3 nodes, in three modes: datagrams, reliable, and mixed. Also checks that chat messages on the reliable port are ignored. |
| `tools/radio_model.py` | MAC simulator; `Mac69` = the v6.9 MAC with FIFO datagram queues |
| `tools/worstcase_sim.py` | worst-case runs; `--v69` for this stack |
| `tools/SIM_REPORT_V69.md`, `tools/SIM_REPORT.md` | results on v6.9 / v6.8 |

## Next step (not done yet, as agreed)

Chat inside the game. The game can open the same TCP port as the chat. It would send the chat's own format (`MAGIC b"CDPC2\0"` + JSON header) so that `cdp_daemon.py` shows those messages too.
