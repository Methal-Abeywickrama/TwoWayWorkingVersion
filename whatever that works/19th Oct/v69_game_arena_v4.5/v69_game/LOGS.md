# CDP link v6.9 – session logs: structure and how to analyse them

Every time `cdp_daemon.py` runs it creates

```
debug/<YYYYmmdd_HHMMSS>_node<N>/
```

next to itself (`debug/` is created if missing). Both the daemon and the flowgraph's **Session Logger** write into this folder. The daemon passes the folder to the flowgraph as `CDP_DEBUG_DIR`.

If the flowgraph is started without the daemon, its files go to `~/cdp_debug/<date>_<time>_node<N>/` instead.

**Who writes them:** the Session Logger block of the flowgraph. Its console line `[LOG] performance logs -> <folder>`
at start says where. It must stay connected (bridge `mac_ctrl` -> logger `attach` in the .grc): GNU Radio runs only
blocks that sit on a connection, and a logger without one writes nothing.

**Live view:** "📊 Performance dashboard" in the chat window (or `python3 cdp_metrics.py debug/<session>`) draws these files as they grow: tiles for the last 10 s and charts. `--html report.html` saves one page.

**Quick start for analysis:**
```bash
python3 cdp_logsum.py debug/<session_A> <session_B_from_the_other_PC> -o summary.md
```
`summary.md` is 100–200 lines, small enough to paste to an LLM. Upload the raw CSVs only when a question needs details: slice by time with the `t` column, or by `bsn` / `useq` / `mid`.

---

## 1. Files at a glance

| file | written by | one line per | typical volume |
|---|---|---|---|
| `session.json` | daemon | – (node and how it was chosen, `opponents` = nodes heard so far, `peers` = when each was first heard and its computer name, OS, ports, flowgraph file, `t0`) | 1 kB |
| `daemon.log` | daemon | lifecycle event (start, node N joined, its key fingerprint, processes started/stopped, link up/down, node number conflicts, encryption on/off, messages that could not be decrypted, voice setup, dashboard URL, whether the C++ kernels are ready) | tiny |
| `chat.csv` | daemon | chat message / image / file queued, delivered, failed or received | tiny |
| `grc.log` / `flowgraph.log` / `game.log` | daemon | console output of that process (`flowgraph.log` only in direct mode) | small |
| `flowgraph.json` | logger | – (`t0`, node, `CDP_*` environment, settings of every block) | 2 kB |
| `csma_txop.csv` | logger | **channel access** (one burst sent, or one Block ACK sent) | ~25 kB/min under load |
| `csma_1s.csv` | logger | second while the MAC is active (every 10 s when idle) | ~6 kB/min |
| `arq_tx.csv` | logger | Block ACK result or timeout at the sender | small |
| `arq_rx.csv` | logger | reliable frame received | ~1 kB per 35 frames |
| `udp.csv` | logger | datagram sent or received | ~25 kB/min at 10 datagrams/s each way |
| `msgs.csv` | logger | reliable message (chat item / file) completed, sent or received | tiny |
| `phy_1s.csv` | logger | second while frames arrive (every 10 s when idle) | ~2 kB/min |
| `events.log` | logger | rare event (drops, late unmute, client connect, errors); at most 5 identical per second | tiny |

**Time base**
- In the flowgraph files, `t` = seconds since `flowgraph.json:t0`.
- In `chat.csv` and `daemon.log`, `t` = seconds since `session.json:t0`.
- Absolute (epoch) time = `t0 + t`.
- The two PCs' clocks are not synchronised. `cdp_logsum.py` estimates the offset from the datagram one-way delays in both directions; `owd_*` values are raw (sender clock vs receiver clock).

**Lists of sequence numbers** are written as ranges: `12-15 18` = 12, 13, 14, 15, 18. Empty cell = not applicable.

---

## 2. Columns

### csma_txop.csv – one row per channel access (the main file for "why was it slow")

