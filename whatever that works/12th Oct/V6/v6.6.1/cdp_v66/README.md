# CDP Link v6.6: chat or game over the radio

Everything needed to run the chat app or the Radio Arena game over the v6.6 CSMA flowgraph. Copy this folder to both PCs and run the commands below.

The flowgraph `v6.6_csma_transport.grc` is unchanged. The game uses the existing UDP Transport block, so the flowgraph needs no new blocks.

```
cdp_v66/
├── v6.6_csma_transport.grc   flowgraph (unchanged)
├── cdp_sock.py               daemon library + CLI, now with the launcher:  python3 cdp_sock.py launch
├── cdp_chat_v66.py           chat app (now ignores game packets, see below)
├── CHAT_V66.md               chat manual
└── game/
    ├── fps_game.py           Radio Arena (3D shooter), new --peer mode for the CDP radio
    ├── game_core.py          game logic + CdpLink (talks to the UDP block)
    ├── payload.py            the 31-byte game payload (build_payload / parse_payload)
    ├── c/                    the same payload in C (payload.c, payload.h, test_payload.c)
    ├── GAME.md               controls, options, payload format
    └── extras/               old plain-UDP bridge test flowgraph (not needed for v6.6)
```

## One-time install (Fedora)

```bash
sudo dnf install python3-qt5 python3-pygame python3-pyopengl
```
If dnf or pip can't download anything on the university Wi-Fi, log in to `wlan.uom.lk` first.

## Start each node

**Node A**
```bash
cd ~/cdp_v66
export CDP_MY_ID=2
gnuradio-companion v6.6_csma_transport.grc &
python3 cdp_sock.py launch
```

**Node B**
```bash
cd ~/cdp_v66
export CDP_MY_ID=5
gnuradio-companion v6.6_csma_transport.grc &
python3 cdp_sock.py launch
```

In GRC press Run (F6). The launcher window then shows:

- **This node.** Taken from `CDP_MY_ID`. You can change it in the window.
- **Flowgraph running / not running.** Checked every 2 s.
- **💬 Chat.** Enter your name, then click **Open chat**.
- **🎮 Radio Arena.** Enter the opponent's node ID, then click **Play over the radio**. **Practice vs bot** works without any radio. "Known nodes" lists the nodes your chat has seen.
- **Running app.** Shows the running app, its console output and a **Stop** button.

**Only one app at a time.** Both apps receive on the UDP block's port 5201, and only one program per PC can use it. If you pick the other app, the launcher asks, closes the running one and starts the new one.

**Playing a match:** both PCs pick Radio Arena and enter each other's node ID. With node IDs 2 and 5, node 2 always plays blue (P0) and node 5 plays red (P1).

## All launcher commands

```bash
python3 cdp_sock.py launch                       # window (terminal menu if there is no display)
python3 cdp_sock.py launch --text                # terminal menu: 1 chat, 2 game over radio, 3 game vs bot
python3 cdp_sock.py launch chat --name Methal    # start the chat directly
python3 cdp_sock.py launch game --peer 5         # game against node 5 directly
python3 cdp_sock.py launch game --peer 5 --rate 3
python3 cdp_sock.py launch game --bot            # offline practice
python3 cdp_sock.py launch --node 2              # override CDP_MY_ID for the launcher
```

The apps can still be started without the launcher:
```bash
python3 cdp_chat_v66.py --name Methal
python3 game/fps_game.py --peer 5                # this node = $CDP_MY_ID, or add --node 2
python3 game/fps_game.py --bot
```

The existing daemon commands are unchanged:
```bash
python3 cdp_sock.py arq-send --dest 5 --text "hello"
python3 cdp_sock.py arq-send --dest 5 --file photo.jpg
python3 cdp_sock.py arq-listen --out-dir ~/cdp_inbox
python3 cdp_sock.py udp-send --dest 255 --text "hi all"
python3 cdp_sock.py udp-listen                   # uses port 5201 too: not together with chat/game
```

## How the game uses the radio

- **Each packet is one radio frame.** The game sends its 31-byte state packet to the UDP Transport block as `dest node | payload` (port 5200). That is one unicast frame to the opponent, sent once, with no ARQ. A late position is useless, so nothing is retransmitted.
- **Hits survive lost packets without ACKs.** Each hit report rides in 6 packets in a row.
- **Receiving (port 5201):** the game keeps only datagrams from the opponent's node that are exactly 31 bytes. Chat beacons and broadcasts are ignored.
- **Packet rate:** the default is **5 packets/s per player** (Packets/s box, or `--rate`). Each frame takes about 8 ms on air (128-byte preamble, Hamming 31 coding, padded to whole sink buffers). Most of the time per frame is the CSMA access: DIFS 20 ms plus a random backoff of 0–7 slots of 15 ms. One node manages roughly 10 frames/s at most.
  - Above about 8/s, the CSMA queue (64 frames) fills and the delay grows to seconds.
  - If the `[CSMA]` stats line in GRC shows the queue growing, lower the rate to 3–4.
- **Smooth movement at low rates.** Between packets the game predicts the opponent's movement for up to 0.3 s, never into walls.
- **HUD link line:** shows packets/s received and the loss % (from the sequence numbers).

## Chat app change

**Game packets no longer show up as files in the chat.** If one PC plays while the other has the chat open, the chat used to save each game packet as a `data_from_nodeX.bin` file in `~/cdp_inbox`, about 5 per second. It now ignores UDP datagrams that are neither chat messages nor readable text, and logs this once. Text sent with `cdp_sock.py udp-send` still appears in the chat. Nothing else in the chat changed.

## Troubleshooting

| Message | Fix |
|---|---|
| `another program is receiving on udp 127.0.0.1:5201` | Close the chat, the game or `cdp_sock.py udp-listen` first. Or use the launcher's Stop button. |
| `this node's ID is unknown` | Run `export CDP_MY_ID=<id>` (the same value the flowgraph uses), or set it in the launcher. |
| Game says "waiting for opponent" | The other PC must also be playing with *your* node ID as its opponent. Check that both flowgraphs run on the same frequency. |
| Game turning stops at the screen edge | `python3 game/fps_game.py --peer 5 --x11`. The arrow keys also turn. |
| `missing Python packages` | `sudo dnf install python3-qt5 python3-pygame python3-pyopengl` |

The launcher never connects to the ARQ block for its status check. It checks the UDP port instead. A connection would make the ARQ block hand over the chat messages it kept while no chat was open, and the launcher would lose them.
