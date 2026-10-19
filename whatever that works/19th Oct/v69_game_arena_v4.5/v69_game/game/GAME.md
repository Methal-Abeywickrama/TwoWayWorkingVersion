# Radio Arena v4: how the game works

A 3D arena shooter for 2 to 8 players. All game traffic goes over the CDP radio: on v6.9 through the socket bridge's datagrams (see `GAME_V69.md`), on v6.8 through the **Game Link** block. One player's PC is the **host** and runs the only real game world. Every other PC is a **client**: it predicts its own movement and shows the others as the host reports them.

## Controls

| key | action |
|---|---|
| W A S D | move |
| mouse | look (full 360°). The arrow keys also turn. |
| left mouse | shoot / slash (rifle and knife: hold; sniper: one click per shot) |
| right mouse (hold) | sniper scope |
| 1 / 2 / 3 | rifle / sniper / knife |
| Q, mouse wheel | last weapon / next or previous weapon |
| H | heal with a medkit |
| R | reload |
| Space / Shift | jump / sprint |
| TAB | scoreboard |
| Esc | pause menu: Resume, Leave arena (back to the lobby), Main menu, Quit game, Close everything (v6.9) |
| F10 | quit |

## The arena (v4)

**Elevation.** The map has two buildings you can climb and some crates:

- **West building:** 3.0 m high. Two flights of stairs run up its east side.
- **South-east building:** 2.0 m high. Its stairs are on the south side.
- **Crates:** a 1.0 m crate can be jumped onto (Space while walking into it). The 1.5 m blocks can be reached with a running jump.
- **Walls:** 4 m high. They can't be climbed, but from a roof you can see and shoot over them.

The host checks every move in 3D:

- A player can only be as high as the ground under it plus a jump.
- A path must not pass through a block it can't step onto.

A client that claims to be on a roof without climbing is put back.

**Weapons.**

| | damage body / head | magazine | fire | reach |
|---|---|---|---|---|
| 1 Rifle | 20 / 50 | 30, reload 1.6 s | automatic, 9 shots/s | 80 m |
| 2 Sniper | 80 / 150 (a headshot always kills) | 5, reload 2.4 s | bolt action, 1 shot per 1.25 s | 150 m |
| 3 Knife | 55 (two hits) | – | a slash every 0.55 s | 2.3 m |

- **Sniper:** hold the right mouse button for the scope. The view zooms and turning slows. Without the scope a shot goes up to 3.4° off the aim, and the scope comes off after every shot.
- **Knife:** also hits a body within about 30° of the aim, so a melee doesn't need a pixel-perfect aim. You move 12 % faster with the knife out.
- **Switching** takes 0.4 s.
- **Host checks:** the host checks the fire rate, ammo and reach of every weapon separately.
- **Kill feed:** it shows [SNIPER] or [KNIFE].

**Healing.**

- **Medkits:**
  - You carry up to 3 and start every life with 1.
  - Pick one up by walking over it. They are white boxes with a red cross: 6 fixed spots (one on each roof), which come back 20 s after they are taken.
  - Every eliminated player drops one where they fell. It stays for 25 s.
- **H** uses a medkit:
  - **+50 HP over 3 s.** The HUD shows the progress, and you walk at 60 % speed meanwhile.
  - **Then an 8 s cooldown,** counted down next to the health panel.
- **Firing stops the heal**, and the rest of that medkit is lost.
- **All on the host:** medkits, healing and pickups are decided there, like HP. A client's H is a request; the host refuses it if the cooldown isn't over or no medkit is left.

## Screens

**Main menu.** These settings are saved in `~/.cdp_game_v69.json` (on v6.8: `~/.cdp_game.json`):

- **Name** (8 characters)
- **Packet rate**: packets per second each player sends
- **Max players**: the arena size
- **Lag limit**
- **Host election**: lowest ping or random
- **Radio**: ARQ on or ARQ bypass

