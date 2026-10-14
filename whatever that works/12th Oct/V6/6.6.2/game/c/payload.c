/* payload.c - see payload.h. Byte-identical to payload.py. */
#include "payload.h"

#include <math.h>
#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif
#include <string.h>

#define TWO_PI (2.0 * M_PI)
#define MAX_PITCH_DEG 89.99

static long rnd(double v) { return (long)floor(v + 0.5); }      /* round half up */

static int16_t clamp_i16(long v)
{
    return (int16_t)(v < -32768 ? -32768 : v > 32767 ? 32767 : v);
}

static void put16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)(v & 0xFF); p[1] = (uint8_t)(v >> 8); }
static uint16_t get16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }

static uint16_t q_yaw(double rad)
{
    double r = fmod(rad, TWO_PI);
    if (r < 0) r += TWO_PI;
    return (uint16_t)(rnd(r / TWO_PI * 65536.0) & 0xFFFF);
}

static int16_t q_pitch(double rad)
{
    double d = rad * 180.0 / M_PI;
    if (d > MAX_PITCH_DEG) d = MAX_PITCH_DEG;
    if (d < -MAX_PITCH_DEG) d = -MAX_PITCH_DEG;
    return clamp_i16(rnd(d * 100.0));
}

size_t gp_build(const gp_state_t *s, uint8_t out[GP_LEN])
{
    uint8_t n = s->n_events > GP_MAX_EVENTS ? GP_MAX_EVENTS : s->n_events;
    memset(out, 0, GP_LEN);
    out[0] = s->pid;
    put16(out + 1, s->seq);
    put16(out + 3, (uint16_t)clamp_i16(rnd(s->x * 100.0)));
    put16(out + 5, (uint16_t)clamp_i16(rnd(s->y * 100.0)));
    put16(out + 7, (uint16_t)clamp_i16(rnd(s->z * 100.0)));
    put16(out + 9, q_yaw(s->yaw));
    put16(out + 11, (uint16_t)q_pitch(s->pitch));
    out[13] = s->hp; out[14] = s->ammo; out[15] = s->flags;
    out[16] = s->deaths; out[17] = s->aux; out[18] = n;
    for (int i = 0; i < n; i++) {
        uint8_t *e = out + 19 + 4 * i;
        put16(e, s->events[i].eid);
        e[2] = s->events[i].target;
        e[3] = s->events[i].damage;
    }
    return GP_LEN;
}

int gp_parse(const uint8_t *b, size_t len, gp_state_t *o)
{
    if (len < GP_LEN) return -1;
    memset(o, 0, sizeof *o);
    o->pid = b[0];
    o->seq = get16(b + 1);
    o->x = (int16_t)get16(b + 3) / 100.0;
    o->y = (int16_t)get16(b + 5) / 100.0;
    o->z = (int16_t)get16(b + 7) / 100.0;
    o->yaw = get16(b + 9) / 65536.0 * TWO_PI;
    o->pitch = (int16_t)get16(b + 11) / 100.0 * M_PI / 180.0;
    o->hp = b[13]; o->ammo = b[14]; o->flags = b[15];
    o->deaths = b[16]; o->aux = b[17];
    o->n_events = b[18] > GP_MAX_EVENTS ? GP_MAX_EVENTS : b[18];
    for (int i = 0; i < o->n_events; i++) {
        const uint8_t *e = b + 19 + 4 * i;
        o->events[i].eid = get16(e);
        o->events[i].target = e[2];
        o->events[i].damage = e[3];
    }
    return 0;
}
