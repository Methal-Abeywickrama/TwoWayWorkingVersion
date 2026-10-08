# COMMANDS — step by step

Everything is set in GRC. No environment variables, no scripts.

---------------------------------------------------------------------------------------------------
## 0. Once per laptop

```
sudo apt install python3-zmq python3-numpy python3-yaml      # usually already there with GNU Radio
unzip cdp_perf_suite.zip
cd cdp_perf_suite
```
Copy the same folder to both laptops.

---------------------------------------------------------------------------------------------------
## 1. Real hardware: two laptops, two ANTSDRs

### On BOTH laptops: open the flowgraph
```
gnuradio-companion perf_hw.grc
```
Set the two variables at the top of the canvas (double-click them):

| Laptop | MY_ID | PEER_ID |
|---|---|---|
| first  | 0 | 1 |
| second | 1 | 0 |

That is all you change. The rest follows from these two:
- **Frequencies.** The lower ID transmits on TX_FREQ_A (2412 MHz) and listens on TX_FREQ_B (2437 MHz). The other node swaps them automatically.
- **Addressing.** The framer, deframer, SR-ARQ and address filters all use MY_ID and PEER_ID.

If your ANTSDR is not at `ip:192.168.1.10`, change the `ADDR` variable.

Press **Run** (or F6). GRC writes `cdp_perf.py` next to the .grc. It does not touch your own `transeciever.py`.

### On BOTH laptops: start the perf daemon (second terminal, in the cdp_perf_suite folder)
First stop the chat app / `folder_sync_daemon.py` if they are running; they use the same ZMQ ports. Then:
```
python3 perf/perf_daemon.py
```
It finds MY_ID / PEER_ID by itself from the running flowgraph and prints:
```
node 0 -> peer 1; logging to .../perf_data/perf_logs/node0_<host>_<date>.jsonl
dashboard: http://127.0.0.1:8088/
```
Open **http://127.0.0.1:8088** in a browser on each laptop.

### Check that everything is connected (optional)
With the flowgraph running and the daemon **stopped**:
```
python3 perf/perf_daemon.py check
```
It lists what every tap delivered in 8 s: config, hb, rssi, link_tx, link_rx, evm, tp_tx, tp_rx, app_in, app_out.
`config`, `hb` and `rssi` must appear even when nothing is being sent.

---------------------------------------------------------------------------------------------------
## 2. One PC, no radios (simulation through ZMQ IQ)

Open both sim flowgraphs and run them. They already have different IDs and ports:
```
gnuradio-companion perf_sim_node0.grc        # MY_ID 0, ports 52001-52004
gnuradio-companion perf_sim_node1.grc        # MY_ID 1, ports 52011-52014
```
Two daemons:
```
python3 perf/perf_daemon.py --sim-node0      # dashboard http://127.0.0.1:8088
python3 perf/perf_daemon.py --sim-node1      # dashboard http://127.0.0.1:8089
```

---------------------------------------------------------------------------------------------------
## 3. Running tests

Start a test from the dashboard of the node that should **send**. The other node's daemon records
the receiving side; keep it running.

| Dashboard tab | Same thing from a terminal (while the daemon runs) |
|---|---|
| Full review (everything, ~6–10 min) | `python3 perf/perf_daemon.py submit plan --name full_review` |
| Quick check (~2 min) | `python3 perf/perf_daemon.py submit plan --name quick_check` |
| Link BER set | `python3 perf/perf_daemon.py submit link --set standard` |
| Link BER, custom | `python3 perf/perf_daemon.py submit link --sizes 64,256,1024 --count 40 --patterns random,zeros --load 0.8` |
| ARQ sweep | `python3 perf/perf_daemon.py submit arq --sizes 16,200,1000,5000 --patterns random --reps 3` |
| Idle baseline | `python3 perf/perf_daemon.py submit idle --duration 20` |
| Note | `python3 perf/perf_daemon.py submit mark --text "antennas 3 m apart, CH_GAIN 10"` |
| (status) | `python3 perf/perf_daemon.py status` |

Add `--sim-node1` to these commands when talking to the second simulated node.

Message sets (`perf/test_sets/`): `quick`, `standard`, `patterns`, `stress` (100 % load = max bit rate),
`long_soak`. To make your own, copy `my_set_example.json`, change `name` and the `items` (size ≥ 23 bytes, count,
pattern = random / zeros / ones / alt55 / altAA / text / ramp). It shows up in the dashboard at once.

Between tests you can change radio settings in GRC (gain, preamble, loop bandwidths...). Restart the flowgraph and add a **Note**; every report lists the parameters that were active.

---------------------------------------------------------------------------------------------------
## 4. Building the report (both PCs' data)

1. On the **receiving** laptop's dashboard, under *Logs and reports*, click **download** next to its log (marked ●).
   The log file is also at `perf_data/perf_logs/node1_<host>_<date>.jsonl`.
2. Copy that file to the sending laptop (USB stick, scp, ...).
3. On the **sending** laptop's dashboard: *Import peer log* → choose the file → tick both logs →
   **Build report from selected logs** → open the report link.

From a terminal instead:
```
python3 perf/perf_report.py perf_data/perf_logs/node0_*.jsonl /path/to/node1_*.jsonl -o my_report --title "Lab test 1"
xdg-open my_report/report.html
```
Output folder: `report.html` (open in any browser), `report.md`, `frames.csv`, `messages.csv`, `packets.csv`,
`summary.json`, `config.json`.

One log alone also works, giving the one-sided view.

---------------------------------------------------------------------------------------------------
## 5. Where things are

| | |
|---|---|
| `perf_data/perf_logs/` | one `.jsonl` log per daemon start (the start and end of the program are in it) |
| `perf_data/perf_logs/imported/` | logs imported from the other PC |
| `perf_data/perf_reports/` | reports built from the dashboard |
| `perf_data/perf_inbox/` | non-test files the other node sent (if any) |

Stop the daemon with Ctrl+C. It writes the end marker and the counters into its log.

---------------------------------------------------------------------------------------------------
## 6. If something does not work

| Symptom | Fix |
|---|---|
| Dashboard: "waiting for the flowgraph" | The flowgraph is not running, or ports differ. Run the flowgraph; for the one-PC sim use `--sim-node1` for node 1. |
| `Address already in use` in the flowgraph | Another flowgraph / chat daemon uses 52001-52004. Stop it. |
| ARQ messages all `timeout` / `tx_failed` | The other laptop's flowgraph is not running, MY_ID / PEER_ID are not mirrored (0/1 and 1/0), or there is no RF link (check the RX SNR tile on the other dashboard). |
| Link BER test: receiver shows nothing | Same as above; also check frequencies: one node must show TX 2412 / RX 2437, the other TX 2437 / RX 2412 (Flowgraph parameters card). |
| `check` shows no `link_rx` / `evm` | Nothing has been received yet; send a test from the other side. |
| Report says "sender log missing" | Import the other PC's log; for BER of failed frames the report needs the log of the PC that sent them. |
