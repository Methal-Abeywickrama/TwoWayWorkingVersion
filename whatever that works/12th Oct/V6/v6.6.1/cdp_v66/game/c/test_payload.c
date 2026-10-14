/* test_payload.c - builds the same state as payload.py's self-test and prints the hex.
 *   gcc -O2 -Wall test_payload.c payload.c -o test_payload -lm && ./test_payload
 */
#include <math.h>
#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif
#include <stdio.h>
#include "payload.h"

int main(void)
{
    gp_state_t s = {0}, r;
    uint8_t buf[GP_LEN];
    s.pid = 1; s.seq = 65535;
    s.x = 12.34; s.y = 0.5; s.z = -3.21;
    s.yaw = 270.0 * M_PI / 180.0; s.pitch = -12.5 * M_PI / 180.0;
    s.hp = 73; s.ammo = 17; s.flags = GP_FLAG_FIRED | GP_FLAG_RELOADING; s.deaths = 4; s.aux = 9;
    s.n_events = 2;
    s.events[0] = (gp_event_t){500, 0, 20};
    s.events[1] = (gp_event_t){501, 0, 50};

    gp_build(&s, buf);
    for (int i = 0; i < GP_LEN; i++) printf("%02x", buf[i]);
    printf("\n");

    if (gp_parse(buf, GP_LEN, &r) != 0 || r.hp != 73 || r.seq != 65535 ||
        r.n_events != 2 || r.events[1].eid != 501) { puts("PARSE FAIL"); return 1; }
    puts("C self-test OK");
    return 0;
}
