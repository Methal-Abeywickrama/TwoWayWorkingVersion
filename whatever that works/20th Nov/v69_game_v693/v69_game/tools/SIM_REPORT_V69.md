# Radio Arena on the v6.9 stack (120 s per run, seed 1)

Both columns: the host-authoritative game on the unchanged v6.9 MAC (FIFO datagram queue of 32 per destination, oldest dropped, p 0.2, CWmax 63, 11 B bridge header; ARQ on = inputs as reliable messages). Fixed rate = the menu rate always; adaptive rate = the game's congestion control (rate x0.7 when the median queueing delay over 1.5 s > 0.25 s, +0.5/s per second otherwise).

## normal: 4 players, 5 packets/s, ARQ on, 2 % loss

| | v6.9, fixed rate | v6.9, adaptive rate |
|---|---|---|
| hits applied | 36 | 70 |
| ghost hits: victim really > 1.5 m from where the hit was judged | 35 (97 %) | 33 (47 %) |
| distance between judged and true victim position, p95 | 29.15 m | 29.15 m |
| hits applied > 1 s after the shot (max) | 0 (0.0 s) | 0 (0.4 s) |
| deaths / killed by a hit fired > 1 s earlier / killed by a ghost hit | 7 / 0 / 7 | 15 / 0 / 8 |
| hits on the shooter's screen that counted (NEW: not counting shots at players already dead on the host) | 35 of 283 (12 %) | 64 of 252 (25 %) |
| shots rejected (reason count) | late 372 | dead 6, late 671 |
| view age the host judged shots at, p50 / p95 | 30717 ms / 54547 ms | 1863 ms / 4402 ms |
| MAC queue max / payload age when sent p50 / p95 / max | 256 / 24116 ms / 54992 ms / 64.9 s | 23 / 591 ms / 4345 ms / 8.6 s |
| MAC queue-full drops / newest-only replacements | 383 / 0 | 0 / 0 |
| frames on air DATA / Block ACK, collisions, channel airtime | 1599 / 235, 1176, 66 s | 1384 / 436, 795, 39 s |
| NEW: roles at the end, migrations, client corrections, shots at lagging players | {'host': 1, 'client': 3}, 0, 13, 1 | {'client': 3, 'host': 1}, 28, 0, 0 |
| v6.9: datagrams dropped from full queues / rate cuts / rates at the end | 0 / 0 / [5.0, 5.0, 5.0, 5.0] | 0 / 195 / [1.0, 1.8, 2.0, 5.0] |

## normal_bypass: 4 players, 5 packets/s, ARQ bypassed (CDP_GAME_ARQ=0), 2 % loss

| | v6.9, fixed rate | v6.9, adaptive rate |
|---|---|---|
| hits applied | 127 | 114 |
| ghost hits: victim really > 1.5 m from where the hit was judged | 69 (54 %) | 53 (46 %) |
| distance between judged and true victim position, p95 | 5.99 m | 5.21 m |
| hits applied > 1 s after the shot (max) | 0 (0.4 s) | 0 (0.4 s) |
| deaths / killed by a hit fired > 1 s earlier / killed by a ghost hit | 26 / 0 / 16 | 25 / 0 / 16 |
| hits on the shooter's screen that counted (NEW: not counting shots at players already dead on the host) | 119 of 309 (39 %) | 109 of 317 (34 %) |
| shots rejected (reason count) | dead 28, late 209 | dead 36, late 219 |
| view age the host judged shots at, p50 / p95 | 896 ms / 2099 ms | 851 ms / 2113 ms |
| MAC queue max / payload age when sent p50 / p95 / max | 3 / 79 ms / 157 ms / 0.3 s | 5 / 78 ms / 153 ms / 0.3 s |
| MAC queue-full drops / newest-only replacements | 0 / 0 | 0 / 0 |
| frames on air DATA / Block ACK, collisions, channel airtime | 2668 / 0, 1855, 41 s | 2481 / 0, 1639, 38 s |
| NEW: roles at the end, migrations, client corrections, shots at lagging players | {'host': 1, 'client': 3}, 6, 4, 20 | {'host': 1, 'client': 3}, 9, 3, 19 |
| v6.9: datagrams dropped from full queues / rate cuts / rates at the end | 0 / 0 / [5.0, 5.0, 5.0, 5.0] | 0 / 47 / [5.0, 5.0, 2.4, 5.0] |

## overload: 4 players at 10 packets/s, ARQ on: far more than the channel carries

