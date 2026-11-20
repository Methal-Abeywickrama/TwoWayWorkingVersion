# v6.9.3 - changes against v6.9 (use this folder on BOTH PCs)

## Run
    cd v69_game
    python3 cdp_daemon.py --node 1        # --node 32 on the other PC
The login screen now asks for the **modulation** (BPSK or QPSK, remembered for next time; `--mod qpsk` or
`export CDP_MOD=qpsk` pre-selects it). Both PCs must use the same - a node in the other mode is simply not heard.
Recommended as well: `export CDP_MAC_SIFS=0.012`.

The flowgraph prints at start:
    [MAC] v6.9.2: first frame of a transmission with a 256 B preamble, a duplicate after 4 retries, Block ACK with the window bitmap
    [MAC] v6.9.3: frames 2..4 of a burst with a 64 B preamble and no postamble | 32 samples per byte, preamble 128 B
(QPSK: 512 / 128 / 16 samples per byte / 256 B.)

## What changed
| | |
|---|---|
| ARQ (v6.9.2) | longer preamble on the first frame of each transmission (`CDP_MAC_PRE1`), a duplicate copy after 4 retries (`CDP_MAC_DUP_AFTER`), window Block ACK (`CDP_MAC_WBA`) |
| bursts (v6.9.3) | frames 2..M: short preamble (`CDP_MAC_PRE_IN`, 64 B; 32 also fine) and no postamble; the last frame pads the whole burst to Pluto buffers |
| p-persistence | `CDP_MAC_P` default 0.5 (was 0.2) |
| BPSK / QPSK | `CDP_MOD`; differential QPSK with the flowgraph's QPSK_CONST (Gray), Costas order 4, equalizer and correlator trained on 32 QPSK symbols (train + sync word), inverse Gray map before the deframer; preambles double in bytes so they last as long |
| dashboard | a window of the app (`cdp_dashboard.py`), no web browser; `python3 cdp_dashboard.py debug/<session>` for old logs |

Files: `cdp_link_epy_csma.py` (MAC), `cdp_link_epy_block_0_0_1.py` (framer), `v6.9_cdp_link.grc` + `cdp_link.py`
(flowgraph), `cdp_daemon.py` (login, dashboard window), `cdp_dashboard.py` (new), `cdp_metrics.py` (web server
removed), docs. `v693.patch` = the code changes as a diff.

## Tests (no radio, no GNU Radio needed)
    python3 tools/test_qpsk_path.py       # BPSK and QPSK bit path: framer -> (de)modulator maps -> correlator -> deframer
    python3 tools/test_arq_v692.py        # two MACs on a simulated channel, v6.9 vs v6.9.2 vs v6.9.3
    python3 tools/test_dashboard.py       # dashboard data;   tools/test_dashboard_qt.py  the window
Not testable here: the GNU Radio C++ blocks with QPSK (Costas loop order 4, equalizer, symbol sync) - first test
on the radios: watch "train words (clean ...)" and "CRC fail" in the [RX stats] lines.
If you edit a block's .py: `python3 tools/embed_blocks.py epy_csma epy_block_0_0_1` copies it into the .grc.