The flowgraph variables `GAME_RATE`, `GAME_MAX`, `GAME_LAG` and `GAME_ARQ` are the defaults. A value the flowgraph passes wins if it changed since the last start; otherwise your last menu choice is used.

Under the settings, a line estimates how many frames per second a full arena puts on the air and whether the v6.8 channel can carry it.

**Practice vs N bots** plays offline. Every bot is a full game node, using the same protocol through an in-process "radio".

**Lobby.** Lists every node running the game, with columns for:

- status: lobby, ready, arena or HOST
- link: two-way or one-way
- ping
- packets per second
- the node's settings

The header line says whether an arena is running. **Enter arena** either joins the running arena or starts a host election among the players who want to play. If the arena is full, the button says so.

**In the arena:**

- **Top left:** your role.
  - Host: each client's ping, marked LAGGING if its inputs stopped.
  - Client: ping to the host, how far behind the host you are drawn, and your corrections count.
- **Top centre:** your kills and deaths.
- **Top right:** minimap, then the kill feed below it.
- **Above each player in sight:** name tag and HP bar.
- **Crosshair hit markers:** white means your own screen showed a hit; red means the host confirmed it.

A player whose packets stopped reaching the host for longer than the lag limit is drawn as a grey ghost tagged **LAGGING - shots don't count**.

## How a game runs

1. **Lobby.** Every node broadcasts a BEACON once per second. It carries the node's name and settings, and the round-trip time to every node it hears. Each beacon echoes the newest packet heard from each node, so the RTTs come for free.
2. **Election.** "Enter arena" checks for a running host.
   - If one exists, you join it.
   - If none exists, the nodes that want to play exchange beacons for 2.5 s. Each of them votes for the node with the lowest average RTT to the others, or for a random pick that every node computes the same way. The candidate with the most votes becomes host; a tie goes to the lowest node ID.
   - If two hosts meet (a split network heals), the arena with more players stays, then the newer epoch, then the lower node ID. The other host closes its arena and its players move over.
3. **Clients** send INPUT packets at the packet rate. Each one carries a timestamp, the client's position, angle, life counter and actions (fire, reload). A fire action records exactly what the shooter's screen showed: which two snapshots it was drawn between, and where between them, or how far it extrapolated past the newest one. It also records where the shooter stood.
   - Actions repeat in the following inputs until the host acknowledges them, so lost packets don't lose shots.
   - A new shot makes the next input go out early (at most twice the packet rate).
4. **The host** keeps a 3-second circular buffer of every player's position by host time.
   - **Movement:** a client's position is accepted only if it was reachable at running speed since the client's previous input and the path doesn't cross a wall. The player must also be alive and on the same life. Otherwise the host keeps its own position and flags the player CORRECTED.
   - **Shots:** the host rewinds every other player to exactly what the shooter saw, then ray-casts from the shooter's position against walls and the head and body hitboxes. A hit takes effect **now** and goes out in the next snapshot.
   - A shot is rejected:
     - if its view is older than the **lag limit** ("late");
     - if the shooter was dead or on an old life;
     - if it comes faster than the fire rate allows;
     - if it comes during a reload.
   - Players whose inputs stopped for longer than the lag limit are frozen ghosts on the host and can't be hit.
   - HP, deaths, kills and respawns exist only on the host. It respawns players at the spawn point farthest from everyone else.
5. **SNAPSHOT packets** are broadcast at the host's packet rate. They carry every player's position, angle, HP, flags, kills and deaths, plus hit and kill events. Each player's entry also says which of that client's inputs the host applied last.
6. **Client prediction and reconciliation.**
   - You move instantly on your own PC.
   - When a snapshot says the host applied your input N at a different position than you predicted for input N (more than 0.3 m), you are moved to the host's position plus the movement you made since input N. The camera smooths the jump.
   - Other players are drawn 1.15 snapshot intervals behind the newest host time, interpolated between snapshots. They are extrapolated for at most 0.25 s if snapshots are late.
