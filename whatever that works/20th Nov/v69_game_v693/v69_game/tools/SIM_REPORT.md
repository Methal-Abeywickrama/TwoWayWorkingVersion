# Radio Arena worst-case simulation (180 s per run, seed 1)

OLD = earlier peer-to-peer game, FIFO MAC queue. NEW = host-authoritative game with rewind, newest-only game frames.

## normal: 4 players, 5 packets/s, ARQ on, 2 % loss

| | OLD | NEW |
|---|---|---|
| hits applied | 112 | 104 |
| ghost hits: victim really > 1.5 m from where the hit was judged | 79 (71 %) | 43 (41 %) |
| distance between judged and true victim position, p95 | 18.12 m | 3.72 m |
| hits applied > 1 s after the shot (max) | 112 (122.3 s) | 0 (0.4 s) |
| deaths / killed by a hit fired > 1 s earlier / killed by a ghost hit | 24 / 24 / 18 | 18 / 0 / 7 |
| hits on the shooter's screen that counted (NEW: not counting shots at players already dead on the host) | 112 of 477 (23 %) | 97 of 250 (39 %) |
| shots rejected (reason count) | dead 83 | dead 33, late 86, reloading 2 |
| view age the host judged shots at, p50 / p95 | - / - | 795 ms / 1619 ms |
| MAC queue max / payload age when sent p50 / p95 / max | 256 / 39033 ms / 95285 ms / 122.6 s | 5 / 110 ms / 382 ms / 1.9 s |
| MAC queue-full drops / newest-only replacements | 3264 / 0 | 0 / 1901 |
| frames on air DATA / Block ACK, collisions, channel airtime | 1623 / 664, 879, 72 s | 2515 / 820, 1141, 32 s |
| NEW: roles at the end, migrations, client corrections, shots at lagging players | - | {'host': 1, 'client': 3}, 0, 9, 13 |

## normal_bypass: 4 players, 5 packets/s, ARQ bypassed (CDP_GAME_ARQ=0), 2 % loss

| | OLD | NEW |
|---|---|---|
| hits applied | 218 | 138 |
| ghost hits: victim really > 1.5 m from where the hit was judged | 47 (22 %) | 46 (33 %) |
| distance between judged and true victim position, p95 | 2.55 m | 3.22 m |
| hits applied > 1 s after the shot (max) | 45 (4.7 s) | 0 (0.5 s) |
| deaths / killed by a hit fired > 1 s earlier / killed by a ghost hit | 45 / 16 / 19 | 27 / 0 / 12 |
| hits on the shooter's screen that counted (NEW: not counting shots at players already dead on the host) | 218 of 354 (62 %) | 128 of 244 (52 %) |
| shots rejected (reason count) | dead 133 | dead 29, late 105, reloading 4 |
| view age the host judged shots at, p50 / p95 | - / - | 734 ms / 1730 ms |
| MAC queue max / payload age when sent p50 / p95 / max | 4 / 124 ms / 301 ms / 0.7 s | 2 / 97 ms / 189 ms / 0.5 s |
| MAC queue-full drops / newest-only replacements | 0 / 0 | 0 / 666 |
| frames on air DATA / Block ACK, collisions, channel airtime | 3549 / 0, 1816, 43 s | 3395 / 0, 1636, 41 s |
| NEW: roles at the end, migrations, client corrections, shots at lagging players | - | {'host': 1, 'client': 3}, 0, 3, 7 |

## overload: 4 players at 10 packets/s, ARQ on: far more than the channel carries

| | OLD | NEW |
|---|---|---|
| hits applied | 84 | 97 |
| ghost hits: victim really > 1.5 m from where the hit was judged | 37 (44 %) | 22 (23 %) |
| distance between judged and true victim position, p95 | 18.40 m | 2.68 m |
| hits applied > 1 s after the shot (max) | 84 (132.8 s) | 0 (0.4 s) |
| deaths / killed by a hit fired > 1 s earlier / killed by a ghost hit | 16 / 16 / 9 | 18 / 0 / 8 |
| hits on the shooter's screen that counted (NEW: not counting shots at players already dead on the host) | 84 of 415 (20 %) | 93 of 216 (43 %) |
| shots rejected (reason count) | dead 48 | dead 5, late 29, reloading 4 |
| view age the host judged shots at, p50 / p95 | - / - | 677 ms / 1152 ms |
| MAC queue max / payload age when sent p50 / p95 / max | 256 / 46418 ms / 96415 ms / 140.5 s | 4 / 56 ms / 292 ms / 1.9 s |
| MAC queue-full drops / newest-only replacements | 10208 / 0 | 0 / 4574 |
| frames on air DATA / Block ACK, collisions, channel airtime | 1598 / 704, 853, 74 s | 2684 / 787, 1239, 33 s |
| NEW: roles at the end, migrations, client corrections, shots at lagging players | - | {'host': 1, 'client': 3}, 0, 9, 7 |

