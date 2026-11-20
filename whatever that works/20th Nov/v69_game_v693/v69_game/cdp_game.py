#!/usr/bin/env python3
"""
cdp_game.py - starts Radio Arena on the v6.9 cdp_link. cdp_daemon.py finds this file ("🎮 Start game") and runs it
with the session's environment:

    CDP_MY_ID         this node                     CDP_PEERS          nodes the daemon has heard so far, e.g. "1,3"
    CDP_UDP_PORT      5200 (datagrams to the radio)  CDP_UDP_OUT_PORT   5201 (datagrams from the radio)
    CDP_ARQ_PORT      5100 (reliable messages)       CDP_CTL_PORT       5300 ("Close everything")
    CDP_DEBUG_DIR     the session's log folder (the game's output goes to game.log there)

Optional, set before starting cdp_daemon.py:
    CDP_GAME_ARQ=1    send the inputs to the host as reliable messages (default 0: datagrams, see GAME_V69.md)
    CDP_GAME_ARGS     extra arguments for the game, e.g. "--fullscreen" or "--rate 4 --max-players 3"

Run by hand (flowgraph already running, same environment as above):
    CDP_MY_ID=0 CDP_PEERS=1 python3 cdp_game.py
"""
import os
import shlex
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.join(HERE, "game", "fps_game.py")


def main():
    if not os.path.exists(GAME):
        print(f"cdp_game.py: {GAME} is missing - copy the game/ folder next to cdp_daemon.py", flush=True)
        return 1
    args = [sys.executable, "-u", GAME, "--stack", "v69"]
    args += shlex.split(os.environ.get("CDP_GAME_ARGS", ""))
    args += sys.argv[1:]
    print("cdp_game.py: " + " ".join(shlex.quote(a) for a in args), flush=True)
    print(f"  node {os.environ.get('CDP_MY_ID', '?')}, nodes heard {os.environ.get('CDP_PEERS', '?') or '-'}, "
          f"udp {os.environ.get('CDP_UDP_PORT', '5200')}/{os.environ.get('CDP_UDP_OUT_PORT', '5201')}, "
          f"arq {os.environ.get('CDP_ARQ_PORT', '5100')}, ctl {os.environ.get('CDP_CTL_PORT', '5300')}, "
          f"CDP_GAME_ARQ={os.environ.get('CDP_GAME_ARQ', '(menu)')}", flush=True)
    os.chdir(os.path.join(HERE, "game"))
    os.execv(sys.executable, args)          # the daemon's "stop game" signal then reaches the game itself


if __name__ == "__main__":
    sys.exit(main())
