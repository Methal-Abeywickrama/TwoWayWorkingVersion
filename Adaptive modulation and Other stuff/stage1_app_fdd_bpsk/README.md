# Stage 1 — Application (SR-ARQ + chat/folder sync) on the real FDD radio link (BPSK)

Starting point: your working `integrationTest.grc` (app + SR-ARQ over a loopback channel model).
This stage puts it on the two ANTSDRs (FDD, same Pluto settings as your encryption flowgraph) and
fixes the link-layer bugs that stop two *different* nodes from talking. Modulation is still the
working BPSK chain; nothing else changes.

## What was wrong
| # | Problem | Effect |
|---|---------|--------|
| 1 | Framer `peer_id` hard-coded to `1`, so every frame carried link `dest_id = 1` | Node 2 dropped every frame from node 1, so the SYN never arrived → `SYN retry limit exceeded` → `tx_failed` (reproduced with your original file in the offline test) |
| 2 | Deframer `max_bit_errors` was wired to `tl_peer_addr` (= 2) | Sync threshold silently loosened to 2 bit errors → more false syncs |
| 3 | Radio blocks disabled; TX looped straight into RX through a channel model | Only one node could exist; it also heard itself |
| 4 | Link layer ignored broadcast (`dst_addr 0`), which the FSM supports | Broadcast transfers impossible |

## What changed (GRC edits vs `integrationTest.grc`)
- **Variables added:** `TX_FREQ_A` 2412e6, `TX_FREQ_B` 2437e6, `fdd_side`, `tx_freq`, `rx_freq`, `iq_tx_port`, `iq_rx_port`; `ADDR` now `os.environ.get('TL_SDR_URI', 'ip:192.168.1.10')`.
  `fdd_side` = `TL_FDD_SIDE` env, default **A if TL_LOCAL_ADDR < TL_PEER_ADDR else B**. Side A transmits on 2412 MHz and listens on 2437 MHz; side B does the reverse. The same file therefore works on both laptops, with no per-node edits.
- **PlutoSDR Sink** enabled: frequency `int(tx_freq)`, bandwidth `int(1.5e6)`. **PlutoSDR Source** enabled: frequency `int(rx_freq)`, bandwidth `int(1.5e6)`.
- **Channel Model** moved from the loopback into the RX chain: `AGC → Channel Model → FLL`. Its noise is now the **`nv` slider** ("RX extra noise", default 0 = transparent). Use it to stress the link.
- **ZMQ PUB Sink / ZMQ SUB Source (complex)** added for a no-radio test on one PC. They are disabled in `stage1_hw.grc`; in `stage1_sim.grc` they replace the Pluto blocks.
- **epy_block_0_0 (framer):** link `dest_id` = the FSM's `dst_addr` metadata on each PDU (fallback `peer_id = tl_peer_addr`).
- **epy_block_0_0_0 (deframer):** `max_bit_errors = 1`; accepts `dest_id == MY_ID` **or 0** (broadcast).

## FDD and CSMA
**FDD stays.** Each node owns its transmit frequency, so the two transmitters never share a channel. The SR-ARQ's DATA and ACK can be on air at the same time, with no turnaround or collision handling.
**CSMA is not needed** for the same reason: there is nothing to sense, because no one else transmits on your TX frequency. CSMA (or TDMA) only becomes necessary if you move both nodes to one frequency (TDD) or add a third node.

## Files
- `stage1_hw.grc`: open in GRC (generates `transeciever.py`).
- `stage1_sim.grc`: same flowgraph, radios swapped for ZMQ IQ (generates `transeciever_sim.py`).
- `generated_hw/`, `generated_sim/`: already generated with GNU Radio 3.10.12's `grcc`, ready to run.
- `app/`: your `folder_sync_daemon.py` and `cdp_chat.py`, unchanged.
- `run_node.sh`, `run_app.sh`: start a node and its chat window with the right env vars.
- `offline_test/`: the logic test used to verify this stage (no GNU Radio or radio needed).

## How to test
**1. Offline logic test (any PC, ~30 s):** `python3 offline_test/test_stage1.py`
It runs two nodes built from `generated_hw/` (your FSM, header blocks, framer, deframer and the daemon's frame format) over a bit-error channel. Expected output is in `offline_test/expected_output.txt`; the last line must be `RESULT: PASS`.

**2. One PC, no radio:** four terminals in this folder:
```
./run_node.sh A sim      ./run_node.sh B sim      ./run_app.sh A sim      ./run_app.sh B sim
```
**3. Two laptops + ANTSDRs:** laptop 1 runs `./run_node.sh A` and `./run_app.sh A`; laptop 2 runs `./run_node.sh B` and `./run_app.sh B`.
(Each ANTSDR at `ip:192.168.1.10`, or `export TL_SDR_URI=...` first.)

Then send a chat message and drop a file (e.g. a 5–20 kB image) into the chat or into `node_A/outbox/`.

Note: if you reuse your old `link.json` (`{"peer": 1}`), node A would send to itself. Delete it, or set `peer` to the *other* node's address.

## Pass criteria
- Daemon log on the sender: `[TX] <file>: ... -> addr 2 ...` then `[TX] <file>: delivered`.
- The file appears in `node_B/inbox/`, identical (`cmp` it). The chat shows ✓ on the message.
- Flowgraph console (per frame): `[TX] msg #n (to peer 2): ... B payload (1 bursts queued)`; the other side prints `[RX RECEIVED] #n:` and `[RX] msg #n: 1/1 bursts OK`.
- Same in the other direction (B → A).
- With the `nv` slider raised you see `FSM: DATA RTO` retries, but transfers still finish until the link is truly gone.