## stall: 4 players, 5/s, ARQ on; node 1 cannot get the channel for 3 s every 15 s (its frames queue up), node 2 hears nothing for 2 s every 20 s

| | OLD | NEW |
|---|---|---|
| hits applied | 110 | 111 |
| ghost hits: victim really > 1.5 m from where the hit was judged | 48 (44 %) | 35 (32 %) |
| distance between judged and true victim position, p95 | 15.01 m | 4.18 m |
| hits applied > 1 s after the shot (max) | 110 (108.9 s) | 0 (0.5 s) |
| deaths / killed by a hit fired > 1 s earlier / killed by a ghost hit | 21 / 21 / 9 | 19 / 0 / 9 |
| hits on the shooter's screen that counted (NEW: not counting shots at players already dead on the host) | 110 of 442 (25 %) | 107 of 278 (38 %) |
| shots rejected (reason count) | dead 61 | dead 15, late 84 |
| view age the host judged shots at, p50 / p95 | - / - | 766 ms / 1667 ms |
| MAC queue max / payload age when sent p50 / p95 / max | 256 / 38958 ms / 100312 ms / 132.1 s | 5 / 105 ms / 364 ms / 1.9 s |
| MAC queue-full drops / newest-only replacements | 3414 / 0 | 0 / 2003 |
| frames on air DATA / Block ACK, collisions, channel airtime | 1570 / 644, 826, 70 s | 2389 / 809, 1000, 30 s |
| NEW: roles at the end, migrations, client corrections, shots at lagging players | - | {'host': 1, 'client': 3}, 1, 10, 16 |

## worst: 4 players, 5/s, ARQ on, 10 % loss, 2 s interference every 15 s, node 1 stalls 3 s every 20 s, node 3 deaf 2 s every 25 s

| | OLD | NEW |
|---|---|---|
| hits applied | 143 | 88 |
| ghost hits: victim really > 1.5 m from where the hit was judged | 69 (48 %) | 29 (33 %) |
| distance between judged and true victim position, p95 | 15.97 m | 4.30 m |
| hits applied > 1 s after the shot (max) | 143 (136.5 s) | 0 (0.4 s) |
| deaths / killed by a hit fired > 1 s earlier / killed by a ghost hit | 27 / 27 / 13 | 12 / 0 / 4 |
| hits on the shooter's screen that counted (NEW: not counting shots at players already dead on the host) | 143 of 447 (32 %) | 82 of 214 (38 %) |
| shots rejected (reason count) | dead 83 | dead 4, late 78 |
| view age the host judged shots at, p50 / p95 | - / - | 799 ms / 1880 ms |
| MAC queue max / payload age when sent p50 / p95 / max | 256 / 41687 ms / 111135 ms / 138.4 s | 6 / 107 ms / 397 ms / 2.0 s |
| MAC queue-full drops / newest-only replacements | 3143 / 0 | 0 / 2207 |
| frames on air DATA / Block ACK, collisions, channel airtime | 1339 / 599, 657, 60 s | 2020 / 599, 813, 26 s |
| NEW: roles at the end, migrations, client corrections, shots at lagging players | - | {'host': 1, 'client': 3}, 3, 15, 0 |

## worst_bypass: the same worst case with the ARQ bypassed

| | OLD | NEW |
|---|---|---|
| hits applied | 197 | 125 |
| ghost hits: victim really > 1.5 m from where the hit was judged | 94 (48 %) | 55 (44 %) |
| distance between judged and true victim position, p95 | 20.69 m | 3.93 m |
| hits applied > 1 s after the shot (max) | 129 (11.5 s) | 0 (0.5 s) |
| deaths / killed by a hit fired > 1 s earlier / killed by a ghost hit | 39 / 30 / 21 | 24 / 0 / 12 |
| hits on the shooter's screen that counted (NEW: not counting shots at players already dead on the host) | 197 of 391 (50 %) | 114 of 297 (38 %) |
| shots rejected (reason count) | dead 154 | dead 24, late 130 |
| view age the host judged shots at, p50 / p95 | - / - | 783 ms / 1965 ms |
| MAC queue max / payload age when sent p50 / p95 / max | 55 / 805 ms / 6857 ms / 11.0 s | 3 / 97 ms / 197 ms / 2.0 s |
| MAC queue-full drops / newest-only replacements | 0 / 0 | 0 / 1152 |
| frames on air DATA / Block ACK, collisions, channel airtime | 3492 / 0, 2062, 42 s | 2861 / 0, 1368, 34 s |
| NEW: roles at the end, migrations, client corrections, shots at lagging players | - | {'host': 1, 'client': 3}, 5, 10, 7 |

