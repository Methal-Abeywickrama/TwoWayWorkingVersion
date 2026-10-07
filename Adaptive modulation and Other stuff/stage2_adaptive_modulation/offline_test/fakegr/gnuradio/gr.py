"""Pure-Python stand-in for gnuradio.gr: enough for GRC's compiler/epy extraction and for
running embedded-python *message* blocks in a test harness (no stream scheduler)."""
import logging as _logging
import types as _types
import numpy as _np
import pmt as _pmt

_logging.basicConfig(level=_logging.INFO, format="%(message)s")
log = _types.SimpleNamespace(
    info=lambda m: _logging.getLogger("gr").info(m),
    warn=lambda m: _logging.getLogger("gr").warning("WARN " + str(m)),
    warning=lambda m: _logging.getLogger("gr").warning("WARN " + str(m)),
    error=lambda m: _logging.getLogger("gr").error("ERROR " + str(m)),
    debug=lambda m: None)


def version(): return "3.10.12.0"
def major_version(): return "3"
def api_version(): return "10"
def minor_version(): return "12"
def prefix(): return "/usr"


class _Prefs:
    def get_string(self, cat, item, default): return default
    def set_string(self, *a): pass
    def get_long(self, cat, item, default): return default
    def get_bool(self, cat, item, default): return default
    def save(self): pass


def prefs(): return _Prefs()


class top_block:
    def __init__(self, *a, **k): pass


TPP_DONT, TPP_ALL_TO_ALL, TPP_ONE_TO_ONE = 0, 1, 2


class _Block:
    def __init__(self, name="", in_sig=None, out_sig=None, *a, **k):
        self._name = name
        self._in = [_np.dtype(t) for t in (in_sig or [])]
        self._out = [_np.dtype(t) for t in (out_sig or [])]
        self._mp_in, self._mp_out, self._handlers, self._subs = [], [], {}, {}

    def name(self): return self._name
    def in_sig(self): return self._in
    def out_sig(self): return self._out
    def message_port_register_in(self, p): self._mp_in.append(p.val)
    def message_port_register_out(self, p): self._mp_out.append(p.val); self._subs.setdefault(p.val, [])
    def message_ports_in(self): return _pmt.P("list", [_pmt.intern(x) for x in ["system"] + self._mp_in])
    def message_ports_out(self): return _pmt.P("list", [_pmt.intern(x) for x in ["system"] + self._mp_out])
    def set_msg_handler(self, p, h): self._handlers[p.val] = h
    def set_tag_propagation_policy(self, p): pass

    def message_port_pub(self, p, msg):
        for fn in list(self._subs.get(p.val, [])):
            fn(msg)


import threading as _threading


def msg_connect(src, src_port, dst, dst_port):
    """Harness helper: wire src.src_port -> dst.dst_port. Delivery is synchronous, but each
    destination block handles one message at a time (like GNU Radio's per-block msg thread)."""
    if callable(dst):
        src._subs.setdefault(src_port, []).append(dst)
        return
    if not hasattr(dst, "_msg_lock"):
        dst._msg_lock = _threading.RLock()

    def deliver(m, d=dst, p=dst_port):
        with d._msg_lock:
            d._handlers[p](m)
    src._subs.setdefault(src_port, []).append(deliver)


gateway = _types.SimpleNamespace(gateway_block=_Block)
basic_block = sync_block = decim_block = interp_block = _Block