| | v6.9, fixed rate | v6.9, adaptive rate |
|---|---|---|
| hits applied | 16 | 62 |
| ghost hits: victim really > 1.5 m from where the hit was judged | 16 (100 %) | 42 (68 %) |
| distance between judged and true victim position, p95 | 20.23 m | 11.98 m |
| hits applied > 1 s after the shot (max) | 0 (0.0 s) | 0 (0.5 s) |
| deaths / killed by a hit fired > 1 s earlier / killed by a ghost hit | 0 / 0 / 0 | 12 / 0 / 8 |
| hits on the shooter's screen that counted (NEW: not counting shots at players already dead on the host) | 16 of 233 (7 %) | 60 of 286 (21 %) |
| shots rejected (reason count) | late 45, unknown_view 34 | late 601 |
| view age the host judged shots at, p50 / p95 | 22259 ms / 36827 ms | 3135 ms / 17663 ms |
| MAC queue max / payload age when sent p50 / p95 / max | 256 / 30847 ms / 83338 ms / 93.2 s | 126 / 4195 ms / 22017 ms / 24.1 s |
| MAC queue-full drops / newest-only replacements | 2008 / 0 | 0 / 0 |
| frames on air DATA / Block ACK, collisions, channel airtime | 1822 / 144, 1382, 67 s | 1383 / 361, 908, 51 s |
| NEW: roles at the end, migrations, client corrections, shots at lagging players | {'host': 1, 'client': 3}, 0, 13, 2 | {'client': 3, 'host': 1}, 33, 0, 0 |
| v6.9: datagrams dropped from full queues / rate cuts / rates at the end | 0 / 0 / [10.0, 10.0, 10.0, 10.0] | 0 / 158 / [1.0, 3.0, 4.9, 1.4] |

## stall: 4 players, 5/s, ARQ on; node 1 cannot get the channel for 3 s every 15 s (its frames queue up), node 2 hears nothing for 2 s every 20 s

| | v6.9, fixed rate | v6.9, adaptive rate |
|---|---|---|
| hits applied | 51 | 77 |
| ghost hits: victim really > 1.5 m from where the hit was judged | 46 (90 %) | 40 (52 %) |
| distance between judged and true victim position, p95 | 29.15 m | 29.15 m |
| hits applied > 1 s after the shot (max) | 0 (0.0 s) | 0 (0.4 s) |
| deaths / killed by a hit fired > 1 s earlier / killed by a ghost hit | 7 / 0 / 6 | 13 / 0 / 9 |
| hits on the shooter's screen that counted (NEW: not counting shots at players already dead on the host) | 49 of 245 (20 %) | 73 of 329 (22 %) |
| shots rejected (reason count) | late 370 | dead 9, late 743, unknown_view 3 |
| view age the host judged shots at, p50 / p95 | 28751 ms / 49318 ms | 2063 ms / 5351 ms |
| MAC queue max / payload age when sent p50 / p95 / max | 256 / 23053 ms / 53879 ms / 66.0 s | 27 / 637 ms / 4151 ms / 8.6 s |
| MAC queue-full drops / newest-only replacements | 370 / 0 | 0 / 0 |
| frames on air DATA / Block ACK, collisions, channel airtime | 1562 / 238, 1096, 64 s | 1333 / 427, 775, 38 s |
| NEW: roles at the end, migrations, client corrections, shots at lagging players | {'host': 1, 'client': 3}, 3, 7, 0 | {'client': 3, 'host': 1}, 22, 3, 0 |
| v6.9: datagrams dropped from full queues / rate cuts / rates at the end | 0 / 0 / [5.0, 5.0, 5.0, 5.0] | 0 / 193 / [3.0, 1.5, 1.0, 2.2] |

## worst: 4 players, 5/s, ARQ on, 10 % loss, 2 s interference every 15 s, node 1 stalls 3 s every 20 s, node 3 deaf 2 s every 25 s

