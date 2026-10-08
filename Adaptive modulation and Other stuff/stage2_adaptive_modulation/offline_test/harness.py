"""
Two-node end-to-end test of a generated stage flowgraph, without GNU Radio or radios.

- Message graph: taken verbatim from the generated transeciever.py (msg_connect lines).
- Embedded Python blocks: the generated transeciever_epy_block_*.py files (real code).
- Non-epy message blocks: small fakes (pdu_filter, message_debug, ZMQ msg source/sink).
- Stream path (PDU -> modem -> RF -> demod -> deframer) replaced by one channel per direction
  (FDD): Stage 1 = bit errors on the BPSK bitstream; Stage 2/3 = symbol-level AWGN with gain,
  carrier phase drift and residual CFO. Air time is simulated in real time so the FSM's RTO
  timers see realistic delays.
- Application side: the real folder_sync_daemon.py frame format (build/parse_app_frame).
"""
import importlib.util
import os
import queue
import re
import sys
import threading
import time
import types

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "fakegr"))   # pure-Python stand-ins, this process only
import pmt  # noqa: E402  (stand-in)
from gnuradio import gr  # noqa: E402  (stand-in)

sys.modules.setdefault("zmq", types.ModuleType("zmq"))   # daemon only needs its frame helpers
_spec = importlib.util.spec_from_file_location(
    "folder_sync_daemon", os.path.join(HERE, "..", "app", "folder_sync_daemon.py"))
daemon = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(daemon)

SAMP_RATE, SPS = 1.5e6, 4
rng = np.random.default_rng(7)


# ------------------------------------------------------------------ fake non-epy blocks
class FakeBlock(gr.basic_block):
    def __init__(self):
        gr.basic_block.__init__(self, name=type(self).__name__)


class PduFilter(FakeBlock):
    def __init__(self, k, v, invert=False):
        super().__init__()
        self.k, self.v, self.invert = k, v, invert
        self.message_port_register_out(pmt.intern("pdus"))
        self.set_msg_handler(pmt.intern("pdus"), self.handle)

    def handle(self, msg):
        meta = pmt.car(msg)
        ok = pmt.is_dict(meta) and pmt.dict_has_key(meta, self.k) and \
            pmt.equal(pmt.dict_ref(meta, self.k, pmt.PMT_NIL), self.v)
        if ok != self.invert:
            self.message_port_pub(pmt.intern("pdus"), msg)


class MsgDebug(FakeBlock):
    def __init__(self, *a):
        super().__init__()
        for p in ("print", "print_pdu", "store"):
            self.set_msg_handler(pmt.intern(p), lambda m: None)


class ZmqSource(FakeBlock):
    def __init__(self, *a):
        super().__init__()
        self.message_port_register_out(pmt.intern("out"))


class ZmqSink(FakeBlock):
    def __init__(self, *a):
        super().__init__()
        self.got = queue.Queue()
        self.set_msg_handler(pmt.intern("in"), self.got.put)


class Radio(FakeBlock):
    """Stands in for pdu_to_tagged_stream -> ... -> Pluto sink. Collects TX bursts."""
    def __init__(self, *a):
        super().__init__()
        self.q = queue.Queue()
        self.set_msg_handler(pmt.intern("pdus"), lambda m: self.q.put(pmt.cdr(m)))


# ------------------------------------------------------------------ channels
class Link(threading.Thread):
    """One FDD direction: TX node's radio queue -> channel -> RX node's deframer."""
    def __init__(self, tx, rx, kind, quality):
        super().__init__(daemon=True)
        self.tx, self.rx, self.kind, self.quality = tx, rx, kind, quality
        self.ph = rng.uniform(0, 2 * np.pi)
        self.stop = False

    def run(self):
        dfr = self.rx.blocks["epy_block_0_0_0"]
        while not self.stop:
            try:
                vec = self.tx.radio.q.get(timeout=0.05)
            except queue.Empty:
                continue
            if self.kind == "bits":               # Stage 1: BPSK framer -> bytes
                bits = np.unpackbits(np.frombuffer(vec.val, np.uint8))
                flips = rng.random(len(bits)) < self.quality()
                x = (bits ^ flips).astype(np.uint8)
                airtime = len(bits) * SPS / SAMP_RATE
                gap = np.zeros(200, np.uint8)
            else:                                 # Stage 2/3: complex symbols
                s = np.array(vec.val, np.complex64)
                x = self.channel(s, self.quality())
                airtime = len(s) * SPS / SAMP_RATE
                gap = self.channel(np.zeros(200, np.complex64), self.quality())
            time.sleep(airtime)
            for k in range(0, len(x), 4096):
                dfr.work([x[k:k + 4096]], [])
            dfr.work([gap], [])

    def channel(self, s, esn0_db):
        n = len(s)
        ph = self.ph + 2 * np.pi * 2e-5 * np.arange(n) + np.cumsum(rng.normal(0, 0.002, n))
        self.ph = ph[-1]
        sigma = 2.0 / np.sqrt(10 ** (esn0_db / 10))
        noise = sigma / np.sqrt(2) * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
        return (2.0 * s * np.exp(1j * ph) + noise).astype(np.complex64)


