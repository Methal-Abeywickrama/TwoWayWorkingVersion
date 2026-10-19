"""Minimal gr.basic_block / gr.sync_block: the test driver sets the item counters and calls work() itself."""
import pmt


class Tag:
    __slots__ = ("offset", "key", "value", "srcid")

    def __init__(self, offset, key, value):
        self.offset, self.key, self.value, self.srcid = offset, key, value, None


class basic_block:
    def __init__(self, name="", in_sig=None, out_sig=None):
        self.name = name
        self.in_sig, self.out_sig = in_sig, out_sig
        self._history = 1
        self._nread = 0
        self._nwritten = 0
        self.in_tags = []           # tags on the input stream (absolute offsets), set by the driver
        self.out_tags = []          # tags added by the block: (offset, key str, value)
        self.published = []         # (port, msg)
        self._handlers = {}
        self._routes = {}           # out port -> [callables] (set by the test driver)

    # streams
    def set_history(self, h):
        self._history = int(h)

    def history(self):
        return self._history

    def nitems_read(self, port):
        return self._nread

    def nitems_written(self, port):
        return self._nwritten

    def add_item_tag(self, port, offset, key, value, srcid=None):
        self.out_tags.append((int(offset), pmt.symbol_to_string(key), value))

    def get_tags_in_window(self, port, rel_start, rel_end, key=None):
        a, b = self._nread + rel_start, self._nread + rel_end
        return [t for t in self.in_tags if a <= t.offset < b]

    def get_tags_in_range(self, port, a, b, key=None):
        return [t for t in self.in_tags if a <= t.offset < b]

    # messages
    def message_port_register_in(self, name):
        pass

    def message_port_register_out(self, name):
        pass

    def set_msg_handler(self, name, fn):
        self._handlers[pmt.symbol_to_string(name)] = fn

    def message_port_pub(self, port, msg):
        name = pmt.symbol_to_string(port)
        self.published.append((name, msg))
        for fn in self._routes.get(name, ()):
            fn(msg)

    def route(self, port, fn):
        """test driver: deliver what this block publishes on `port` to fn(msg), synchronously"""
        self._routes.setdefault(port, []).append(fn)


class sync_block(basic_block):
    pass
