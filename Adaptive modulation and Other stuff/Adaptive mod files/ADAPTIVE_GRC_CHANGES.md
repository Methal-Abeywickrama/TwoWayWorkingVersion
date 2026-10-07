# Adaptive BPSK/QPSK — GRC changes (start from the working `cdp_transeciever_Mid Evaluation.grc`)

Do all of this on both nodes. Node B keeps its usual swaps (TX/RX frequencies, `MY_ID = 1`, framer `peer_id = 0`).

## 1. Replace the two embedded Python blocks
- **epy_block_0_0** (framer): open → *Open in Editor* → paste `transeciever_epy_block_0_0.py` → save.
  Parameters: `peer_id 1`, `preamble_len 1024`, `repeat_count 50`, `max_payload_len 8192`, `preamble_byte 0xFF`, `mcs_mode MCS_MODE`, `feedback_timeout 30`.
- **epy_block_0_0_0** (deframer): paste `transeciever_epy_block_0_0_0.py`. Its input becomes **complex**.
  Parameters: `peer_id MY_ID`, `max_bit_errors 1`, `max_payload_len 8192`, `sym_rate samp_rate/sps`, `snr_up_db 16`, `snr_down_db 13`, `snr_avg 0.2`.

## 2. TX chain
1. **pdu_pdu_to_tagged_stream_0_0**: Type `Byte` → `Complex`.
2. **blocks_copy_0_0**: Type `Byte` → `Complex` (the `packet_tx` virtual sink/source follow automatically).
3. **Disable** `digital_constellation_modulator_0_0_0` (BPSK modulator). Leave the QPSK one disabled.
4. **Add** *Root Raised Cosine Filter*:
   - FIR Type: `Complex->Complex (Real Taps) (Interp)`
   - Interpolation: `sps`, Decimation: `1`, Gain: `sps`
   - Sample Rate: `samp_rate`, Symbol Rate: `samp_rate/float(sps)`, Alpha: `alpha`, Num Taps: `11*sps+1`
5. Wire `blocks_copy_0_0 → RRC filter → blocks_tag_gate_0_0` (tag gate → Pluto sink stays as it is).

## 3. RX chain
1. **digital_costas_loop_cc_0**: Order `BPSK_CONST.arity()` → `4`.
2. **Disable**: `digital_constellation_decoder_cb_0_0`, `digital_diff_decoder_bb_0_0`, `blocks_unpack_k_bits_bb_0_0`, `virtual_sink_2_0` (decoded_stream), `virtual_source_2_0`.
3. Connect **virtual_source_2** (`decoded_constallation`, i.e. the Costas output) → **epy_block_0_0_0** input.
4. Leave AGC, FLL, RRC, symbol sync and the CMA linear equalizer unchanged. (CMA only uses the modulus, so it is fine for both modes.)

## 4. Message connection (the feedback path)
- **epy_block_0_0_0 `link_out` → epy_block_0_0 `link_in`**.
  `pdu_out` / `text_out` on the deframer are unchanged for your ARQ layer (PDU metadata now also carries `mcs` and `snr_db`).

## 5. Mode selector (optional but handy for the demo)
Add *QT GUI Chooser*: ID `MCS_MODE`, Type `Integer`, Default `-1`, Options `[-1, 0, 1]`, Labels `["Auto", "BPSK", "QPSK"]`.
GRC generates the callback, so switching it changes the next message's modulation live.

## What you will see
- TX console: `[TX] msg #n ... QPSK (peer request) ... asking peer for BPSK/QPSK, our RX SNR x dB`
- RX console: `[RX] msg #n: 48/50 bursts OK (Loss: 4%) [QPSK, SNR 18.3 dB]` and `[LINK] ...` when the request changes.
- Constellation sinks now show the 4 diagonal points (BPSK frames use two opposite ones).

## Notes
- Auto mode starts at BPSK and only goes to QPSK after the **peer** has received from you and sent something back
  (its request rides in every header it transmits). One message each way is enough; your ARQ ACKs will keep it fresh.
- Thresholds come from the simulation table (`python3 sim_adaptive_link.py`): QPSK is clean from about 14 dB Es/N0,
  BPSK from about 10 dB. Tune `snr_up_db` / `snr_down_db` after watching the SNR numbers on real hardware.
- With a 1024-byte preamble (8192 symbols), QPSK saves very little airtime on a 50-byte message; the gain shows up with
  larger payloads or once the preamble is shortened.
