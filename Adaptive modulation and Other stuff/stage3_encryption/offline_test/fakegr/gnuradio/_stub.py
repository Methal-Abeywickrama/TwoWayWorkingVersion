"""Permissive placeholder objects for GRC parameter evaluation (no DSP happens here)."""
class Dummy:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return Dummy()
    def __getattr__(self, n):
        if n.startswith("__"): raise AttributeError(n)
        return Dummy()
    def base(self): return self
    def __repr__(self): return "Dummy()"


def module_getattr(name):
    if name.startswith("__"):
        raise AttributeError(name)
    return 0 if name.isupper() else Dummy()
