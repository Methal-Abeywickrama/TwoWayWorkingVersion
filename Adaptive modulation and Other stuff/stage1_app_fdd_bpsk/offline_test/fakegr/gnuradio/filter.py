from ._stub import module_getattr as __getattr__  # noqa
import types as _t
firdes = _t.SimpleNamespace(root_raised_cosine=lambda gain, fs, sr, a, n: [0.0] * int(n),
                            low_pass=lambda *a, **k: [0.0] * 11, low_pass_2=lambda *a, **k: [0.0] * 11)