| | v6.9, fixed rate | v6.9, adaptive rate |
|---|---|---|
| hits applied | 17 | 43 |
| ghost hits: victim really > 1.5 m from where the hit was judged | 17 (100 %) | 28 (65 %) |
| distance between judged and true victim position, p95 | 10.59 m | 10.58 m |
| hits applied > 1 s after the shot (max) | 0 (0.0 s) | 0 (0.4 s) |
| deaths / killed by a hit fired > 1 s earlier / killed by a ghost hit | 4 / 0 / 4 | 6 / 0 / 6 |
| hits on the shooter's screen that counted (NEW: not counting shots at players already dead on the host) | 16 of 256 (6 %) | 39 of 268 (15 %) |
| shots rejected (reason count) | late 316 | late 679, unknown_view 1 |
| view age the host judged shots at, p50 / p95 | 38050 ms / 71617 ms | 2784 ms / 8897 ms |
| MAC queue max / payload age when sent p50 / p95 / max | 256 / 25712 ms / 65079 ms / 78.4 s | 40 / 1505 ms / 8143 ms / 14.3 s |
| MAC queue-full drops / newest-only replacements | 603 / 0 | 0 / 0 |
| frames on air DATA / Block ACK, collisions, channel airtime | 1393 / 203, 985, 57 s | 1178 / 358, 666, 38 s |
| NEW: roles at the end, migrations, client corrections, shots at lagging players | {'host': 1, 'client': 3}, 7, 4, 0 | {'client': 3, 'host': 1}, 28, 9, 0 |
| v6.9: datagrams dropped from full queues / rate cuts / rates at the end | 0 / 0 / [5.0, 5.0, 5.0, 5.0] | 0 / 190 / [1.5, 1.0, 1.6, 5.0] |

## worst_bypass: the same worst case with the ARQ bypassed

| | v6.9, fixed rate | v6.9, adaptive rate |
|---|---|---|
| hits applied | 104 | 77 |
| ghost hits: victim really > 1.5 m from where the hit was judged | 55 (53 %) | 36 (47 %) |
| distance between judged and true victim position, p95 | 5.29 m | 4.38 m |
| hits applied > 1 s after the shot (max) | 0 (0.4 s) | 0 (0.4 s) |
| deaths / killed by a hit fired > 1 s earlier / killed by a ghost hit | 20 / 0 / 14 | 12 / 0 / 5 |
| hits on the shooter's screen that counted (NEW: not counting shots at players already dead on the host) | 94 of 234 (40 %) | 74 of 263 (28 %) |
| shots rejected (reason count) | dead 14, late 171 | dead 20, late 197 |
| view age the host judged shots at, p50 / p95 | 852 ms / 2313 ms | 867 ms / 2663 ms |
| MAC queue max / payload age when sent p50 / p95 / max | 24 / 92 ms / 1640 ms / 3.2 s | 25 / 88 ms / 1783 ms / 3.2 s |
| MAC queue-full drops / newest-only replacements | 0 / 0 | 0 / 0 |
| frames on air DATA / Block ACK, collisions, channel airtime | 2281 / 0, 1592, 41 s | 1971 / 0, 1236, 34 s |
| NEW: roles at the end, migrations, client corrections, shots at lagging players | {'client': 3, 'host': 1}, 16, 4, 0 | {'host': 1, 'client': 3}, 20, 5, 4 |
| v6.9: datagrams dropped from full queues / rate cuts / rates at the end | 0 / 0 / [5.0, 5.0, 5.0, 5.0] | 0 / 61 / [4.4, 2.8, 5.0, 5.0] |

## host_dies: 4 players, 5/s, ARQ on, 5 % loss; node 0 (the host in NEW) is switched off at 60 s

| | v6.9, fixed rate | v6.9, adaptive rate |
|---|---|---|
| hits applied | 18 | 82 |
| ghost hits: victim really > 1.5 m from where the hit was judged | 18 (100 %) | 41 (50 %) |
| distance between judged and true victim position, p95 | 29.15 m | 29.15 m |
| hits applied > 1 s after the shot (max) | 0 (0.0 s) | 0 (0.4 s) |
| deaths / killed by a hit fired > 1 s earlier / killed by a ghost hit | 3 / 0 / 3 | 16 / 0 / 8 |
| hits on the shooter's screen that counted (NEW: not counting shots at players already dead on the host) | 18 of 133 (14 %) | 73 of 233 (31 %) |
| shots rejected (reason count) | late 182 | dead 20, late 526 |
| view age the host judged shots at, p50 / p95 | 17823 ms / 34935 ms | 1416 ms / 4080 ms |
| MAC queue max / payload age when sent p50 / p95 / max | 256 / 28861 ms / 73322 ms / 83.5 s | 21 / 417 ms / 3741 ms / 6.5 s |
| MAC queue-full drops / newest-only replacements | 21 / 0 | 0 / 0 |
| frames on air DATA / Block ACK, collisions, channel airtime | 1236 / 104, 763, 58 s | 1248 / 431, 610, 31 s |
| NEW: roles at the end, migrations, client corrections, shots at lagging players | {'host': 3}, 3, 1, 0 | {'host': 1, 'client': 2}, 17, 1, 1 |
| v6.9: datagrams dropped from full queues / rate cuts / rates at the end | 211 / 0 / [5.0, 5.0, 5.0, 5.0] | 0 / 174 / [1.0, 1.0, 1.1, 2.4] |

