#!/usr/bin/env bash
# Start the chat window (+ folder_sync_daemon.py) for one node of this stage.
#   ./run_app.sh A [hw|sim]      ./run_app.sh B [hw|sim]
# Each node gets its own folder: node_A/ or node_B/ (outbox/, inbox/ inside).
set -e
SIDE=${1:-A}; MODE=${2:-hw}
HERE="$(cd "$(dirname "$0")" && pwd)"
case "$SIDE" in
  A) : "${TL_LOCAL_ADDR:=1}" "${TL_PEER_ADDR:=2}" ;;
  B) : "${TL_LOCAL_ADDR:=2}" "${TL_PEER_ADDR:=1}" ;;
  *) echo "usage: $0 A|B [hw|sim]"; exit 1 ;;
esac
if [ "$SIDE" = B ] && [ "$MODE" = sim ]; then : "${TL_ZMQ_TX_PORT:=52011}" "${TL_ZMQ_RX_PORT:=52012}"; fi
export TL_LOCAL_ADDR TL_PEER_ADDR TL_ZMQ_TX_PORT TL_ZMQ_RX_PORT
ROOT="$HERE/node_$SIDE"; mkdir -p "$ROOT"
exec python3 "$HERE/app/cdp_chat.py" --daemon --root "$ROOT" --name "node $SIDE" --peer "$TL_PEER_ADDR"
