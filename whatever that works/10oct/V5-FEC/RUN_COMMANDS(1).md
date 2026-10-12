# CDP link – run commands (two nodes)

Frequencies come from MY_ID inside integrationTest.grc – no TX_FREQ / RX_FREQ exports needed.

| Node | MY_ID | TL_LOCAL_ADDR | TL_PEER_ADDR | TX       | RX       |
|------|-------|---------------|--------------|----------|----------|
| A    | 0     | 1             | 2            | 1250 MHz | 1270 MHz |
| B    | 1     | 2             | 1            | 1270 MHz | 1250 MHz |

## Node A

    export MY_ID=0 TL_LOCAL_ADDR=1 TL_PEER_ADDR=2 TL_RTO_MS=1500
    gnuradio-companion integrationTest.grc &
    python3 cdp_chat.py --daemon

## Node B

    export MY_ID=1 TL_LOCAL_ADDR=2 TL_PEER_ADDR=1 TL_RTO_MS=1500
    gnuradio-companion integrationTest.grc &
    python3 cdp_chat.py --daemon

Then press Run (F6) in GRC on both nodes.

Notes
- MY_ID in the export needs the patched integrationTest.grc. With the original .grc,
  set the MY_ID variable to 0 on node A and 1 on node B inside GRC instead.
- If the chat sidebar shows "Sending to" an address other than the peer, a link.json from
  an earlier run is overriding TL_PEER_ADDR: fix it with "Change destination…" or `rm link.json`.
