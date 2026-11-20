/* payload.c - Radio Arena v4 packets, see payload.h. Byte-identical to payload.py. */
#include "payload.h"

#include <math.h>
#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif
#include <string.h>

#define TWO_PI (2.0 * M_PI)

static long rnd(double v) { return (long)floor(v + 0.5); }
static long clampl(long v, long lo, long hi) { return v < lo ? lo : v > hi ? hi : v; }
static uint8_t q4(double ms) { return (uint8_t)clampl(rnd(ms / 4.0), 0, 255); }
static void p16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static uint16_t g16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }

static uint16_t yaw16(double r) { r = fmod(r, TWO_PI); if (r < 0) r += TWO_PI; return (uint16_t)(rnd(r / TWO_PI * 65536.0) & 0xFFFF); }
static uint8_t yaw8(double r) { r = fmod(r, TWO_PI); if (r < 0) r += TWO_PI; return (uint8_t)(rnd(r / TWO_PI * 256.0) & 0xFF); }
static int16_t pitch16(double r)
{
    double d = r * 180.0 / M_PI;
    if (d > 89.99) d = 89.99;
    if (d < -89.99) d = -89.99;
    return (int16_t)clampl(rnd(d * 100.0), -32768, 32767);
}
static int8_t pitch8(double r)
{
    double d = r * 180.0 / M_PI;
    if (d > 90.0) d = 90.0;
    if (d < -90.0) d = -90.0;
    return (int8_t)clampl(rnd(d * 127.0 / 90.0), -127, 127);
}
static int16_t cm16(double m) { return (int16_t)clampl(rnd(m * 100.0), -32768, 32767); }

static size_t hdr(uint8_t *o, uint8_t type, uint8_t node, uint16_t seq, uint16_t ts)
{
    o[0] = type; o[1] = node; p16(o + 2, seq); p16(o + 4, ts);
    return 6;
}

size_t gp_build_beacon(const gp_beacon_t *m, uint8_t *o)
{
    size_t n = hdr(o, GP_T_BEACON, m->node, m->seq, m->ts);
    uint8_t np = m->n_peers > GP_MAX_PEERS ? GP_MAX_PEERS : m->n_peers;
    o[n++] = m->flags;
    memset(o + n, 0, GP_NAME_LEN);
    for (int i = 0; i < GP_NAME_LEN && m->name[i]; i++) o[n + i] = (uint8_t)m->name[i];
    n += GP_NAME_LEN;
    o[n++] = (uint8_t)clampl(rnd(m->rate * 10.0), 0, 255);
    o[n++] = m->max_players;
    o[n++] = (uint8_t)clampl(rnd(m->max_lag * 100.0), 0, 255);
    o[n++] = m->host;
    o[n++] = m->epoch;
    o[n++] = np;
    for (int i = 0; i < np; i++) {
        const gp_peer_t *p = &m->peers[i];
        o[n++] = p->id;
        o[n++] = p->echo;
        o[n++] = q4(p->hold_ms);
        o[n++] = p->rtt_ms < 0 ? 255 : (q4(p->rtt_ms) > 254 ? 254 : q4(p->rtt_ms));
    }
    return n;
}

size_t gp_build_input(const gp_input_t *m, uint8_t *o)
{
    size_t n = hdr(o, GP_T_INPUT, m->node, m->seq, m->ts);
    uint8_t na = m->n_actions > GP_MAX_ACTIONS ? GP_MAX_ACTIONS : m->n_actions;
    o[n++] = m->host;
    o[n++] = m->flags;
    p16(o + n, (uint16_t)cm16(m->x)); n += 2;
    p16(o + n, (uint16_t)cm16(m->y)); n += 2;
    p16(o + n, (uint16_t)cm16(m->z)); n += 2;
    p16(o + n, yaw16(m->yaw)); n += 2;
    p16(o + n, (uint16_t)pitch16(m->pitch)); n += 2;
    p16(o + n, m->snap_ack); n += 2;
    o[n++] = q4(m->snap_hold_ms);
    o[n++] = na;
    for (int i = 0; i < na; i++) {
        const gp_action_t *a = &m->actions[i];
        o[n++] = a->aid;
        o[n++] = (uint8_t)((a->kind & 0x0F) | ((a->weapon & 3) << 4));
        p16(o + n, (uint16_t)(int16_t)clampl(a->dt_ms, -32768, 32767)); n += 2;
        p16(o + n, yaw16(a->yaw)); n += 2;
        p16(o + n, (uint16_t)pitch16(a->pitch)); n += 2;
        p16(o + n, a->view_seq); n += 2;
        o[n++] = a->view_next;
        o[n++] = a->view_frac;
        o[n++] = (uint8_t)(int8_t)(a->pos_known ? clampl(rnd(a->dx * 25.0), -127, 127) : -128);
        o[n++] = (uint8_t)(int8_t)(a->pos_known ? clampl(rnd(a->dz * 25.0), -127, 127) : -128);
    }
    return n;
}

