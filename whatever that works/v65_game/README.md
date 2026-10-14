# v6.5 CSMA mono + Radio Arena

`v6.5_csma_mono_game.grc` is your `v6.5_csma_mono.grc` with the game added straight onto the link layer. There is no daemon, ARQ or UDP transport block. Press Run in GRC and the game window opens; stop the flowgraph and it closes.

```
v65_game/
├── v6.5_csma_mono_game.grc   your v6.5 flowgraph + Game Link block
├── epy_game_link.py          the Game Link block's code (same as inside the .grc, for reading)
└── game/                     Radio Arena (must stay next to the .grc)
    ├── fps_game.py  game_core.py  payload.py  GAME.md
    └── c/                    payload in C
```

## One-time install (Fedora)
```bash
sudo dnf install python3-pygame python3-pyopengl
```

## Start each node

**Node 0**
```bash
cd ~/v65_game
export CDP_MY_ID=0
gnuradio-companion v6.5_csma_mono_game.grc &
```

**Node 1**
```bash
cd ~/v65_game
export CDP_MY_ID=1
gnuradio-companion v6.5_csma_mono_game.grc &
```

Press Run (F6) on both. Each PC opens its game window: node 0 plays blue (P0), node 1 red (P1). The HUD shows "P1 linked … pkt/s" once the other node's packets arrive. The QT text box still works for typed messages at the same time.

### Options (environment, set before starting GRC)

| Variable | Default | Meaning |
|---|---|---|
| `CDP_MY_ID` | 1 | this node's ID (as before) |
| `CDP_PEER_ID` | `1 - CDP_MY_ID` for IDs 0/1 | the other node's ID. Needed for other IDs, e.g. 2 and 5: `export CDP_PEER_ID=5` |
| `CDP_GAME` | 1 | `0` = don't open the game window. The Game Link then prints the command to start it yourself. |

To run the game by hand (with `CDP_GAME=0`), on node 0:
```bash
python3 game/fps_game.py --id 0 --tx-port 52001 --rx-port 52002 --rate 5      # node 1: --id 1
```

## What was changed in the flowgraph

| Block | Change |
|---|---|
| **Game Link** (`epy_game`, new) | Starts/stops the game. Takes the game's 31-byte states from udp 52001 and gives received ones to the game on udp 52002. |
| Packet Framer TX (`epy_block_0_0_1`) | Binary payloads starting with `C5` are sent without the `[TX] msg …` line. The input metadata key `app` is copied into the frame PDU. |
| Packet Deframer RX (`epy_block_0_0_0`) | No `[RX RECEIVED]`, `[RX] msg …` or `[RX dbg] frame … delivered` lines for payloads starting with `C5`. They still count in `[RX stats]`. |

Three new message connections:
- Deframer `pdu_out` → Game Link `from_radio`
- CSMA `pdu_out` → Game Link `tx_done`
- Game Link `to_framer` → Framer `msg_in`

Everything else is identical to your v6.5 file: PHY, CSMA, squelch, correlator, sliders.

## How it avoids the full CSMA queue

- **Newest state only.** The Game Link keeps just the latest game state. It gives a new state to the framer only after its previous game frame has left the CSMA block, which it learns from the CSMA's `pdu_out`. It also sends at most `max_rate` (5) frames/s.
  - The CSMA queue never holds more than one game frame, so nothing goes on air seconds late.
  - If the channel only manages 2 frames/s, you get 2 fresh updates per second instead of a growing backlog.
  - States overtaken by a newer one are dropped ("replaced by newer").
- **Frame format.** On air the payload is `C5 10` + the 31-byte game payload. The 2 bytes are a link-layer type, the same idea as the v6.6 transport blocks; the game payload itself is unchanged. One game frame measured **384 bytes = 12.3 ms** on air with this framer (128-byte preamble, Hamming 31, padding).
- **Stats line.** Every 10 s the Game Link prints:
  ```
  [GAME] since last line: game states in 50 | sent 23 frames, on air 23, replaced by newer 27, timeouts 0 | time to air 180 ms median, max 420 ms | from radio to game 21 (other nodes 0)
  ```
  "Time to air" is how long each game frame waited for the CSMA.
- **HUD loss %.** The game's "loss %" also counts states the Game Link skipped on purpose (they show up as gaps in the sequence numbers).

## Tuning

| Setting | Where | Effect |
|---|---|---|
| `max_rate` | Game Link block | upper limit of game frames/s per node (5) |
| `game_rate` | Game Link block | how often the game produces a state (5/s). Keep it ≥ `max_rate`. |
| `inflight_timeout_s` | Game Link block | send anyway if a frame was never seen leaving the CSMA (1.5 s) |
| CS margin slider | GUI | your last log showed ~19 deferrals per frame. Try 9 dB instead of 6 to cut false-busy deferrals and get more frames/s. |

## Troubleshooting

- **`[GAME] game not found`**: the `game` folder must sit next to the `.grc`.
- **`[GAME] cannot listen on udp 127.0.0.1:52001`**: another flowgraph or game is still running on this PC.
- **The game window closes right away**: run `python3 game/fps_game.py --bot` in a terminal to see the error.
- **Both games say "waiting for opponent"**: check `CDP_MY_ID` / `CDP_PEER_ID` on both PCs. The `[RX stats]` line shows whether frames arrive at all ("good frames by src ID").
- **A console line for every frame**: the train-word lines (`[RX dbg] train word …`) still appear for every frame, game frames included, at most 20/s. Set the deframer's `debug` to 0 to silence them (that also turns off `[RX stats]`).
