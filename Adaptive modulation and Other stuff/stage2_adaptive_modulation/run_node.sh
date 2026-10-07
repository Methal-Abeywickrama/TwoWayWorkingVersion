#!/usr/bin/env bash
# Start one node of the CDP transceiver flowgraph (this stage).
#   ./run_node.sh A          node A (address 1) with its ANTSDR
#   ./run_node.sh B          node B (address 2) with its ANTSDR (other laptop)
#   ./run_node.sh A sim      no radio: run "A sim" and "B sim" on ONE PC (linked by ZMQ IQ)
# Anything you export yourself (TL_LOCAL_ADDR, TL_SDR_URI, TL_NOISE, ...) wins over these defaults.
set -e
SIDE=${1:-A}; MODE=${2:-hw}
HERE="$(cd "$(dirname "$0")" && pwd)"
case "$SIDE" in
  A) : "${TL_LOCAL_ADDR:=1}" "${TL_PEER_ADDR:=2}" ;;
  B) : "${TL_LOCAL_ADDR:=2}" "${TL_PEER_ADDR:=1}" ;;
  *) echo "usage: $0 A|B [hw|sim]"; exit 1 ;;
esac
# two nodes on one PC need different app (ZMQ message) ports
if [ "$SIDE" = B ] && [ "$MODE" = sim ]; then : "${TL_ZMQ_TX_PORT:=52011}" "${TL_ZMQ_RX_PORT:=52012}"; fi
export TL_LOCAL_ADDR TL_PEER_ADDR TL_ZMQ_TX_PORT TL_ZMQ_RX_PORT
case "$MODE" in
  hw)  cd "$HERE/generated_hw";  PY=transeciever.py ;;
  sim) cd "$HERE/generated_sim"; PY=transeciever_sim.py ;;
  *) echo "usage: $0 A|B [hw|sim]"; exit 1 ;;
esac
echo "node $SIDE ($MODE): addr $TL_LOCAL_ADDR -> peer $TL_PEER_ADDR, app ZMQ ${TL_ZMQ_TX_PORT:-52001}/${TL_ZMQ_RX_PORT:-52002}"
exec python3 -u "$PY"
