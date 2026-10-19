# CDP link v6.9.1 – how to run (Linux and macOS)

## What changed in v6.9.1

- **Big files no longer fail on one lost frame.** In v6.9 a message failed as soon as the MAC gave up on any one of
  its segments (8 attempts). A 2 MB file is 2000 segments, so on a link that loses even a few % of frames or Block ACKs,
  one of them always hit the retry limit. Now the bridge hands such a segment to the MAC again. A message fails only if
  nothing reached the other node for `CDP_MSG_STALL` seconds (default 60), i.e. the link is really down. The old fixed
  15-minute limit is gone.
- **A failed message stops using the air.** Its frames still waiting in the MAC are cancelled.
- **The receiver keeps several messages apart.** A resent segment of an already delivered message used to wipe the
  message being received. Now it is ignored.
- **A text overtakes a file.** The bridge sends from the message with the fewest segments left first.
- **Progress in the chat.** "sending… 45 % (900/2000 parts, 8.1 kB/s)".
- **v6.7 test results applied:**
  - p-persistence default 0.2 (was 0.04)
  - CWmax default 63 (was 1023)
  - TX attenuation start value from `CDP_TX_ATT`, per PC
- The GRC edit box no longer sends a "MESSAGE" frame at every start.
- `cdp_sock.py`: `wait()` no longer misses a SENT that arrived while waiting for another message. It also knows the
  progress message.

### Per-PC start (example: Methal = node 0, fedora = node 1)

```bash
# Methal (its signal was too strong at fedora)
export CDP_TX_ATT=32
python3 cdp_daemon.py
```
```bash
# fedora (its signal was about 18 dB weaker at Methal): 12 dB below the value of the last v6.7 test
# (32 -> 20; if that test ran fedora at 20, use 8)
export CDP_TX_ATT=20
python3 cdp_daemon.py
```
The login screen only asks for this PC's node number (pre-filled from `CDP_MY_ID` or the last run); the other nodes are found by themselves (see "Start"). The slider "TX attenuation while sending" still works while
running; `CDP_TX_ATT` only sets where it starts.

Put these files in **one folder** on each PC (the same files on both):

| file | what it is |
|---|---|
| `cdp_daemon.py` | **the only thing you start.** Opens the flowgraph, finds the other nodes, chat window, logs, "Close everything". |
| `v6.9_cdp_link.grc` | flowgraph: v6.8 CSMA/CA + SR-ARQ, plus a datagram (UDP) service, the socket bridge and the session logger |
| `cdp_logsum.py` | turns the session logs into a short `summary.md` |
| `cdp_sock.py` | command-line test tool and library for the link (same protocol as before) |
| `LOGS.md` | structure of every log file and how to analyse them |

`folder_sync_daemon.py` and `cdp_chat.py` are replaced by `cdp_daemon.py`. Don't run them with v6.9.

## Start

**Linux**
```bash
cd ~/cdp_link
python3 cdp_daemon.py
```
**macOS (radioconda)**
```bash
conda activate radioconda          # the environment that has GNU Radio and PyQt5
cd ~/cdp_link
python cdp_daemon.py
```

Then:
1. **Login screen: only your node number** (your address) and how to start the flowgraph. No opponents to enter any more.
   - The number is pre-filled from `--node N`, else `CDP_MY_ID`, else the number used last time on this PC, else one derived from the computer's name.
   - Each PC needs its own number. If two PCs use the same one, the chat window says so in red.
2. GNU Radio Companion opens `v6.9_cdp_link.grc` with your settings already in its environment (`CDP_MY_ID`, `CDP_DEST`, `CDP_DEBUG_DIR`, …). **Press ▶ Run (F6).**
   - Or choose "Run the flowgraph directly" at login. That option appears once GRC has generated `cdp_link.py`, i.e. after the first run from GRC.
3. The chat window opens. **The other nodes are found by themselves:**
   - While the flowgraph runs, every daemon broadcasts a short *presence* message every `CDP_PRESENCE_S` seconds (default 5).
   - A node that is heard (presence or any message) gets its own chat at once: ● online, ○ away (not heard for 18 s).
   - A newly heard node is answered within about a second, so two PCs find each other within a second or two of both flowgraphs running.
   - The sidebar shows the link state. It turns *connected* as soon as the flowgraph runs.