| column | meaning |
|---|---|
| `t` | time the burst was released (backoff finished) |
| `kind` | `R` reliable data burst (needs a Block ACK) · `U` datagram burst (unicast, no ACK) · `B` broadcast · `A` a Block ACK this node sent |
| `dst` | destination node |
| `bsn` | burst number. The peer's `arq_rx.csv` rows and the Block ACK carry the same `bsn`. |
| `n` | frames in the burst (≤ M) |
| `bytes` | payload bytes in the burst (bridge header included, MAC header excluded) |
| `retx` | frames in the burst that are retransmissions |
| `q` | frames waiting in this MAC (all queues) at release |
| `wait_ms` | **queueing delay**: age of the oldest frame in the burst at release. For `A`: estimated end of the received burst → Block ACK released. |
| `att_ms` | **contention time** for this access: start of contention → release |
| `p_ms`, `p_draws` | part of `att_ms` spent in p-persistence, and the number of draws it took |
| `bo`, `cw` | backoff slots drawn, contention window used (15, 31, … after failures) |
| `defer` | times the backoff was frozen because the medium turned busy |
| `busy_ms`, `nav_ms` | time the medium was busy (carrier sense or NAV) during the contention, and how much of it was NAV |
| `unmute_ms` | wait for the TX attenuation write (TX mute) before sending |
| `air_ms` | airtime of the burst |
| `off_ms` | this node's receiver was off for this long (release → RX on: unmute + echo delay + air + guard + mute write) |
| `echo_ms` | time until the own burst showed up in the own receiver (TX chain latency) |
| `res` | `ba` Block ACK received · `timeout` no Block ACK (the whole burst counts as failed, CW doubles) · `-` no ACK expected |
| `acked`, `bitmap` | frames acknowledged, and the bitmap (`1` = received; position i = i-th frame of the burst) |
| `rtt_ms` | end of the burst on air → Block ACK decoded |
| `sns` | sequence numbers of the burst's frames |

Time budget of one reliable burst ≈ `wait_ms` (queue) → `att_ms` (contention, which includes `p_ms` and `busy_ms`) → `unmute_ms` → `air_ms` → `rtt_ms`. Then the next burst's contention starts.

### csma_1s.csv – MAC state per second

| column | meaning |
|---|---|
| `busy_pct`, `nav_pct`, `tx_pct` | share of the second the medium was sensed busy, blocked by NAV, or used by this node's own transmissions |
| `floor_db`, `thr_db` | carrier-sense noise floor and busy threshold |
| `queue`, `udp_q`, `ba_due` | frames waiting (all queues), datagrams waiting, Block ACKs this node still owes |
| `cw`, `k`, `phase`, `awaiting_ba` | current contention window, backoff stage, contention phase (`persist` / `difs` / `backoff` / `idle`), waiting for a Block ACK (1/0) |
| `rate_pct` | **samples the radio delivered, as % of `samp_rate`**. Below 99 means the PC was not keeping up and samples were lost. |
| `echo_ms`, `mute`, `mute_wr_max_ms` | median TX latency; TX mute state; slowest attenuation write in that second |
| `deferrals`, `busy_on`, `nav_set`, `tx_D`, `tx_BA`, `ba_rx`, `ba_timeout`, `retry_drop`, `rx_data`, `rx_dup` | counts in that second |
| `windows` | sender window per destination as `dst:WS/used/backlog`. `used` = sequence numbers in flight of N; `backlog` = frames waiting for window space. |
| `proc_cpu`, `load1` | CPU of the whole flowgraph process (100 = one full core); system 1-minute load average |

### arq_tx.csv – sender side of selective repeat