size_t gp_build_snapshot(const gp_snapshot_t *m, uint8_t *o)
{
    size_t n = hdr(o, GP_T_SNAPSHOT, m->node, m->seq, m->ts);
    uint8_t np = m->n_players > GP_MAX_PLAYERS ? GP_MAX_PLAYERS : m->n_players;
    uint8_t ne = m->n_events > GP_MAX_EVENTS ? GP_MAX_EVENTS : m->n_events;
    uint8_t nd = m->n_drops > GP_MAX_DROPS ? GP_MAX_DROPS : m->n_drops;
    o[n++] = m->epoch; o[n++] = m->flags; o[n++] = m->max_players; o[n++] = np; o[n++] = ne;
    o[n++] = m->spots; o[n++] = nd;
    for (int i = 0; i < np; i++) {
        const gp_splayer_t *p = &m->players[i];
        uint32_t xc = (uint32_t)clampl(rnd(p->x * 100.0), 0, 4095);
        uint32_t zc = (uint32_t)clampl(rnd(p->z * 100.0), 0, 4095);
        uint32_t yc = (uint32_t)clampl(rnd(p->y * 50.0), 0, 255);      /* 2 cm steps */
        uint32_t pos = xc | (zc << 12) | (yc << 24);
        o[n++] = p->id;
        o[n++] = (uint8_t)pos; o[n++] = (uint8_t)(pos >> 8); o[n++] = (uint8_t)(pos >> 16); o[n++] = (uint8_t)(pos >> 24);
        o[n++] = yaw8(p->yaw);
        o[n++] = (uint8_t)pitch8(p->pitch);
        o[n++] = p->hp; o[n++] = p->flags; o[n++] = p->gear; o[n++] = p->kills; o[n++] = p->deaths; o[n++] = p->ack;
        o[n++] = q4(p->hold_ms);
    }
    for (int i = 0; i < ne; i++) {
        const gp_event_t *e = &m->events[i];
        o[n++] = e->eid; o[n++] = e->shooter; o[n++] = e->victim; o[n++] = e->damage;
        o[n++] = (uint8_t)((e->head ? GP_E_HEAD : 0) | (e->kill ? GP_E_KILL : 0) | ((e->weapon & 3) << 2));
    }
    for (int i = 0; i < nd; i++) {
        const gp_drop_t *d = &m->drops[i];
        o[n++] = d->id;
        o[n++] = (uint8_t)clampl(rnd(d->x * 8.0), 0, 255);
        o[n++] = (uint8_t)clampl(rnd(d->z * 8.0), 0, 255);
    }
    return n;
}

int gp_type(const uint8_t *b, size_t len) { return len < 6 ? -1 : b[0]; }

static void rhdr(const uint8_t *b, uint8_t *node, uint16_t *seq, uint16_t *ts)
{
    *node = b[1]; *seq = g16(b + 2); *ts = g16(b + 4);
}

int gp_parse_beacon(const uint8_t *b, size_t len, gp_beacon_t *m)
{
    if (len < 21 || b[0] != GP_T_BEACON) return -1;
    memset(m, 0, sizeof *m);
    rhdr(b, &m->node, &m->seq, &m->ts);
    m->flags = b[6];
    memcpy(m->name, b + 7, GP_NAME_LEN);
    m->rate = b[15] / 10.0; m->max_players = b[16]; m->max_lag = b[17] / 100.0;
    m->host = b[18]; m->epoch = b[19];
    uint8_t np = b[20] > GP_MAX_PEERS ? GP_MAX_PEERS : b[20];
    if (len < 21u + 4u * np) return -1;
    m->n_peers = np;
    for (int i = 0; i < np; i++) {
        const uint8_t *p = b + 21 + 4 * i;
        m->peers[i].id = p[0]; m->peers[i].echo = p[1]; m->peers[i].hold_ms = p[2] * 4;
        m->peers[i].rtt_ms = p[3] == 255 ? -1 : p[3] * 4;
    }
    return 0;
}