4. **⏻ Close everything** (or closing the window) stops the game, the flowgraph, GNU Radio Companion and the daemon.

Do the same on the other PC with its own node number. The chat starts empty every time. Received files are saved in `received/<session>/`.

Buttons in the sidebar:
- **👥 Nodes in this session**: you and every node heard (computer name, online/away), when each was last heard, messages sent and received
- **🎮 Start game**: shown when a game is configured (below)
- **📂 Open session logs**

## The game

The daemon starts the game with **🎮 Start game** if either:
- `CDP_GAME_CMD` is set, e.g. `export CDP_GAME_CMD="python3 shooter.py"`, or
- a file `cdp_game*.py` / `game*.py` lies in the folder.

The game gets the environment `CDP_MY_ID`, `CDP_PEERS` (e.g. `2,3`), `CDP_DEST`, `CDP_UDP_PORT` (5200), `CDP_UDP_OUT_PORT` (5201), `CDP_ARQ_PORT` (5100) and `CDP_CTL_PORT` (5300).

Datagrams go exactly as before with `cdp_sock.py`'s `UdpClient`:
- send `dest | data` to 127.0.0.1:5200
- receive `src | dest | data` on 127.0.0.1:5201

**The game's own close button.** Calling this stops everything (game, flowgraph, GRC, daemon):

```python
import os, socket
def close_everything():
    with socket.create_connection(("127.0.0.1", int(os.environ.get("CDP_CTL_PORT", "5300"))), timeout=2) as s:
        s.sendall(b"SHUTDOWN\n")
```

From a terminal, the same thing is: `python3 cdp_daemon.py --shutdown`.

(The game's source was not among the files, so this snippet still has to be added to the game itself.)

## Chat app: encryption, voice, performance dashboard

**🔒 Encryption** is on by default. The sidebar button switches it off or on.

- **Keys:** every PC has a key pair, made on the first start and kept in `~/.config/cdp_link/identity.json`.
  - The private key is a random number x.
  - The public key is g^x mod p, over a 2048-bit prime p (RFC 3526).
  - The public key travels in the presence message.
  - Both PCs compute the same shared number y_other^x mod p; nobody who only heard the two public keys can.
- **Messages:** text, files and voice messages are encrypted with a key made from that shared number (SHA-256 keystream), and carry an authentication tag (HMAC). A changed message is rejected.
- **Waiting for a key:** a message to a node whose key hasn't arrived yet waits (🔑) and goes out as soon as the node's next presence message brings the key.
- **Fingerprints:** "👥 Nodes in this session" shows every node's key fingerprint. Compare it once with what the other PC shows for itself; if they match, nobody in between swapped the keys.
- This is basic encryption for learning and for keeping casual listeners out. It hasn't been audited like TLS.

**🎙 Voice:**
- **🎙 Talk:** click once to go on air to the open chat's node. It stays on until you click **⏹ Stop talking**.
  - 8 kHz speech, IMA ADPCM, one datagram per 100 ms: about 4.2 kB/s on the air while you talk.
  - Played at the other end after a 0.2 s jitter buffer. A lost frame is filled in.
  - Each frame is encrypted when encryption is on.
- **Voice msg (click, talk, click again):** records up to 60 s and sends it as a reliable chat item. It arrives complete even on a bad link; ▶ play in the chat.
- **Audio:** comes from `sounddevice` if installed, else `parec`/`pacat` (PipeWire/PulseAudio, Fedora: `sudo dnf install pulseaudio-utils`), else `arecord`/`aplay` (`alsa-utils`).
- **Datagram routing:**
  - The daemon now owns the datagram port 5201.
  - It forwards everything that isn't voice to the game on 5202 (the game started from the chat window is told so).
  - `cdp_sock.py udp-listen` therefore can't run next to the daemon; stop the daemon or use `CDP_UDP_OUT_PORT` for a test.

**📊 Performance dashboard** opens in the web browser (`http://127.0.0.1:5310/`, nothing from the internet). It updates every second from this session's logs.

- **Time axis:** it follows the clock, so the charts keep moving while the link is idle. The logger writes its per-second files only every 10 s when nothing happens, and the dashboard fills those seconds in.
- **Status at the top:** *live*, *live · link idle*, *flowgraph stopped?* (no log rows for 15 s) or *waiting for the flowgraph's logs* (press ▶ Run in GRC).
- **No data at all and no `[LOG] performance logs -> …` line in the flowgraph console:** the Session Logger is not
  running. Up to this version it was not wired to anything, and GNU Radio never starts an unconnected block. It is now
  connected (bridge `mac_ctrl` -> logger `attach`); keep that connection if you edit the flowgraph.
- **Which logs:** the flowgraph writes its logs into the folder it was started with (`CDP_DEBUG_DIR`). If it was started earlier, e.g. a GRC left open from the last start, it keeps writing into that older session's folder.
  - The dashboard looks for the folder that a flowgraph **of this node** is writing to right now: this session's folder, else `debug/` and `~/cdp_debug/`. It follows that folder.
  - A yellow note then names the folder. To get the logs and the chat in one folder again, close GRC and press ▶ Run in the GRC window the chat app opens.
  - If no flowgraph on this PC is writing logs, the note says that too.
- **The last line** names the folder it reads and the rows found in each file.
- **Chat app chart:** this app's own messages and files, delivered (acknowledged, from the bridge's progress reports) and received, in kB/s. It is measured by the chat app itself every second, so it moves even without any flowgraph logs.
- **Errors:** if the dashboard can't read or draw something, it shows the error text in red at the top instead of freezing.