## host_dies: 4 players, 5/s, ARQ on, 5 % loss; node 0 (the host in NEW) is switched off at 60 s

| | OLD | NEW |
|---|---|---|
| hits applied | 94 | 83 |
| ghost hits: victim really > 1.5 m from where the hit was judged | 58 (62 %) | 31 (37 %) |
| distance between judged and true victim position, p95 | 16.59 m | 2.62 m |
| hits applied > 1 s after the shot (max) | 94 (82.1 s) | 0 (0.5 s) |
| deaths / killed by a hit fired > 1 s earlier / killed by a ghost hit | 18 / 18 / 10 | 17 / 0 / 9 |
| hits on the shooter's screen that counted (NEW: not counting shots at players already dead on the host) | 94 of 316 (30 %) | 76 of 152 (50 %) |
| shots rejected (reason count) | dead 64 | dead 10, late 46, reloading 1 |
| view age the host judged shots at, p50 / p95 | - / - | 752 ms / 1449 ms |
| MAC queue max / payload age when sent p50 / p95 / max | 256 / 34090 ms / 112964 ms / 147.5 s | 6 / 109 ms / 388 ms / 2.0 s |
| MAC queue-full drops / newest-only replacements | 1915 / 0 | 0 / 1335 |
| frames on air DATA / Block ACK, collisions, channel airtime | 1433 / 567, 655, 65 s | 2188 / 762, 778, 28 s |
| NEW: roles at the end, migrations, client corrections, shots at lagging players | - | {'host': 1, 'client': 2}, 3, 11, 3 |

## two_worst: 2 players (the two PCs), 5/s, ARQ on, 10 % loss, 2 s interference every 15 s, node 1 stalls 3 s every 20 s

| | OLD | NEW |
|---|---|---|
| hits applied | 67 | 36 |
| ghost hits: victim really > 1.5 m from where the hit was judged | 32 (48 %) | 8 (22 %) |
| distance between judged and true victim position, p95 | 20.97 m | 2.11 m |
| hits applied > 1 s after the shot (max) | 47 (10.9 s) | 0 (0.4 s) |
| deaths / killed by a hit fired > 1 s earlier / killed by a ghost hit | 13 / 9 / 7 | 6 / 0 / 2 |
| hits on the shooter's screen that counted (NEW: not counting shots at players already dead on the host) | 67 of 105 (64 %) | 33 of 41 (80 %) |
| shots rejected (reason count) | dead 37 | dead 5, late 2 |
| view age the host judged shots at, p50 / p95 | - / - | 638 ms / 984 ms |
| MAC queue max / payload age when sent p50 / p95 / max | 70 / 1754 ms / 9659 ms / 12.9 s | 4 / 102 ms / 330 ms / 1.7 s |
| MAC queue-full drops / newest-only replacements | 0 / 0 | 0 / 646 |
| frames on air DATA / Block ACK, collisions, channel airtime | 1101 / 635, 320, 37 s | 1343 / 418, 216, 17 s |
| NEW: roles at the end, migrations, client corrections, shots at lagging players | - | {'host': 1, 'client': 1}, 0, 3, 0 |

## Settings sweep (NEW design, 2 % loss, 180 s per run)

| players | rate | radio | view age p50 / p95 | hits on the shooter's screen that counted | late rejects |
|---|---|---|---|---|---|
| 2 | 3/s | ARQ on | 775 / 1033 ms | 85 % | 8 |
| 2 | 3/s | bypass | 752 / 928 ms | 84 % | 2 |
| 2 | 5/s | ARQ on | 625 / 859 ms | 77 % | 2 |
| 2 | 5/s | bypass | 557 / 775 ms | 82 % | 1 |
| 3 | 3/s | ARQ on | 850 / 1702 ms | 62 % | 82 |
| 3 | 3/s | bypass | 768 / 1117 ms | 75 % | 34 |
| 3 | 5/s | ARQ on | 749 / 1652 ms | 51 % | 52 |
| 3 | 5/s | bypass | 682 / 1000 ms | 72 % | 16 |
| 4 | 3/s | ARQ on | 884 / 1984 ms | 32 % | 187 |
| 4 | 3/s | bypass | 818 / 1549 ms | 58 % | 136 |
| 4 | 5/s | ARQ on | 795 / 1619 ms | 39 % | 86 |
| 4 | 5/s | bypass | 734 / 1730 ms | 52 % | 105 |

Two players: 5 packets/s, either radio mode. Three or four players: bypass the ARQ (CDP_GAME_ARQ=0); with four
also consider a lag limit of 1.5 s. The channel itself is the limit: with 3-4 nodes about a third to half of all
frames collide in the model (20 ms TX latency against a 2 ms slot), whatever the game does.
