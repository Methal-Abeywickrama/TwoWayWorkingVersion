from ._stub import Dummy, module_getattr as __getattr__  # noqa


class _Const:
    def __init__(self, points=(), *a, **k): self._p = list(points)
    def arity(self): return len(self._p)
    def points(self): return self._p
    def base(self): return self
    def bits_per_symbol(self): return max(1, (len(self._p) - 1).bit_length())
    def rotational_symmetry(self): return len(self._p)
    def dimensionality(self): return 1


def constellation_rect(points, *a, **k): return _Const(points)
def constellation_calcdist(points, *a, **k): return _Const(points)
def constellation_bpsk(): return _Const([-1, 1])
def constellation_qpsk(): return _Const([1, 1j, -1, -1j])
def constellation_8psk(): return _Const(range(8))
def adaptive_algorithm_cma(*a, **k): return Dummy()
def adaptive_algorithm_lms(*a, **k): return Dummy()
def adaptive_algorithm_nlms(*a, **k): return Dummy()
TED_SIGNAL_TIMES_SLOPE_ML, TED_MUELLER_AND_MULLER, TED_GARDNER = 2, 0, 4
IR_MMSE_8TAP, IR_PFB_NO_MF, IR_PFB_MF = 0, 1, 2
DIFF_DIFFERENTIAL, DIFF_NRZI = 0, 1
THRESHOLD_DYNAMIC, THRESHOLD_ABSOLUTE = 0, 1