## two_worst: 2 players (the two PCs), 5/s, ARQ on, 10 % loss, 2 s interference every 15 s, node 1 stalls 3 s every 20 s

| | v6.9, fixed rate | v6.9, adaptive rate |
|---|---|---|
| hits applied | 28 | 31 |
| ghost hits: victim really > 1.5 m from where the hit was judged | 21 (75 %) | 14 (45 %) |
| distance between judged and true victim position, p95 | 6.36 m | 3.40 m |
| hits applied > 1 s after the shot (max) | 0 (0.5 s) | 0 (0.4 s) |
| deaths / killed by a hit fired > 1 s earlier / killed by a ghost hit | 5 / 0 / 4 | 6 / 0 / 3 |
| hits on the shooter's screen that counted (NEW: not counting shots at players already dead on the host) | 27 of 95 (28 %) | 30 of 61 (49 %) |
| shots rejected (reason count) | dead 5, late 165 | dead 5, late 101 |
| view age the host judged shots at, p50 / p95 | 3250 ms / 6113 ms | 2299 ms / 5199 ms |
| MAC queue max / payload age when sent p50 / p95 / max | 41 / 455 ms / 3796 ms / 6.2 s | 24 / 139 ms / 3173 ms / 6.4 s |
| MAC queue-full drops / newest-only replacements | 0 / 0 | 0 / 0 |
| frames on air DATA / Block ACK, collisions, channel airtime | 985 / 268, 320, 25 s | 690 / 261, 140, 14 s |
| NEW: roles at the end, migrations, client corrections, shots at lagging players | {'host': 1, 'client': 1}, 0, 4, 0 | {'host': 1, 'client': 1}, 1, 3, 0 |
| v6.9: datagrams dropped from full queues / rate cuts / rates at the end | 0 / 0 / [5.0, 5.0] | 0 / 71 / [3.0, 4.2] |

## two_normal_bypass: 2 players (the two PCs), 5/s, ARQ bypassed, 2 % loss

| | v6.9, fixed rate | v6.9, adaptive rate |
|---|---|---|
| hits applied | 45 | 45 |
| ghost hits: victim really > 1.5 m from where the hit was judged | 12 (27 %) | 12 (27 %) |
| distance between judged and true victim position, p95 | 2.05 m | 2.05 m |
| hits applied > 1 s after the shot (max) | 0 (0.3 s) | 0 (0.3 s) |
| deaths / killed by a hit fired > 1 s earlier / killed by a ghost hit | 11 / 0 / 6 | 11 / 0 / 6 |
| hits on the shooter's screen that counted (NEW: not counting shots at players already dead on the host) | 44 of 56 (79 %) | 44 of 56 (79 %) |
| shots rejected (reason count) | dead 11, late 2 | dead 11, late 2 |
| view age the host judged shots at, p50 / p95 | 535 ms / 701 ms | 535 ms / 701 ms |
| MAC queue max / payload age when sent p50 / p95 / max | 3 / 60 ms / 110 ms / 0.2 s | 3 / 60 ms / 110 ms / 0.2 s |
| MAC queue-full drops / newest-only replacements | 0 / 0 | 0 / 0 |
| frames on air DATA / Block ACK, collisions, channel airtime | 1268 / 0, 194, 16 s | 1268 / 0, 194, 16 s |
| NEW: roles at the end, migrations, client corrections, shots at lagging players | {'host': 1, 'client': 1}, 0, 1, 0 | {'host': 1, 'client': 1}, 0, 1, 0 |
| v6.9: datagrams dropped from full queues / rate cuts / rates at the end | 0 / 0 / [5.0, 5.0] | 0 / 0 / [5.0, 5.0] |

## two_worst_bypass: the two-PC worst case with the ARQ bypassed

