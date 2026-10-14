# cdp_chat_v66.py: chat app for the v6.6 flowgraph

A single file. It connects straight to the flowgraph's ARQ and UDP transport blocks, so no separate daemon is needed. It needs PyQt5, which comes with GNU Radio's Qt GUI.

## Start each node

**Node A**

```bash
export CDP_MY_ID=2
gnuradio-companion v6.6_csma_transport.grc &
python3 cdp_chat_v66.py --name Methal
```

**Node B**

```bash
export CDP_MY_ID=5
gnuradio-companion v6.6_csma_transport.grc &
python3 cdp_chat_v66.py --name Peer
```

You can start the app before or after the flowgraph. It reconnects by itself every 2 s, and messages you type while the radio isn't reachable wait as "waiting for the radio".

## Using it

**#everyone** sends to every node over UDP broadcast.
- You get no delivery confirmation: ✓ only means it was sent.
- Text only, up to about 1,800 characters.

**@name · ID** sends to one node through the ARQ block.
- ✓ means the other node confirmed it. ✗ means not delivered, with a reason and a retry link.
- You can send files up to about 4 MB with 📎 or by drag and drop. They are saved in the receiver's `~/cdp_inbox`.

**Other nodes appear in the sidebar on their own.** That happens when they send something, or when their presence beacon is heard (a short broadcast every 60 s). "+ Add node" adds one by its ID.

**If the flowgraph stops while a message is in flight**, the message is marked ⚠ with a resend link. Resending never creates a duplicate at the receiver.

**History** is kept in `~/.cdp_chat/history_node<ID>.jsonl` and reloads when you restart the app.

**Show log** (bottom left) shows connections, failures and received messages.

## Options and settings

| Option / variable | Default | Meaning |
|---|---|---|
| `--name` / `CDP_CHAT_NAME` | `node <ID>` | your display name |
| `--inbox DIR` | `~/cdp_inbox` | where received files are saved |
| `--history FILE` | `~/.cdp_chat/history_node<ID>.jsonl` | chat history |
| `--beacon-s S` | 60 | presence beacon period, 0 = off |
| `CDP_HOST`, `CDP_ARQ_PORT`, `CDP_UDP_PORT`, `CDP_UDP_OUT_PORT`, `CDP_UDP_BIND` | 127.0.0.1, 5100, 5200, 5201, 127.0.0.1 | Must match the flowgraph's settings. |

## Notes

- **The GRC "TX Message" box and this app don't see each other's messages.** Typed GRC messages aren't transport packets, so they only appear in the GRC console.
- **Messages from other programs show up too.** Text or files sent with `cdp_sock.py arq-send` appear in the sender's conversation.
- **Only one program per PC can receive broadcasts on UDP port 5201.** Don't run `cdp_sock.py udp-listen` at the same time as the app.
