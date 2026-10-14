/*
 * payload.h - 3D shooter game payload, C version (byte-identical to payload.py).
 * Pure application data: no marker, no version, no CRC.
 *
 * Fixed 31-byte little-endian packet:
 *   0 pid | 1 seq u16 | 3 x i16 cm | 5 y i16 cm | 7 z i16 cm
 *   9 yaw u16 (0..65535 = 0..360deg) | 11 pitch i16 centideg | 13 hp | 14 ammo
 *  15 flags | 16 deaths | 17 aux | 18 n_events | 19 3x{eid u16, target u8, dmg u8}
 *
 * Use from a GNU Radio C++ OOT block, or load as a shared lib via ctypes:
 *   gcc -O2 -shared -fPIC payload.c -o libgamepayload.so -lm
 */
#ifndef GAME_PAYLOAD_H
#define GAME_PAYLOAD_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define GP_LEN          31
#define GP_MAX_EVENTS   3

#define GP_FLAG_FIRED      0x01
#define GP_FLAG_RELOADING  0x02
#define GP_FLAG_DEAD       0x04

typedef struct {
    uint16_t eid;     /* event id, unique per sender */
    uint8_t  target;  /* player id that was hit      */
    uint8_t  damage;  /* hit points to remove        */
} gp_event_t;

typedef struct {
    uint8_t  pid;
    uint16_t seq;
    double   x, y, z;      /* metres  */
    double   yaw, pitch;   /* radians */
    uint8_t  hp, ammo, flags, deaths, aux;
    uint8_t  n_events;     /* 0..GP_MAX_EVENTS */
    gp_event_t events[GP_MAX_EVENTS];
} gp_state_t;

/* state -> out[GP_LEN]; returns GP_LEN */
size_t gp_build(const gp_state_t *s, uint8_t out[GP_LEN]);

/* reads the first GP_LEN bytes of buf (extra bytes ignored).
   returns 0, or -1 if len < GP_LEN */
int gp_parse(const uint8_t *buf, size_t len, gp_state_t *out);

#ifdef __cplusplus
}
#endif
#endif /* GAME_PAYLOAD_H */