| | v6.9, fixed rate | v6.9, adaptive rate |
|---|---|---|
| hits applied | 41 | 43 |
| ghost hits: victim really > 1.5 m from where the hit was judged | 20 (49 %) | 22 (51 %) |
| distance between judged and true victim position, p95 | 4.04 m | 3.10 m |
| hits applied > 1 s after the shot (max) | 0 (0.5 s) | 0 (0.4 s) |
| deaths / killed by a hit fired > 1 s earlier / killed by a ghost hit | 9 / 0 / 6 | 9 / 0 / 7 |
| hits on the shooter's screen that counted (NEW: not counting shots at players already dead on the host) | 38 of 56 (68 %) | 36 of 54 (67 %) |
| shots rejected (reason count) | dead 7, late 48 | dead 11, late 38 |
| view age the host judged shots at, p50 / p95 | 682 ms / 2513 ms | 701 ms / 2422 ms |
| MAC queue max / payload age when sent p50 / p95 / max | 20 / 66 ms / 1855 ms / 3.1 s | 20 / 66 ms / 1809 ms / 3.1 s |
| MAC queue-full drops / newest-only replacements | 0 / 0 | 0 / 0 |
| frames on air DATA / Block ACK, collisions, channel airtime | 1073 / 0, 220, 16 s | 896 / 0, 126, 14 s |
| NEW: roles at the end, migrations, client corrections, shots at lagging players | {'host': 1, 'client': 1}, 0, 0, 0 | {'host': 1, 'client': 1}, 0, 1, 0 |
| v6.9: datagrams dropped from full queues / rate cuts / rates at the end | 0 / 0 / [5.0, 5.0] | 0 / 38 / [4.1, 3.0] |

## three_bypass: 3 players, 5/s, ARQ bypassed, 5 % loss

| | v6.9, fixed rate | v6.9, adaptive rate |
|---|---|---|
| hits applied | 94 | 95 |
| ghost hits: victim really > 1.5 m from where the hit was judged | 45 (48 %) | 53 (56 %) |
| distance between judged and true victim position, p95 | 4.06 m | 3.34 m |
| hits applied > 1 s after the shot (max) | 0 (0.5 s) | 0 (0.5 s) |
| deaths / killed by a hit fired > 1 s earlier / killed by a ghost hit | 19 / 0 / 11 | 19 / 0 / 13 |
| hits on the shooter's screen that counted (NEW: not counting shots at players already dead on the host) | 82 of 138 (59 %) | 86 of 147 (59 %) |
| shots rejected (reason count) | dead 31, late 58 | dead 23, late 44 |
| view age the host judged shots at, p50 / p95 | 748 ms / 1363 ms | 751 ms / 1300 ms |
| MAC queue max / payload age when sent p50 / p95 / max | 3 / 69 ms / 134 ms / 0.3 s | 3 / 69 ms / 131 ms / 0.3 s |
| MAC queue-full drops / newest-only replacements | 0 / 0 | 0 / 0 |
| frames on air DATA / Block ACK, collisions, channel airtime | 1970 / 0, 930, 29 s | 1928 / 0, 890, 28 s |
| NEW: roles at the end, migrations, client corrections, shots at lagging players | {'client': 2, 'host': 1}, 0, 1, 0 | {'client': 2, 'host': 1}, 2, 1, 0 |
| v6.9: datagrams dropped from full queues / rate cuts / rates at the end | 0 / 0 / [5.0, 5.0, 5.0] | 0 / 10 / [2.9, 5.0, 5.0] |

## Settings sweep: datagrams (ARQ bypass), adaptive rate, 2 % loss, 60 s per run (v4 packets)

| players | menu rate | view age p50 / p95 | MAC queue age p95 | channel airtime | rates at the end | rate cuts |
|---|---|---|---|---|---|---|
| 2 | 5/s | 553 / 726 ms | 107 ms | 8 s of 60 | 5, 5 | 0 |
| 2 | 8/s | 501 / 701 ms | 126 ms | 12 s | 8, 8 | 0 |
| 2 | 10/s | 495 / 864 ms | 123 ms | 14 s | 10, 10 | 0 |
| 3 | 5/s | 767 / 1515 ms | 126 ms | 14 s | 4, 5, 5 | 3 |
| 3 | 8/s | 798 / 1802 ms | 147 ms | 19 s | 8, 8, 5.1 | 7 |
| 3 | 10/s | 749 / 1651 ms | 148 ms | 22 s | 5.9, 10, 10 | 8 |
| 4 | 5/s | 884 / 2197 ms | 155 ms | 18 s | 2.9, 5, 5, 5 | 24 |
| 4 | 8/s | 865 / 2099 ms | 160 ms | 22 s | 3.8, 7, 6.6, 4.4 | 30 |
| 4 | 10/s | 835 / 2049 ms | 157 ms | 24 s | 10, 7.9, 1.0, 7.9 | 34 |

v4 packets are a few bytes longer than v3 (weapon, healing and medkits per player, medkit drops), so the channel is a
little busier: 2 players fit at any rate, 3 players at 5-8/s, 4 players at 5/s with the congestion control stepping in.