- **Tiles (last 10 s), each marked ok / high / critical:**
  - link and nodes online
  - throughput
  - channel busy
  - queueing delay
  - CRC frame errors
  - datagram loss
  - Block ACK timeouts
  - PC load
- **Charts (1 / 5 / 15 min or the whole session):**
  - throughput and frames per second
  - channel use
  - delays (queueing, access, jitter)
  - errors
  - retries and drops
  - queues
  - CPU and sample rate
  - voice
- **Tables:**
  - the nodes with their key fingerprints
  - the last reliable messages
  - encryption and voice counters
  - the last events

For an old session or the other PC's logs: `python3 cdp_metrics.py debug/<session>` (live), or `--html report.html` (one file to send).

## Speed: C++ kernels for the Python blocks

Four Python blocks of the flowgraph touch every sample or bit: the MAC (carrier sense and its per-256-sample state
machine), the training-word correlator, the deframer and the noise-floor tracker. Their per-sample loops now run
in C++ (`cdp_fast.cpp`, loaded by `cdp_fast.py`):

- **Same results.** The logic stays in the blocks' Python code; C++ only runs the stretches where nothing but
  arithmetic happens, with the same operations in the same order.
  - The MAC hands every step that has a side effect back to its own Python code: a busy/idle change, a random draw,
    an event, the start of a transmission, a Block ACK deadline, a received frame.
  - `tools/test_fast_blocks.py` runs each block on long realistic streams with and without the kernels and compares
    everything it produces: samples, tags, every MAC event in order, the bursts sent, the frames delivered,
    counters.
- **Less CPU.** Measured at 1 MS/s, the four blocks need about 2.2 to 2.7 times less CPU (`python3 tools/bench_blocks.py`).
  - Sync search: about 7 to 10 times faster.
  - Correlator: about 2.5 times faster.
  - MAC: about 1.6 to 2 times faster.
  - The kernels run without holding Python's GIL, so they don't block the other Python blocks while they work.
- **Automatic.** The first start compiles `cdp_fast.cpp` with `g++` (about 2 s) into `~/.cache/cdp_fast/`.
  - The chat app does this in the background as it starts. Later starts load the cached library.
  - A self-test against the NumPy code runs before each use.
- **Console:** the flowgraph prints `[FAST] <block>: C++ kernel on` for each block, or `original Python code (<reason>)`.
- **Safe fallback.** If `g++` is missing, the build fails, or the self-test finds a difference, the blocks run their
  original NumPy code, which is byte-for-byte the v6.9.1 code. `CDP_FAST=0` forces that.
- **One-time install (Fedora):** `sudo dnf install gcc-c++`.

## Settings (optional, environment variables before starting the daemon)