| column | meaning |
|---|---|
| `what` | `ba` Block ACK processed · `noba` timeout (all frames of the burst sent again) · `late_ba` Block ACK that came after the timeout · `cum` stray Block ACK used only for its cumulative part |
| `dst`, `bsn` | destination, burst answered |
| `acked`, `nacked`, `dropped` | sequence numbers acknowledged, to be sent again, given up after the retry limit |
| `ws0`, `ws` | window start before / after: how far the window moved |
| `used`, `backlog` | sequence numbers in the window after the update (of N), frames waiting behind the window |

### arq_rx.csv – receiver side, one row per reliable frame

| column | meaning |
|---|---|
| `src`, `sn`, `bsn`, `idx`, `cnt` | sender, sequence number, burst number, position in the burst / burst size |
| `dup` | already received (sent again because a Block ACK was lost) |
| `deliv` | frames delivered in order because this one arrived (> 1 = it filled a gap) |
| `buf` | frames held in the reorder buffer after it (waiting for a missing earlier frame) |
| `gaveup` | missing frames skipped because the sender gave up on them |
| `rws` | receiver window start after it |
| `retry` | 1 if the frame was marked as a retransmission |
| `fec` | bits fixed by the Hamming FEC |

### udp.csv – datagrams (the game's traffic)

| column | meaning |
|---|---|
| `dir` | `tx` (handed to the radio) or `rx` (received from the radio) |
| `peer` | destination (tx) / source (rx) |
| `useq` | datagram sequence number of the sender (16 bit) |
| `bytes` | application bytes |
| `owd_ms` | rx only: receive time minus the **sender's send timestamp** (carried in every datagram). Raw: it includes the clock offset between the PCs. |
| `iat_ms` | rx only: time since the previous datagram from that peer |
| `gap` | rx only: `useq` jump minus 1. 0 = in sequence; > 0 = that many missing; < 0 = arrived after a later one. |
| `mac_sn` | rx only: MAC sequence number of the frame |

A tx row means the datagram reached the bridge. The time it then waited in the MAC is the `wait_ms` of the `U` row in `csma_txop.csv` that carried it (same time window). Datagrams dropped because the MAC's datagram queue was full show up in `events.log` as `udp_drop`.

### msgs.csv – reliable messages at the socket bridge (chat items, files)

| column | meaning |
|---|---|
| `dir`, `peer` | `tx` / `rx`, other node |
| `mid` | message id in the over-the-air segment headers (the same on both PCs) |
| `ref` | the application's reference (tx; equals `ref` in `chat.csv`) |
| `bytes`, `segs` | message size, number of segments (`mss` bytes each, one radio frame per segment) |
| `got` | segments acknowledged (tx) / received (rx) |
| `frames_tx` | tx: radio transmissions used for its segments, retransmissions included. `frames_tx / segs` = overhead. |
| `resent` | tx (v6.9.1): segments the MAC gave up on (retry limit / queue full) that the bridge handed to the MAC again. `> 0` with `res=sent` = the end-to-end retransmission saved the message. |
| `dups` | rx (v6.9.1): segments that arrived twice (the sender resent a segment whose Block ACK was lost) |
| `wait_s` | tx: submit → first segment handed to the MAC (time spent queued behind other messages at the bridge) |
| `dur_s` | tx: submit → SENT/FAIL. rx: first → last segment received. |
| `kBps` | `bytes / dur_s / 1000` |
| `owd_first_ms`, `owd_max_ms` | rx: one-way delay of the first segment / the worst segment (raw clocks) |
| `res` | `sent`, `fail:<reason>`, `ok`, `incomplete (no segment for 600 s)`. v6.9.1: a tx message fails only when nothing reached the peer for `CDP_MSG_STALL` s (default 60); `fail:no ACK` then means the link was down that long. |

### phy_1s.csv – receiver chain per second

`corr_tags`, `corr_best` (training-word correlator hits, best correlation), `train`, `train_ok` (train words checked / clean), `locks` (sync words found), `crc_fail`, `bad_len`, `ok` (CRC good), `other` / `own` (good frames for other nodes / own echo), `sq_eob` (squelch closures), `noise_db`, `squelch_db` (noise floor, squelch threshold).