int gp_parse_input(const uint8_t *b, size_t len, gp_input_t *m)
{
    if (len < 22 || b[0] != GP_T_INPUT) return -1;
    memset(m, 0, sizeof *m);
    rhdr(b, &m->node, &m->seq, &m->ts);
    m->host = b[6]; m->flags = b[7];
    m->x = (int16_t)g16(b + 8) / 100.0; m->y = (int16_t)g16(b + 10) / 100.0; m->z = (int16_t)g16(b + 12) / 100.0;
    m->yaw = g16(b + 14) / 65536.0 * TWO_PI;
    m->pitch = (int16_t)g16(b + 16) / 100.0 * M_PI / 180.0;
    m->snap_ack = g16(b + 18); m->snap_hold_ms = b[20] * 4;
    uint8_t na = b[21] > GP_MAX_ACTIONS ? GP_MAX_ACTIONS : b[21];
    if (len < 22u + 14u * na) return -1;
    m->n_actions = na;
    for (int i = 0; i < na; i++) {
        const uint8_t *a = b + 22 + 14 * i;
        gp_action_t *o = &m->actions[i];
        o->aid = a[0]; o->kind = a[1] & 0x0F; o->weapon = (a[1] >> 4) & 3; o->dt_ms = (int16_t)g16(a + 2);
        o->yaw = g16(a + 4) / 65536.0 * TWO_PI; o->pitch = (int16_t)g16(a + 6) / 100.0 * M_PI / 180.0;
        o->view_seq = g16(a + 8); o->view_next = a[10]; o->view_frac = a[11];
        o->pos_known = (int8_t)a[12] != -128 && (int8_t)a[13] != -128;
        o->dx = o->pos_known ? (int8_t)a[12] / 25.0 : 0.0; o->dz = o->pos_known ? (int8_t)a[13] / 25.0 : 0.0;
    }
    return 0;
}

int gp_parse_snapshot(const uint8_t *b, size_t len, gp_snapshot_t *m)
{
    if (len < 13 || b[0] != GP_T_SNAPSHOT) return -1;
    memset(m, 0, sizeof *m);
    rhdr(b, &m->node, &m->seq, &m->ts);
    m->epoch = b[6]; m->flags = b[7]; m->max_players = b[8];
    uint8_t np = b[9] > GP_MAX_PLAYERS ? GP_MAX_PLAYERS : b[9];
    uint8_t ne = b[10] > GP_MAX_EVENTS ? GP_MAX_EVENTS : b[10];
    uint8_t nd = b[12] > GP_MAX_DROPS ? GP_MAX_DROPS : b[12];
    m->spots = b[11];
    if (len < 13u + 14u * np + 5u * ne + 3u * nd) return -1;
    m->n_players = np; m->n_events = ne; m->n_drops = nd;
    for (int i = 0; i < np; i++) {
        const uint8_t *p = b + 13 + 14 * i;
        gp_splayer_t *o = &m->players[i];
        uint32_t pos = (uint32_t)p[1] | ((uint32_t)p[2] << 8) | ((uint32_t)p[3] << 16) | ((uint32_t)p[4] << 24);
        o->id = p[0];
        o->x = (pos & 0xFFF) / 100.0; o->z = ((pos >> 12) & 0xFFF) / 100.0; o->y = ((pos >> 24) & 0xFF) / 50.0;
        o->yaw = p[5] / 256.0 * TWO_PI; o->pitch = (int8_t)p[6] * 90.0 / 127.0 * M_PI / 180.0;
        o->hp = p[7]; o->flags = p[8]; o->gear = p[9]; o->kills = p[10]; o->deaths = p[11]; o->ack = p[12];
        o->hold_ms = p[13] * 4;
    }
    const uint8_t *e = b + 13 + 14 * np;
    for (int i = 0; i < ne; i++, e += 5) {
        gp_event_t *o = &m->events[i];
        o->eid = e[0]; o->shooter = e[1]; o->victim = e[2]; o->damage = e[3];
        o->head = (e[4] & GP_E_HEAD) != 0; o->kill = (e[4] & GP_E_KILL) != 0; o->weapon = (e[4] >> 2) & 3;
    }
    for (int i = 0; i < nd; i++, e += 3) {
        m->drops[i].id = e[0]; m->drops[i].x = e[1] / 8.0; m->drops[i].z = e[2] / 8.0;
    }
    return 0;
}
