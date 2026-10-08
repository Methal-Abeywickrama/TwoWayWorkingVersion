# Stage 2 — Adaptive modulation (BPSK ⇄ QPSK) under the SR-ARQ application

Built on Stage 1. Only the modem and framing change; the app, FSM, addressing and FDD radio are the same.

## How it works
- **One QPSK grid for both modes** (diagonal points). BPSK frames use 180° steps only, so a single **order-4 Costas loop** serves both.
- **Differential coding is done per burst inside the framer**, from a known reference symbol. Nothing depends on encoder state left over from a previous packet.
- **Preamble, sync word and an 8-byte header are always BPSK.** The header carries the payload MCS plus the MCS *this node wants the peer to use* and a CRC-8. Only payload+CRC32 switch to QPSK.
- **The receiver measures SNR on every CRC-valid frame** (data-aided: it rebuilds the sent symbols). It applies hysteresis: ask for QPSK above 16 dB, back to BPSK below 13 dB. A header-valid QPSK frame that fails CRC requests BPSK at once.
- **The request travels in the header of every frame the receiver sends back** (SYN_ACK, ACK, ...). The sender's framer obeys it. With FDD, each direction adapts independently, and the ARQ ACKs keep the feedback fresh.
- **Safety rules in the framer:**
  - Control PDUs shorter than 32 B (SYN/ACK/FIN) are always BPSK.
  - If QPSK frames get *no reply at all* for 1 s, it drops to BPSK until the peer is heard again (the peer can't report frames it never decoded).
  - With no fresh feedback it uses BPSK.

## What changed (GRC edits vs Stage 1)
- **TX:**
  - `PDU to Tagged Stream` type Byte → **Complex**; `blocks_copy_0_0` → **Complex**.
  - **Constellation Modulator (BPSK) disabled**, replaced by **Root Raised Cosine Filter** `root_raised_cosine_filter_tx`: type *Interpolating, complex→complex, real taps*, interp `sps`, gain `sps`, sample rate `samp_rate`, symbol rate `samp_rate/float(sps)`, alpha `alpha`, taps `11*sps+1`.
  - Chain: `copy → RRC TX → Tag Gate → Pluto Sink`.
- **RX:**
  - **Costas Loop order 4** (was `BPSK_CONST.arity()`).
  - **Disabled:** BPSK Constellation Decoder, Differential Decoder, Unpack K Bits, `decoded_stream` virtual sink/source.
  - The deframer now takes the Costas output directly (`decoded_constallation` virtual source → deframer). AGC, FLL, RRC, symbol sync and the CMA linear equalizer are unchanged.
- **epy_block_0_0 → Adaptive Packet Framer TX.** New params: `postamble_len 160` (constant-carrier tail, same job as your 160 zero bytes), `mcs_mode MCS_MODE`, `feedback_timeout 30`, `qpsk_min_len 32`, `fallback_s 1.0`; `repeat_count 1` (the ARQ does the retransmitting).
- **epy_block_0_0_0 → Adaptive Packet Deframer RX** (complex input). Params: `sym_rate samp_rate/sps`, `snr_up_db 16`, `snr_down_db 13`, `snr_avg 0.2`, `verbose 1`.
- **New message link:** deframer `link_out` → framer `link_in` (the feedback path).
- **New QT chooser `MCS_MODE`:** Auto / BPSK / QPSK (env `TL_MCS_MODE` = -1/0/1 sets the start value).

## How to test
**1. Offline logic test (~1 min):** `python3 offline_test/test_stage2.py`
Two nodes over a symbol-level AWGN channel with gain, phase drift and CFO, running your real SR-ARQ FSM. Scenarios:
1. 20 dB both ways: must switch to QPSK.
2. A→B drops 20 → 11 dB mid-transfer: must fall back to BPSK *and still deliver the file*.
3. 10 dB: must stay BPSK.

Expected output: `offline_test/expected_output.txt`; must end with `RESULT: PASS`.

**2. One PC, no radio:** `./run_node.sh A sim`, `./run_node.sh B sim`, `./run_app.sh A sim`, `./run_app.sh B sim`.
**3. Two laptops + ANTSDRs:** `./run_node.sh A` / `./run_app.sh A` on one, `B` on the other.

## Pass criteria
1. Send a chat message A → B, then B → A (this exchange is what starts the feedback flowing).
2. Send a file of 10–20 kB A → B. Flowgraph console on A:
   `[TX] #12 -> 2 QPSK (peer request) 218 B | ask peer: QPSK rxSNR 21.4dB`
   On B: `[RX] #12 QPSK 218 B SNR 21.3 dB`, and earlier `[LINK] avg SNR ... -> asking peer for QPSK`.
3. Raise the **`nv` slider** on B (it adds noise to what B receives).
   - B prints `[LINK] ... -> asking peer for BPSK` (or `QPSK frame failed CRC -> asking peer for BPSK`), and A's next frames say `BPSK (peer request)`. The transfer still completes (`delivered`).
   - Rough guide with no other noise: nv ≲ 0.3 → QPSK, nv ≳ 0.45 → BPSK, and above ~0.6 the link starts to fail.
   - On hardware the radio's own noise adds to this, so read the printed SNR rather than nv.
4. Lower `nv` again: after a few frames B asks for QPSK again.
5. Chooser *BPSK* or *QPSK* forces the mode (`(forced)` in the TX log). Short control frames follow the forced mode too.

Constellation sinks now show four diagonal points (BPSK frames use two opposite ones).

## Notes
- With a 1024-byte preamble (8192 symbols), a 200-byte QPSK packet saves only about 2 ms of a roughly 30 ms burst. Shorten the preamble (`preamble_len`) once the link is stable to see the throughput gain.
- Thresholds 16/13 dB come from the offline frame-success curves (QPSK clean from ~14 dB, BPSK from ~10 dB Es/N0). Retune after looking at the SNR your hardware prints.
