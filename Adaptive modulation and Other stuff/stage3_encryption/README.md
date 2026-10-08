# Stage 3 — Encryption (your modular-exponentiation cipher) on top of Stage 2

Built on Stage 2. The radio, modem, adaptive link and SR-ARQ are unchanged; the encrypt/decrypt blocks
from your encryption flowgraph are inserted at the application layer.

## Where encryption sits
```
ZMQ PULL (daemon) → [Encrypt] → FSM app_in ... radio ... FSM app_out → [Decrypt] → ZMQ PUSH (daemon)
                                                                        └→ [Printer]
```
- **End-to-end:** each file is encrypted once before the ARQ splits it into packets, and decrypted once after reassembly. Retransmissions, ACKs and the transport header are unaffected.
- **The first 8 bytes stay clear.** They are the app header `[dst_addr][dst_port][media][res][len:4]` that the FSM needs to route and size the transfer. The file name and file bytes are encrypted.
- FSM events (`tx_done` / `tx_failed` / `tx_busy`) pass through Decrypt untouched, so the daemon's delivery reports still work.

## What was wrong in the encryption flowgraph
| # | Problem | Effect |
|---|---------|--------|
| 1 | `PUBLIC_KEY_PEER = 0`, so `x ** 0 % 257 = 1` | Every byte became `0x01`; the message was destroyed (reproduced in the offline test, scenario 4) |
| 2 | No key check | Even keys have no inverse mod 256, so the receiver can never decrypt |
| 3 | Encrypting a whole PDU would hide the header the FSM must read | Not usable with the ARQ; fixed by the 8-byte clear header |
| 4 | Event messages would be "decrypted" too | Fixed: passed through |

Your math itself is correct for odd keys. With prime 257, `x → x^e mod 257` is a permutation of 0–255 (I checked all 256 values for 17/241 and 3/171), so no `uint8` wrap-around occurs. The blocks now use a 256-entry lookup table instead of `np.vectorize`.

## What changed (GRC edits vs Stage 2)
- **Variables** (env-backed, so the same file runs on both nodes):
  - `PRIME` 257
  - `PRIVATE_KEY` = `TL_PRIVKEY` (default 241)
  - `PUBLIC_KEY_PEER` = `TL_PEER_PUBKEY` (default 17)
  - `PUBLIC_KEY_MINE` = `TL_PUBKEY` (default 17, for reference)
- **epy_block_3 Encrypt Payload** (`prime PRIME`, `key PUBLIC_KEY_PEER`, `skip_bytes 8`): ZMQ PULL `out` → `pdus_in`; `pdus_out` → FSM `app_in` (replaces ZMQ → FSM).
- **epy_block_4 Decrypt Payload** (`prime PRIME`, `private_key PRIVATE_KEY`, `skip_bytes 8`): FSM `app_out` → `pdus_in`; `pdus_out` → ZMQ PUSH `in` (replaces FSM → ZMQ).
- **epy_block_5 PDU Payload Printer:** `pdus_out` → `pdu_in`. Prints file name and the first 120 characters; FSM events are shown as one line.

## Keys used by `run_node.sh`
| Node | private (`TL_PRIVKEY`) | public (`TL_PUBKEY`) | encrypts with peer's public (`TL_PEER_PUBKEY`) |
|------|-----|----|----|
| A (addr 1) | 241 | 17 | 3  |
| B (addr 2) | 171 | 3  | 17 |

A key pair needs an odd public key `e` and `d = e⁻¹ mod 256` (`pow(e, -1, 256)` in Python 3.8+).

**Security note:** this cipher is fine for demonstrating the idea, but it is not secure.
- With `prime` and the public key known, anyone computes the private key with one line of Python, so this isn't a public-key scheme.
- It is a fixed byte substitution: equal bytes give equal ciphertext, so frequency analysis breaks text quickly.

Treat it as a demonstration; for real confidentiality you would need an authenticated cipher (e.g. AES-GCM) with a proper key exchange.

## How to test
**1. Offline logic test (~1–2 min):** `python3 offline_test/test_stage3.py`
1. Two key pairs: both directions decrypt correctly, and the plaintext is not visible in the bytes handed to the radio.
2. Default keys: works.
3. B has the wrong private key: A→B arrives garbled, B→A is still fine.
4. Old `key = 0`: everything garbled, with a warning.

Expected output: `offline_test/expected_output.txt`; must end with `RESULT: PASS`.

**2. One PC, no radio / 3. two laptops:** same commands as Stage 2 (`run_node.sh` sets the keys above).

## Pass criteria
- Files and chat messages arrive identical in both directions. The receiving flowgraph prints
  `[DECRYPTED PAYLOAD RECEIVED] from addr 1: 'notes.txt' (... bytes)` with readable content.
- `blocks_message_debug_1` (TX side, after the header serializer) shows the DATA payload bytes scrambled. Note that `blocks_message_debug_0` still prints the *plaintext* coming from the daemon, before encryption.
- Wrong-key check: restart node B with `TL_PRIVKEY=241 ./run_node.sh B`.
  - A file from A arrives in `node_B/inbox/` with scrambled content, under a scrambled file name (or as `rx_<date>.bin` when the name can't be parsed).
  - The sender still reports `delivered`, because the transport worked; only the content is unreadable.
  - B → A still works.
- Everything from Stage 2 (adaptive switching, `nv` slider) behaves the same.