7. **Host migration.** If no snapshot arrives for 3.5 s, or the host closes its arena, the remaining players take a new host from the newest snapshot they all hold: the lowest node ID among the players the old host still heard well. All survivors compute the same choice without talking. The new host continues that snapshot's world, with the same positions, HP and scores. In simulation the game goes on about 4 to 5 s after the host disappears. If the old host comes back before the new one is chosen, the players simply stay with it.

### Why a lagged player no longer dies all at once

The earlier version broke in three ways:

- The victim applied every hit report whenever it arrived.
- A shooter could hit an image of a player that was seconds old.
- The MAC queued every game frame first-in first-out.

So under load the hits piled up in the queue and arrived together, up to minutes late in the worst case. Now:

- **Shots are judged by the host only.** The host judges against what the shooter actually saw, and only if that view is younger than the lag limit (1 s by default).
- **Hits take effect when the host judges them**, never later. They can't accumulate.
- **A player whose packets don't reach the host can't be shot at its frozen position.**
- **The MAC keeps only the newest game frame of each kind per destination,** so a snapshot or input never waits behind older ones. Game frames older than 2 s are given up.

In the worst-case simulation (`tools/SIM_REPORT.md`), deaths caused by hits fired more than 1 s earlier dropped from 9–30 per run to **0** in every scenario. The longest time between a shot and its effect dropped from up to 137 s to **0.5 s**.

The cost: a hit can land on a victim that already moved on its own screen, because the host favours what the shooter saw. That is bounded by the lag limit.

## Packets (`payload.py`, `c/payload.c`): v4

Pure application data: there is no marker, no version byte and no CRC. The radio adds its own header in front (v6.9 bridge: 11 B; v6.8 Game Link: `C5 12`), and the framer adds CRC32 and FEC.

v4 uses types 4–6 (v3 used 1–3), so an old game and a new one ignore each other. The lobby then says "node N runs an older Radio Arena". Every packet starts with the same 6-byte header, little-endian:

`type u8 | node u8 | seq u16 (per node, shared by all types) | ts u16 (sender clock in ms mod 65536)`

| type | sent by / to | size | content |
|---|---|---|---|
| 4 BEACON | every node, broadcast, 1/s (every 3 s in the arena, 2/s while electing) | 21 + 4 per peer | flags (READY, IN_ARENA, HOSTING, LEAVING, ARQ, ELECT_RANDOM), name, rate, max players, lag limit, host (or vote while electing), epoch, then per heard node: id, echo of its newest seq, hold time, RTT |
| 5 INPUT | client to host | 22 + 14 per action (at most 3) | host, flags (IN, LEAVING, RELOADING, ALIVE, life, weapon in hand), x y z (cm), yaw, pitch, newest snapshot received plus its hold time, then per action: id, kind (fire / reload / heal, plus the weapon), time offset, yaw, pitch, view snapshot, next snapshot offset, fraction, shooter dx/dz |
| 6 SNAPSHOT | host to everyone, broadcast | 13 + 14 per player + 5 per event + 3 per dropped medkit | epoch, flags (CLOSING, FULL), max players, medkit spots bitmask, then per player: id, packed position (x and z 12 bits each in cm, y 8 bits in 2 cm up to 5.1 m), yaw, pitch, HP, flags (DEAD, FIRED, RELOADING, life, CORRECTED, HOST), gear (weapon, HEALING, medkits), kills, deaths, last applied input, hold. Then events: id, shooter, victim, damage, HEAD / KILL / weapon. Then dropped medkits: id, x, z (12.5 cm) |

With 4 players a snapshot is about 70 bytes and an input at most 64.

```bash
python3 game/payload.py                       # self-test, prints the three sample packets in hex
gcc -O2 -Wall game/c/test_payload.c game/c/payload.c -o test_payload -lm && ./test_payload   # same hex
python3 tools/test_payload_c.py               # 18,000 random packets: C and Python byte-identical (C parse too)
```

C API: `gp_build_beacon / gp_build_input / gp_build_snapshot` return the length. `gp_type()` reads the type. `gp_parse_*` return 0 on success, or -1 if the packet is short or of the wrong type.
