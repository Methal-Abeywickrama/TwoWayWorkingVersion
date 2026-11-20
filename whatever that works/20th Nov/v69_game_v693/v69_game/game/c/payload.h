/*
 * payload.h - Radio Arena v4 packets (host-authoritative), C version. Byte-identical to payload.py.
 * Pure application data: no marker, no version byte, no CRC. Layout: see payload.py.
 * v4 = v3 + weapons, healing, medkit pickups, 2 cm height steps; type numbers 4-6 (v3 used 1-3).
 *
 *   common header (6 B): type u8 | node u8 | seq u16 | ts u16 (ms)          little-endian
 *   type 4 BEACON   (lobby / election, broadcast)       21 + 4 per peer
 *   type 5 INPUT    (client -> host)                    22 + 14 per action
 *   type 6 SNAPSHOT (host -> everyone)                  13 + 14 per player + 5 per event + 3 per drop
 *
 *   gcc -O2 -shared -fPIC payload.c -o libgamepayload.so -lm
 */
#ifndef GAME_PAYLOAD_H
#define GAME_PAYLOAD_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define GP_T_BEACON    4
#define GP_T_INPUT     5
#define GP_T_SNAPSHOT  6
#define GP_NONE        255
#define GP_NAME_LEN    8
#define GP_MAX_PEERS   7
#define GP_MAX_ACTIONS 3
#define GP_MAX_PLAYERS 8
#define GP_MAX_EVENTS  3
#define GP_MAX_DROPS   4
#define GP_MAX_LEN     (6 + 7 + 14 * GP_MAX_PLAYERS + 5 * GP_MAX_EVENTS + 3 * GP_MAX_DROPS)   /* largest: 152 B */
#define GP_FRAME_BUDGET 68      /* payload bytes that still fit a 12.3 ms frame on v6.8 */
#define GP_FRAME_BUDGET_V69 59  /* the same on v6.9 (11 B bridge datagram header instead of the 2 B tag) */

/* BEACON flags */
#define GP_B_READY 0x01
#define GP_B_IN_ARENA 0x02
#define GP_B_HOSTING 0x04
#define GP_B_LEAVING 0x08
#define GP_B_ARQ 0x10
#define GP_B_ELECT_RANDOM 0x20
/* INPUT flags (bits 4-5 = life, bits 6-7 = weapon in hand) */
#define GP_I_IN 0x01
#define GP_I_LEAVING 0x02
#define GP_I_RELOADING 0x04
#define GP_I_ALIVE 0x08
/* SNAPSHOT flags */
#define GP_S_CLOSING 0x01
#define GP_S_FULL 0x02
/* SNAPSHOT player flags (bits 3-4 = life) */
#define GP_P_DEAD 0x01
#define GP_P_FIRED 0x02
#define GP_P_RELOADING 0x04
#define GP_P_CORRECTED 0x20
#define GP_P_HOST 0x40
/* actions (low nibble of the kind byte; weapon in bits 4-5), weapons, gear, event info */
#define GP_A_FIRE 1
#define GP_A_RELOAD 2
#define GP_A_HEAL 3
#define GP_W_RIFLE 0
#define GP_W_SNIPER 1
#define GP_W_KNIFE 2
#define GP_G_HEALING 0x04        /* gear: bits0-1 weapon, bit2 healing, bits3-4 medkits */
#define GP_E_HEAD 0x01
#define GP_E_KILL 0x02           /* event info bits2-3 = weapon */

typedef struct { uint8_t id, echo; int hold_ms, rtt_ms; /* rtt -1 = unknown */ } gp_peer_t;

typedef struct {
    uint8_t node; uint16_t seq, ts; uint8_t flags;
    char name[GP_NAME_LEN + 1];
    double rate;            /* packets/s */
    uint8_t max_players;
    double max_lag;         /* s */
    uint8_t host, epoch, n_peers;
    gp_peer_t peers[GP_MAX_PEERS];
} gp_beacon_t;

typedef struct {
    uint8_t aid, kind; int dt_ms;
    double yaw, pitch;      /* radians */
    uint16_t view_seq;      /* snapshot the shooter's view starts from */
    uint8_t view_next;      /* + this many seq = second snapshot; 0 = extrapolating */
    uint8_t view_frac;      /* 0..255 between them; extrapolating: 4 ms steps past view_seq */
    int pos_known;          /* 0: dx/dz unknown (sent as -128) */
    double dx, dz;          /* shooter position at the shot - packet position (m, 4 cm steps) */
    uint8_t weapon;         /* GP_W_*: sent in bits 4-5 of the kind byte (kind = low nibble) */
} gp_action_t;

typedef struct {
    uint8_t node; uint16_t seq, ts; uint8_t host, flags;
    double x, y, z, yaw, pitch;
    uint16_t snap_ack; int snap_hold_ms;
    uint8_t n_actions;
    gp_action_t actions[GP_MAX_ACTIONS];
} gp_input_t;

typedef struct {
    uint8_t id; double x, y, z, yaw, pitch;
    uint8_t hp, flags, kills, deaths, ack; int hold_ms;
    uint8_t gear;           /* weapon bits0-1 | GP_G_HEALING | medkits << 3 */
} gp_splayer_t;

typedef struct { uint8_t eid, shooter, victim, damage, head, kill, weapon; } gp_event_t;

typedef struct { uint8_t id; double x, z; /* m, 12.5 cm steps */ } gp_drop_t;

typedef struct {
    uint8_t node; uint16_t seq, ts; uint8_t epoch, flags, max_players, n_players, n_events;
    gp_splayer_t players[GP_MAX_PLAYERS];
    gp_event_t events[GP_MAX_EVENTS];
    uint8_t spots;          /* bit k: fixed medkit spot k holds a medkit */
    uint8_t n_drops;
    gp_drop_t drops[GP_MAX_DROPS];
} gp_snapshot_t;

/* build: returns the packet length (out needs GP_MAX_LEN bytes) */
size_t gp_build_beacon(const gp_beacon_t *m, uint8_t *out);
size_t gp_build_input(const gp_input_t *m, uint8_t *out);
size_t gp_build_snapshot(const gp_snapshot_t *m, uint8_t *out);
/* parse: 0 = ok, -1 = short / wrong type */
int gp_type(const uint8_t *buf, size_t len);       /* message type or -1 */
int gp_parse_beacon(const uint8_t *buf, size_t len, gp_beacon_t *m);
int gp_parse_input(const uint8_t *buf, size_t len, gp_input_t *m);
int gp_parse_snapshot(const uint8_t *buf, size_t len, gp_snapshot_t *m);

#ifdef __cplusplus
}
#endif
#endif
