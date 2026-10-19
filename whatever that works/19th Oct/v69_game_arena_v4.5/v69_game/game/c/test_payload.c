/* test_payload.c - builds the same three messages as payload.py's self-test and prints them in hex.
 *   gcc -O2 -Wall test_payload.c payload.c -o test_payload -lm && ./test_payload
 */
#include <math.h>
#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif
#include <stdio.h>
#include <string.h>
#include "payload.h"

#define RAD(d) ((d) * M_PI / 180.0)

static void hex(const char *name, const uint8_t *b, size_t n)
{
    printf("%-8s %3zu B  ", name, n);
    for (size_t i = 0; i < n; i++) printf("%02x", b[i]);
    printf("\n");
}

int main(void)
{
    uint8_t buf[GP_MAX_LEN];
    size_t n;
    gp_beacon_t b; memset(&b, 0, sizeof b);
    b.node = 3; b.seq = 65535; b.ts = 54321; b.flags = GP_B_READY | GP_B_ARQ; strcpy(b.name, "Methal");
    b.rate = 4.5; b.max_players = 4; b.max_lag = 0.8; b.host = 1; b.epoch = 7; b.n_peers = 2;
    b.peers[0] = (gp_peer_t){0, 200, 120, 88}; b.peers[1] = (gp_peer_t){1, 17, 4, -1};
    n = gp_build_beacon(&b, buf); hex("beacon", buf, n);
    gp_beacon_t rb; if (gp_parse_beacon(buf, n, &rb) || strcmp(rb.name, "Methal") || rb.peers[1].rtt_ms != -1) return puts("beacon parse FAIL"), 1;

    gp_input_t in; memset(&in, 0, sizeof in);
    in.node = 3; in.seq = 1000; in.ts = 777; in.host = 1; in.flags = GP_I_IN | GP_I_ALIVE | (2 << 4);
    in.x = 12.34; in.y = 0.5; in.z = -3.21; in.yaw = RAD(270); in.pitch = RAD(-12.5); in.snap_ack = 4242; in.snap_hold_ms = 36;
    in.n_actions = 3;
    in.actions[0] = (gp_action_t){9, GP_A_FIRE, -120, RAD(271.5), RAD(-12.0), 4240, 1, 180, 1, -0.36, 1.2, GP_W_SNIPER};
    in.actions[1] = (gp_action_t){10, GP_A_RELOAD, -20, 0, 0, 0, 0, 0, 0, 0, 0, 0};
    in.actions[2] = (gp_action_t){11, GP_A_HEAL, -8, 0, 0, 0, 0, 0, 0, 0, 0, 0};
    n = gp_build_input(&in, buf); hex("input", buf, n);
    gp_input_t ri; if (gp_parse_input(buf, n, &ri) || ri.actions[0].dt_ms != -120 || ri.actions[0].view_frac != 180 || ri.actions[1].pos_known || ri.actions[0].weapon != GP_W_SNIPER || ri.actions[2].kind != GP_A_HEAL) return puts("input parse FAIL"), 1;

    gp_snapshot_t s; memset(&s, 0, sizeof s);
    s.node = 1; s.seq = 4243; s.ts = 60000; s.epoch = 7; s.max_players = 4; s.n_players = 2; s.n_events = 2;
    s.players[0] = (gp_splayer_t){1, 3.0, 0.0, 29.5, RAD(90), RAD(10), 100, GP_P_HOST, 5, 2, 7, 16, GP_W_KNIFE | (1 << 3)};
    s.players[1] = (gp_splayer_t){3, 12.34, 3.52, 30.0, RAD(270), RAD(-12.5), 40, GP_P_FIRED | (2 << 3), 1, 4, 0xE8, 8,
                                  GP_W_SNIPER | GP_G_HEALING | (3 << 3)};
    s.events[0] = (gp_event_t){77, 1, 3, 20, 0, 0, 0};
    s.events[1] = (gp_event_t){78, 1, 3, 150, 1, 1, GP_W_SNIPER};
    s.spots = 0x2D; s.n_drops = 1; s.drops[0] = (gp_drop_t){5, 12.5, 20.125};
    n = gp_build_snapshot(&s, buf); hex("snapshot", buf, n);
    gp_snapshot_t rs; if (gp_parse_snapshot(buf, n, &rs) || !rs.events[1].kill || rs.players[1].hp != 40 ||
                          rs.events[1].damage != 150 || rs.drops[0].z != 20.125 || rs.players[1].gear != s.players[1].gear)
        return puts("snapshot parse FAIL"), 1;
    puts("C self-test OK");
    return 0;
}