# ------------------------------------------------------------------ node from generated code
class Node:
    def __init__(self, name, gen_dir, env):
        self.name = name
        py = open(os.path.join(gen_dir, "transeciever.py")).read()
        old_env = dict(os.environ)
        os.environ.update(env)
        try:
            ns = self._variables(py)
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        self.ns = ns
        self.blocks = {}
        for m in re.finditer(r"self\.(epy_block_\w+) = (epy_block_\w+)\.(\w+)\((.*)\)\n", py):
            var, mod, cls, args = m.groups()
            module = self._load(gen_dir, f"transeciever_{mod}")
            self.blocks[var] = eval(f"module.{cls}({args})", {"module": module, **ns})
        for m in re.finditer(r"self\.(pdu_pdu_filter_\w+) = pdu\.pdu_filter\((.*)\)\n", py):
            self.blocks[m.group(1)] = eval(f"PduFilter({m.group(2)})", {"PduFilter": PduFilter, "pmt": pmt, **ns})
        for m in re.finditer(r"self\.(blocks_message_debug_\w+) = ", py):
            self.blocks[m.group(1)] = MsgDebug()
        self.blocks["zeromq_pull_msg_source_0"] = self.app_in = ZmqSource()
        self.blocks["zeromq_push_msg_sink_0"] = self.app_out = ZmqSink()
        self.blocks["pdu_pdu_to_tagged_stream_0_0"] = self.radio = Radio()
        self.wires = re.findall(r"self\.msg_connect\(\(self\.(\w+), '(\w+)'\), \(self\.(\w+), '(\w+)'\)\)", py)
        for a, ap, b, bp in self.wires:
            gr.msg_connect(self.blocks[a], ap, self.blocks[b], bp)

    @staticmethod
    def _variables(py):
        body = py.split("# Variables")[1].split("# Blocks")[0]
        lines = [ln[8:] for ln in body.splitlines() if ln.startswith("        ") and not ln.strip().startswith("#")]
        ns = {"self": types.SimpleNamespace()}
        exec("import os\nfrom gnuradio import digital\n" + "\n".join(lines), ns)
        ns.pop("self")
        return ns

    _mods = {}

    @classmethod
    def _load(cls, gen_dir, modname):
        key = (gen_dir, modname)
        if key not in cls._mods:
            spec = importlib.util.spec_from_file_location(modname, os.path.join(gen_dir, modname + ".py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            cls._mods[key] = mod
        return cls._mods[key]

    def send_file(self, peer, name, data, port=0):
        frame = daemon.build_app_frame(peer, name, data, dst_port=port)
        self.app_in.message_port_pub(pmt.intern("out"), pmt.cons(pmt.make_dict(), pmt.init_u8vector(len(frame), frame)))

    def wait_app(self, timeout):
        """Next app_out message: ('event', name) or ('file', name, data, src_addr)."""
        try:
            msg = self.app_out.got.get(timeout=timeout)
        except queue.Empty:
            return None
        meta, data = pmt.car(msg), pmt.cdr(msg)
        if pmt.dict_has_key(meta, pmt.intern("event")):
            return ("event", pmt.symbol_to_string(pmt.dict_ref(meta, pmt.intern("event"), pmt.PMT_NIL)))
        frame = bytes(pmt.u8vector_elements(data))
        try:
            name, content = daemon.parse_app_frame(frame)
        except ValueError as e:
            name, content = f"<unparseable: {e}>", frame[8:]
        return ("file", name, content, daemon.meta_int(meta, "src_addr"))


def make_pair(gen_dir, env_a=None, env_b=None):
    a = Node("A", gen_dir, {"TL_LOCAL_ADDR": "1", "TL_PEER_ADDR": "2", **(env_a or {})})
    b = Node("B", gen_dir, {"TL_LOCAL_ADDR": "2", "TL_PEER_ADDR": "1", **(env_b or {})})
    return a, b


def transfer(src, dst, name, data, timeout=60):
    """Send one file src->dst. Returns (event at sender, what dst received, seconds)."""
    t0 = time.time()
    src.send_file(dst.ns["tl_local_addr"], name, data)
    got, ev = None, None
    end = t0 + timeout
    while time.time() < end and (got is None or ev is None):
        if ev is None:
            r = src.wait_app(0.05)
            if r and r[0] == "event":
                ev = r[1]
                if ev != "tx_done":
                    break
        if got is None:
            r = dst.wait_app(0.05)
            if r and r[0] == "file":
                got = r
    return ev, got, time.time() - t0
