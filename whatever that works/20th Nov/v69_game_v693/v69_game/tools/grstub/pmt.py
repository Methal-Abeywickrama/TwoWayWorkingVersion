"""Minimal PMT stand-in (Python objects)."""


class _Sym(str):
    pass


class _Pair(tuple):
    pass


class _Dict(dict):
    pass


class _U8(bytes):
    pass


PMT_NIL = None
PMT_T = True
PMT_F = False


def intern(s):
    return _Sym(s)


string_to_symbol = intern


def symbol_to_string(s):
    return str(s)


def is_symbol(x):
    return isinstance(x, _Sym)


def from_double(x):
    return float(x)


def to_double(x):
    return float(x)


def from_long(x):
    return int(x)


def to_long(x):
    return int(x)


def is_integer(x):
    return isinstance(x, int) and not isinstance(x, bool)


def from_bool(x):
    return bool(x)


def make_dict():
    return _Dict()


def dict_add(d, k, v):
    n = _Dict(d)
    n[k] = v
    return n


def dict_ref(d, k, default):
    return d.get(k, default) if isinstance(d, dict) else default


def is_dict(x):
    return isinstance(x, dict)


def cons(a, b):
    return _Pair((a, b))


def car(p):
    return p[0]


def cdr(p):
    return p[1]


def is_pair(x):
    return isinstance(x, _Pair)


def is_null(x):
    return x is None


def init_u8vector(n, data):
    return _U8(bytes(data[:n]))


def is_u8vector(x):
    return isinstance(x, _U8)


def u8vector_elements(v):
    return list(v)


def length(x):
    return len(x)


def list2(a, b):
    return [a, b]


def make_tuple(*a):
    return tuple(a)


def to_python(x):
    return x


def from_python(x):
    return x