| variable | default | meaning |
|---|---|---|
| `CDP_MY_ID` | – | pre-fills the node number at login (or `--node N`); without it: the number used last time, else one from the computer name |
| `CDP_PRESENCE_S` | 5 | how often the daemon announces itself so the other nodes find it (s) |
| `CDP_ENCRYPT` | 1 | `0` starts with encryption off (the sidebar button switches it at any time) |
| `CDP_IDENTITY` | `~/.config/cdp_link/identity.json` | where this PC's key pair is kept |
| `CDP_AUDIO` | auto | `sounddevice`, `pulse` (parec/pacat), `alsa` (arecord/aplay) or `off`; or set `CDP_AUDIO_IN_CMD` / `CDP_AUDIO_OUT_CMD` to any commands that record / play raw s16le 8 kHz mono |
| `CDP_VOICE_FRAME_MS` | 100 | voice per datagram (200 = half as many frames on the air, 0.1 s more delay) |
| `CDP_GAME_UDP_PORT` | 5202 | where the daemon forwards the game's datagrams |
| `CDP_DASH_PORT` | 5310 | the dashboard's local web port |
| `CDP_FG_ECHO` | 1 | "Run the flowgraph directly": its output ([MAC], [RX], [FAST] …) also shows in the daemon's terminal (always in `debug/<session>/flowgraph.log`); `0` = log file only |
| `CDP_FAST` | 1 | `0` runs the flowgraph's blocks without the C++ kernels (original Python code) |
| `CDP_FAST_CXX`, `CDP_FAST_FLAGS` | `g++`, `-O3 -march=native` | compiler and flags for `cdp_fast.cpp` |
| `CDP_SAMP_RATE` | `1e6` | sample rate. v6.8 used 1e6; the last hardware-proven v6.5 ran at 1.5e6. |
| `CDP_ARQ_M`, `CDP_ARQ_N` | 4, 16 | frames per burst / selective-repeat window |
| `CDP_MSS` | 1000 | bytes per reliable segment (one radio frame) |
| `CDP_INFLIGHT` | 32 | segments per destination handed to the MAC at a time |
| `CDP_UDP_QUEUE` | 32 | datagrams queued per destination before the oldest is dropped |
| `CDP_TX_ATT` | 0 | TX attenuation while sending (dB), start value of the slider. Higher = weaker signal. (Default 20 before the game package.) |
| `CDP_MSG_STALL` | 60 | a reliable message fails only when nothing reached the other node for this many seconds |
| `CDP_MAC_P`, `CDP_MAC_CWMAX` | 0.2, 63 | p-persistence and maximum contention window (v6.9: 0.04, 1023) |
| `CDP_MAC_*` | see `V6.8_CSMA_SRARQ_COMMANDS.md` | slot, SIFS, turnaround, CWmin, retries, Block ACK timeout, TXOP |
| `CDP_GRC_CMD` | `gnuradio-companion` | command that opens GRC |
| `CDP_GRC` | newest `v6.9*cdp_link*.grc` here | flowgraph file |
| `CDP_ARQ_PORT`, `CDP_UDP_PORT`, `CDP_UDP_OUT_PORT`, `CDP_CTL_PORT` | 5100, 5200, 5201, 5300 | local ports |

## Logs

Each run of the daemon makes `debug/<date>_<time>_node<N>/` with:
- from the daemon: `daemon.log`, `session.json`, `chat.csv`
- from the flowgraph: `csma_txop.csv`, `csma_1s.csv`, `arq_tx.csv`, `arq_rx.csv`, `udp.csv`, `msgs.csv`, `phy_1s.csv`, `events.log`, `flowgraph.json`

To analyse, copy the session folders of both PCs to one place and run:
```bash
python3 cdp_logsum.py debug/<session_PC_A> <session_PC_B> -o summary.md
```
Upload `summary.md` first; add individual CSVs if a question needs the details. `LOGS.md` documents every column.

## Quick link test without the chat

```bash
python3 cdp_sock.py arq-send --dest 2 --text "hello"        # reliable, waits for SENT/FAIL
python3 cdp_sock.py arq-listen
python3 cdp_sock.py udp-send --dest 2 --text "hi"
python3 cdp_sock.py udp-listen
```
