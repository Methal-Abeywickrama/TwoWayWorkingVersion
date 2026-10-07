"""Pure-Python stand-in for GNU Radio's pmt module (only what this project's blocks use)."""


class P:
    __slots__ = ("kind", "val")

    def __init__(self, kind, val):
        self.kind, self.val = kind, val

    def __repr__(self):
        return f"pmt<{self.kind}:{self.val!r}>"

    def __eq__(self, other):
        return isinstance(other, P) and equal(self, other)

    def __hash__(self):
        return hash((self.kind, str(self.val)))


PMT_NIL = P("nil", None)
PMT_T = P("bool", True)
PMT_F = P("bool", False)
_NUM = ("long", "uint64", "double")


def intern(s): return P("sym", str(s))
string_to_symbol = intern
def symbol_to_string(p): return p.val
def is_symbol(p): return p.kind == "sym"
def is_null(p): return p.kind == "nil"
def is_bool(p): return p.kind == "bool"
def from_bool(b): return PMT_T if b else PMT_F
def to_bool(p): return bool(p.val)
def from_long(v): return P("long", int(v))
def from_uint64(v): return P("uint64", int(v) & 0xFFFFFFFFFFFFFFFF)
def from_double(v): return P("double", float(v))
def to_long(p): return int(p.val)
def to_uint64(p): return int(p.val)
def to_double(p): return float(p.val)
def is_integer(p): return p.kind == "long"
def is_uint64(p): return p.kind == "uint64"
def is_number(p): return p.kind in _NUM
def is_real(p): return p.kind == "double"
def cons(a, b): return P("pair", (a, b))
def is_pair(p): return p.kind == "pair"
def car(p): return p.val[0]
def cdr(p): return p.val[1]
def init_u8vector(n, data): return P("u8", bytes(int(x) & 0xFF for x in data))
def is_u8vector(p): return p.kind == "u8"
def u8vector_elements(p): return list(p.val)
def init_c32vector(n, data): return P("c32", [complex(x) for x in data])
def is_c32vector(p): return p.kind == "c32"
def c32vector_elements(p): return list(p.val)
def is_uniform_vector(p): return p.kind in ("u8", "c32")
def length(p): return len(p.val)
def make_dict(): return P("dict", {})
def is_dict(p): return p.kind == "dict"


def dict_add(d, k, v):
    nd = dict(d.val)
    nd[k.val] = (k, v)
    return P("dict", nd)


def dict_has_key(d, k): return k.val in d.val
def dict_ref(d, k, default): return d.val[k.val][1] if k.val in d.val else default


def dict_keys(d):
    out = PMT_NIL
    for k, _v in reversed(list(d.val.values())):
        out = cons(k, out)
    return out


def equal(a, b):
    if a.kind in _NUM and b.kind in _NUM:
        return a.val == b.val
    if a.kind != b.kind:
        return False
    if a.kind == "pair":
        return equal(a.val[0], b.val[0]) and equal(a.val[1], b.val[1])
    if a.kind == "dict":
        return a.val.keys() == b.val.keys() and all(equal(a.val[k][1], b.val[k][1]) for k in a.val)
    return a.val == b.val


eqv = eq = equal


def to_python(p):
    if p.kind == "list":
        return [to_python(x) for x in p.val]
    if p.kind == "dict":
        return {k: to_python(v) for k, (_kk, v) in p.val.items()}
    if p.kind == "pair":
        return (to_python(p.val[0]), to_python(p.val[1]))
    if p.kind == "u8":
        return list(p.val)
    return p.val


def to_pmt(x):
    if isinstance(x, P):
        return x
    if isinstance(x, bool):
        return from_bool(x)
    if isinstance(x, int):
        return from_long(x)
    if isinstance(x, float):
        return from_double(x)
    if isinstance(x, str):
        return intern(x)
    if x is None:
        return PMT_NIL
    raise TypeError(x)