### chat.csv – chat items (daemon)

| column | meaning |
|---|---|
| `dir`, `peer`, `id`, `kind`, `name` | `tx` / `rx`, other node, chat message id (the same on both PCs), `text` (typed) / `image` / `doc` (text document) / `file`, file name |
| `bytes` | bytes on the link (chat header included) |
| `ref` | bridge reference: joins the `msgs.csv` row |
| `dur_s` | tx: queued → delivered (SENT) |
| `kBps` | throughput |
| `age_s` | rx: receive time minus the sender's timestamp (includes the clock offset) |
| `res` | `queued` / `sent` / `fail:<reason>` / `received` |

### events.log

```
<t> <block> <kind> key=value ...
```

The kinds are:
- `udp_drop`: datagram queue full, oldest datagram dropped; `age_ms` = how long it had waited
- `drop`: MAC queue full
- `retry_drop`: frame given up after the retry limit
- `seg_resend` (bridge, v6.9.1): a segment the MAC gave up on was handed to the MAC again (`why` = dropped / qfull; logged for the first 3 and every 100th per message)
- `cancel` (csma, v6.9.1): a failed message's frames still queued in the MAC were removed (`frames` = how many)
- `late_unmute`: TX attenuation write too slow
- `ba_stray`, `late Block ACK`
- `abort`: framer did not answer
- `transitions_suppressed`: carrier sense flapping
- `client`: an app connected or disconnected
- `error`: socket bind problems etc.
- `flowgraph stopped`

---

## 3. How to answer the usual questions

**"Transfers are sometimes fast, sometimes slow."**
1. In `msgs.csv`, take the `tx` rows. Compare `dur_s`, `kBps`, `wait_s` and `frames_tx / segs` for fast and slow transfers.
2. Take the `csma_txop.csv` rows (both `R` and `U`) in the window `[t - dur_s, t]` of a slow transfer.
   - Large `att_ms` with large `p_ms` → p-persistence dominates.
   - Large `busy_ms` → the other PC was transmitting.
   - Many `res=timeout` → Block ACKs or bursts lost; `cw` grows.
   - Large `wait_ms` while `att_ms` is small → the queue was long, often datagrams competing.
3. In `arq_tx.csv`, check `used == N` with `backlog > 0`: the window was full and waiting for an early frame.
4. In `phy_1s.csv` for the same seconds: `crc_fail`, `locks` vs `ok`.
5. In `csma_1s.csv`: `rate_pct < 99` or `proc_cpu ≥ 90` → the PC was the bottleneck.

**"Datagrams get stuck, then every following one is late."**
1. In `udp.csv` rx rows: find where `owd_ms` jumps and stays high. Check that `gap` stays 0 (no loss) and `iat_ms` bunches (bursts).
2. On the **sender's** PC for those seconds:
   - `csma_txop.csv` `U` rows: `wait_ms` growing, with `n` = M every time, means the queue is building up.
   - `csma_1s.csv` `udp_q` rising.
   - `events.log` `udp_drop` (`age_ms` = how stale the dropped datagrams were).
3. Compare with `R` rows in the same window (a file transfer competing) and with `busy_pct` (the other PC talking).

**"Is it the radio or the MAC?"** Lost bursts at the receiver are explained by:
- `phy_1s.csv` (no `locks` → nothing decodable arrived; `crc_fail` → arrived broken)
- the sender's `off_ms` vs the receiver's own transmissions (`csma_txop.csv` of the other PC: overlapping times = collision)

**Joining rows:**
- `chat.csv ref` ↔ `msgs.csv ref`
- `msgs.csv mid` is the same on both PCs
- `csma_txop.csv bsn` ↔ the receiver's `arq_rx.csv bsn`
- `udp.csv useq` (tx on one PC ↔ rx on the other)
