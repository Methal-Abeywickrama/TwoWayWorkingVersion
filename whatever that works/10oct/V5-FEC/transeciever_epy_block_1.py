"""
Embedded Python Block: Test Traffic RX  (receive logger)

Connect:  Packet Deframer RX  pdu_out  ->  pdu_in

Every frame the deframer delivers is written to  <log_dir>/rx_<date>_<time>.csv  with a wall-clock
timestamp (microseconds), the test run_id / seq when the payload carries the test header (A5 5A ...),
the deframer's msg_id, and the payload as hex. Non-test frames (chat etc.) are logged with is_test = 0.
One file per flowgraph start; rows are flushed immediately, so the log survives a crash or Ctrl-C.
Prints a running count per test run (received, missing so far, delivery %).
The deframer only delivers CRC-good frames, so this log holds correct packets only; losses are
found by the analysis script from the gaps against the TX log.
"""
import csv
import datetime
import os
import socket
import time

import pmt
from gnuradio import gr

MAGIC = b"\xA5\x5A"
RX_FIELDS = ["rx_time_us", "rx_time_iso", "is_test", "run_id", "seq", "size", "msg_id", "host", "payload_hex"]


class TestTrafficRX(gr.basic_block):
    def __init__(self, log_dir="~/link_tests", verbose=1, print_every=10):
        gr.basic_block.__init__(self, name="Test Traffic RX", in_sig=None, out_sig=None)
        self.log_dir = str(log_dir)
        self.verbose = int(verbose)
        self.print_every = max(1, int(print_every))
        self._host = socket.gethostname()
        self._f = None
        self._w = None
        self._path = None
        self._runs = {}            # run_id -> {"seqs": set, "max": int, "dups": int}
        self._n_other = 0

        self.message_port_register_in(pmt.intern("pdu_in"))
        self.set_msg_handler(pmt.intern("pdu_in"), self._handle)

    # ---------------------------------------------------------------- file
    def start(self):
        self._open()
        return True

    def stop(self):
        self._summary()
        if self._f:
            self._f.close()
            self._f = None
        return True

    def _open(self):
        if self._f:
            return
        d = os.path.expanduser(self.log_dir)
        os.makedirs(d, exist_ok=True)
        self._path = os.path.join(d, f"rx_{datetime.datetime.now():%Y%m%d_%H%M%S}.csv")
        self._f = open(self._path, "w", newline="")
        self._w = csv.writer(self._f)
        self._w.writerow(RX_FIELDS)
        self._f.flush()
        print(f"[TEST RX] logging received frames to {self._path}", flush=True)

    # ---------------------------------------------------------------- messages
    def _handle(self, msg):
        t_us = time.time_ns() // 1000
        meta = pmt.PMT_NIL
        try:
            data = msg
            if pmt.is_pair(msg):
                meta, data = pmt.car(msg), pmt.cdr(msg)
            if pmt.is_u8vector(data):
                payload = bytes(pmt.u8vector_elements(data))
            elif pmt.is_symbol(data):
                payload = pmt.symbol_to_string(data).encode("utf-8")
            else:
                payload = str(pmt.to_python(data)).encode("utf-8")
        except Exception as e:
            print(f"[TEST RX] could not parse message: {e!r}", flush=True)
            return

        msg_id = -1
        try:
            if pmt.is_dict(meta):
                v = pmt.dict_ref(meta, pmt.intern("msg_id"), pmt.PMT_NIL)
                if pmt.is_integer(v):
                    msg_id = pmt.to_long(v)
        except Exception:
            pass

        is_test = len(payload) >= 8 and payload[:2] == MAGIC
        run_id = int.from_bytes(payload[2:4], "big") if is_test else -1
        seq = int.from_bytes(payload[4:8], "big") if is_test else -1

        self._open()
        self._w.writerow([t_us,
                          datetime.datetime.fromtimestamp(t_us / 1e6).isoformat(timespec="milliseconds"),
                          int(is_test), run_id, seq, len(payload), msg_id, self._host, payload.hex()])
        self._f.flush()

        if not is_test:
            self._n_other += 1
            return
        st = self._runs.setdefault(run_id, {"seqs": set(), "max": -1, "dups": 0})
        if seq in st["seqs"]:
            st["dups"] += 1
        st["seqs"].add(seq)
        st["max"] = max(st["max"], seq)
        n = len(st["seqs"])
        if self.verbose and (n == 1 or n % self.print_every == 0):
            expected = st["max"] + 1
            print(f"[TEST RX] run {run_id:04x}: {n} received, last seq {seq}, {expected - n} missing so far "
                  f"({100.0 * n / expected:.1f}% of seq 0..{st['max']})", flush=True)

    def _summary(self):
        for run_id, st in self._runs.items():
            n, expected = len(st["seqs"]), st["max"] + 1
            print(f"[TEST RX] run {run_id:04x} total: {n} received of seq 0..{st['max']} "
                  f"({100.0 * n / max(expected, 1):.1f}%), {st['dups']} duplicates", flush=True)
        if self._n_other:
            print(f"[TEST RX] {self._n_other} non-test frames logged", flush=True)
        if self._path:
            print(f"[TEST RX] log: {self._path}", flush=True)