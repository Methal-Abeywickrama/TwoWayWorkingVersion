#!/usr/bin/env python3
"""
cdp_fast.py - loads the C++ kernels (cdp_fast.cpp) for the flowgraph's Python blocks.

The first use compiles cdp_fast.cpp with g++ (-O3 -march=native, about 2 s) into ~/.cache/cdp_fast/ and checks
the result against the NumPy code it replaces. Later starts load the cached library. If g++ is missing, the
build fails, or the check finds a difference, the blocks keep using their original NumPy code: nothing breaks,
it is only slower.

    python3 cdp_fast.py            build (if needed) + self-test + speed comparison
    CDP_FAST=0                     switch the kernels off (the blocks run exactly the original Python code)
    CDP_FAST_CXX=clang++           another compiler; CDP_FAST_FLAGS="-O3" to drop -march=native

Fedora: sudo dnf install gcc-c++
"""
import ctypes
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
SRC = HERE / "cdp_fast.cpp"
VERSION = 9
_lock = threading.RLock()          # re-entrant: the self-test calls lib() again
_lib = None
_why = None
_tried = False

# Python >= 3.12 sums floats with Neumaier's compensation; the MAC's smoothing kernel copies whichever this is
_PY_SUM_COMPENSATED = 1 if sum([1.0, 1e100, 1.0, -1e100]) == 2.0 else 0

# buffers go to C as plain addresses (a.ctypes.data: ~3x cheaper than data_as); the arrays stay referenced by
# the caller for the duration of the call
_f32 = _f64 = _u8 = _i32 = ctypes.c_void_p


def _p(a, t=None):
    return a.ctypes.data


def enabled():
    return os.environ.get("CDP_FAST", "1").strip().lower() not in ("0", "no", "off", "false")


def _cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    for d in (Path(base) / "cdp_fast", HERE / ".cdp_fast_build", Path(tempfile.gettempdir()) / f"cdp_fast_{os.getuid()}"):
        try:
            d.mkdir(parents=True, exist_ok=True)
            if os.access(d, os.W_OK):
                return d
        except OSError:
            continue
    return None


def _cpu_id():
    """the CPU model and its instruction-set flags: -march=native code only runs on the CPU it was built on"""
    try:
        txt = Path("/proc/cpuinfo").read_text()
        keep = [l for l in txt.splitlines() if l.startswith(("model name", "flags", "Features", "CPU part"))]
        return "\n".join(sorted(set(keep)))
    except OSError:
        return platform.processor() or platform.machine()


def _build():
    """-> path of the compiled library, or raises RuntimeError with a short reason."""
    if not SRC.is_file():
        raise RuntimeError(f"{SRC.name} not found next to cdp_fast.py")
    cxx = os.environ.get("CDP_FAST_CXX", "g++")
    if shutil.which(cxx) is None:
        raise RuntimeError(f"'{cxx}' not found (Fedora: sudo dnf install gcc-c++)")
    flags = os.environ.get("CDP_FAST_FLAGS", "-O3 -march=native").split()
    src = SRC.read_bytes()
    tag = hashlib.sha1(src + " ".join([cxx] + flags).encode() + platform.machine().encode()
                       + _cpu_id().encode()).hexdigest()[:12]
    d = _cache_dir()
    if d is None:
        raise RuntimeError("no writable folder for the compiled library")
    so = d / f"libcdp_fast_v{VERSION}_{tag}.so"
    if so.is_file():
        return so
    for fl in (flags, [f for f in flags if not f.startswith("-march")]):
        tmp = so.with_suffix(f".{os.getpid()}.tmp")
        cmd = [cxx, *fl, "-std=c++17", "-shared", "-fPIC", "-o", str(tmp), str(SRC)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except Exception as e:
            raise RuntimeError(f"compiler did not run: {e}")
        if r.returncode == 0:
            os.replace(tmp, so)
            for f in d.glob("libcdp_fast_v*.so"):          # libraries of older versions of cdp_fast.cpp
                if not f.name.startswith(f"libcdp_fast_v{VERSION}_"):
                    try:
                        f.unlink()
                    except OSError:
                        pass
            return so
        err = (r.stderr or r.stdout).strip().splitlines()
        last = err[-1] if err else f"exit {r.returncode}"
        try:
            tmp.unlink()
        except OSError:
            pass
    raise RuntimeError(f"compile failed: {last}")


def _bind(lib):
    lib.cdp_fast_version.restype = ctypes.c_int
    lib.cdp_cs_new.restype = ctypes.c_void_p
    lib.cdp_cs_new.argtypes = [ctypes.c_int, _f32, _i32, ctypes.c_int, ctypes.c_double]
    lib.cdp_cs_free.argtypes = [ctypes.c_void_p]
    lib.cdp_cs_run.restype = ctypes.c_int
    lib.cdp_cs_run.argtypes = [ctypes.c_void_p, _f32, ctypes.c_int, _f64]
    lib.cdp_smooth_means.restype = ctypes.c_int
    lib.cdp_smooth_means.argtypes = [_f64, ctypes.c_int, ctypes.c_int, _f64, ctypes.c_int, _f64, ctypes.c_int]
    D, I = ctypes.c_double, ctypes.c_int
    lib.cdp_mac_steps.restype = I
    lib.cdp_mac_steps.argtypes = [_f64, I, I, ctypes.c_longlong, I, D, I, D, D, D, I, D, D, D, D, I, D, D,
                                  _f64, _i32, _i32]
    lib.cdp_mac_air.restype = I
    lib.cdp_mac_air.argtypes = [_f64, I, I, ctypes.c_longlong, I, D, I, D, D, D, D, D, _f64]
    lib.cdp_corr_metric.restype = ctypes.c_int
    lib.cdp_corr_metric.argtypes = [_f32, ctypes.c_int, _f32, ctypes.c_int, ctypes.c_double, _f64,
                                    _f32, _f32, _f64, _f32, _f32, _f64]
    lib.cdp_corr_peaks.restype = ctypes.c_int
    lib.cdp_corr_peaks.argtypes = [_f64, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_double, _i32, _f64]
    lib.cdp_find_sync.restype = ctypes.c_int
    lib.cdp_find_sync.argtypes = [_u8, ctypes.c_int, ctypes.c_int, ctypes.c_uint32, ctypes.c_int]
    lib.cdp_hamming_dec.restype = ctypes.c_int
    lib.cdp_hamming_dec.argtypes = [_u8, ctypes.c_int, ctypes.c_int, ctypes.c_int, _u8]
    lib.cdp_noise_slices.restype = ctypes.c_int
    lib.cdp_noise_slices.argtypes = [_f32, ctypes.c_int, ctypes.c_int, _f64, _i32, _f64]
    if lib.cdp_fast_version() != VERSION:
        raise RuntimeError("library version mismatch")


def lib():
    """-> the loaded and self-tested library, or None (then the blocks use their NumPy code). Thread-safe."""
    global _lib, _why, _tried
    if not enabled():
        _why = "switched off (CDP_FAST=0)"
        return None
    with _lock:
        if _tried:
            return _lib
        _tried = True
        try:
            path = _build()
            L = ctypes.CDLL(str(path))          # CDLL: the GIL is released during every call
            _bind(L)
            _lib = L
            bad = _selftest()
            if bad:
                _lib = None
                raise RuntimeError(f"self-test failed ({bad})")
        except Exception as e:
            _lib, _why = None, str(e)
        return _lib


def why():
    return _why


def status(block):
    """one line for the flowgraph's console"""
    if lib() is not None:
        return f"[FAST] {block}: C++ kernel on"
    return f"[FAST] {block}: original Python code ({why()})"


# ================================================================================ wrappers used by the blocks
class CsFront:
    """MAC carrier sense: median(|FFT(win * block)|^2 over bins) * norm for each nfft-sample block."""

    def __init__(self, nfft, win, bins, norm):
        self.L = lib()
        self.h = None
        if self.L is None:
            return
        self._win = np.ascontiguousarray(win, np.float32)
        self._bins = np.ascontiguousarray(bins, np.int32)
        self.nfft = int(nfft)
        h = self.L.cdp_cs_new(self.nfft, _p(self._win, _f32), _p(self._bins, _i32), len(self._bins), float(norm))
        self.h = h or None

    @property
    def ok(self):
        return self.h is not None

    def __call__(self, buf, nb):
        x = np.ascontiguousarray(buf[:nb * self.nfft], np.complex64)
        out = np.empty(nb, np.float64)
        self.L.cdp_cs_run(self.h, _p(x, _f32), nb, _p(out, _f64))
        return out

    def smooth_db(self, smooth, med):
        """the MAC's cs_db for each new median: 10 log10(max(mean of the last maxlen medians, 1e-20)), where
        `smooth` is the block's deque before these medians. Same sums as Python's sum(deque), same np.log10."""
        prev = np.fromiter(smooth, np.float64, len(smooth))
        med = np.ascontiguousarray(med, np.float64)
        means = np.empty(len(med), np.float64)
        self.L.cdp_smooth_means(_p(prev, _f64), len(prev), int(smooth.maxlen), _p(med, _f64), len(med),
                                _p(means, _f64), _PY_SUM_COMPENSATED)
        return 10.0 * np.log10(np.maximum(means, 1e-20))

    PHASES = {"persist": 0, "difs": 1, "backoff": 2}

    def steps(self, cs_db, i0, first_abs, nfft, samp_rate, floor_db, margin, hyst, busy, nav_until, deadline,
              floor_next, dt, contend, slot, difs, st, ist):
        """MAC steps without side effects (see cdp_mac_steps). st: 11 floats, ist: 3 ints (in/out arrays).
        -> (steps done, counts [busy, NAV, medium busy, medium busy with NAV])"""
        c = np.zeros(4, np.int32)
        k = self.L.cdp_mac_steps(_p(cs_db, _f64), len(cs_db), int(i0), int(first_abs), int(nfft), float(samp_rate),
                                 0 if floor_db is None else 1, 0.0 if floor_db is None else float(floor_db),
                                 float(margin), float(hyst), 1 if busy else 0, float(nav_until), float(deadline),
                                 float(floor_next), float(dt), 1 if contend else 0, float(slot), float(difs),
                                 _p(st, _f64), _p(ist, _i32), _p(c, _i32))
        return k, c

    def air(self, cs_db, i0, first_abs, nfft, samp_rate, echo_none, echo_thr, win_end, t0, floor_next, dt, st):
        """MAC steps while the own burst is on the air (see cdp_mac_air). st: [tx_s, t_last] in/out"""
        return self.L.cdp_mac_air(_p(cs_db, _f64), len(cs_db), int(i0), int(first_abs), int(nfft), float(samp_rate),
                                  1 if echo_none else 0, float(echo_thr), float(win_end), float(t0),
                                  float(floor_next), float(dt), _p(st, _f64))

    def __del__(self):
        try:
            if self.h:
                self.L.cdp_cs_free(self.h)
        except Exception:
            pass


class CorrMetric:
    """correlator: m[n] = |sum conj(ref) x[n:n+L]|^2 / (E * sum |x[n:n+L]|^2 + 1e-12)"""

    def __init__(self, ref, E):
        self.L = lib()
        self.ref = np.ascontiguousarray(ref, np.complex64)
        self.E = float(E)
        self._cap = 0

    @property
    def ok(self):
        return self.L is not None

    def _grow(self, n):
        if n > self._cap:
            self._cap = max(n, 2 * self._cap, 4096)
            c = self._cap
            self._xr, self._xi, self._cr, self._ci = (np.empty(c, np.float32) for _ in range(4))
            self._p, self._w = np.empty(c + 1, np.float64), np.empty(c, np.float64)

    def __call__(self, x):
        x = np.ascontiguousarray(x, np.complex64)
        n, L = len(x), len(self.ref)
        if n < L:
            return np.zeros(0, np.float64)
        self._grow(n)
        m = np.empty(n - L + 1, np.float64)
        self.L.cdp_corr_metric(_p(x, _f32), n, _p(self.ref, _f32), L, self.E, _p(m, _f64),
                               _p(self._xr, _f32), _p(self._xi, _f32), _p(self._p, _f64),
                               _p(self._cr, _f32), _p(self._ci, _f32), _p(self._w, _f64))
        return m

    def peaks(self, m, n0, n1, thr):
        """-> (indices j - n0 where the block's peak condition holds for j in [n0, n1], max of m)"""
        idx = np.empty(max(0, n1 - n0 + 1), np.int32)
        mx = np.zeros(1, np.float64)
        c = self.L.cdp_corr_peaks(_p(m), len(m), int(n0), int(n1), float(thr), _p(idx), _p(mx))
        return idx[:c], float(mx[0])


def find_sync(bits, start, sync_word, max_err):
    """bits: uint8 0/1 array. -> first index >= start whose 32 bits differ from sync_word in <= max_err bits, or -1"""
    L = lib()
    b = np.ascontiguousarray(bits, np.uint8)
    return L.cdp_find_sync(_p(b, _u8), len(b), int(start), int.from_bytes(sync_word, "big"), int(max_err))


def hamming_decode_bits(coded, n, nblk, interleave):
    """same result as the deframer's NumPy hamming_decode_bits: (data bits uint8, corrected blocks)"""
    L = lib()
    c = np.ascontiguousarray(coded, np.uint8)
    k = {15: 11, 31: 26}[n]
    out = np.empty(nblk * k, np.uint8)
    corr = L.cdp_hamming_dec(_p(c, _u8), int(n), int(nblk), 1 if interleave else 0, _p(out, _u8))
    return out, int(corr)


class NoiseSlices:
    """noise tracker: mean |x|^2 of each `slice` samples, a partial slice carried to the next call"""

    def __init__(self, slice_len):
        self.L = lib()
        self.slice = int(slice_len)
        self._acc = np.zeros(1, np.float64)
        self._cnt = np.zeros(1, np.int32)

    @property
    def ok(self):
        return self.L is not None

    def __call__(self, x):
        x = np.ascontiguousarray(x, np.complex64)
        out = np.empty(len(x) // self.slice + 2, np.float64)
        k = self.L.cdp_noise_slices(_p(x, _f32), len(x), self.slice, _p(self._acc, _f64), _p(self._cnt, _i32),
                                    _p(out, _f64))
        return out[:k]


# ================================================================================ self-test (against NumPy)
def _ref_cs(buf, nb, nfft, win, bins, norm):
    blocks = buf[:nb * nfft].reshape(nb, nfft) * win
    p = np.abs(np.fft.fft(blocks, axis=1)[:, bins]) ** 2
    return np.median(p, axis=1) * norm


def _ref_corr(x, ref, E):
    L = len(ref)
    c = np.correlate(x, ref, "valid")
    w = np.convolve(np.abs(x).astype(np.float64) ** 2, np.ones(L), "valid")
    return (np.abs(c).astype(np.float64) ** 2) / (E * w + 1e-12)


def _ref_hamming(coded, n, nblk, interleave):
    m = {15: 4, 31: 5}[n]
    pos = np.arange(1, n + 1)
    data_idx = np.flatnonzero(~((pos & (pos - 1)) == 0))
    H = ((pos[None, :] >> np.arange(m)[:, None]) & 1).astype(np.int32)
    weights = (1 << np.arange(m)).astype(np.int32)
    c = np.asarray(coded, dtype=np.int32)
    cw = c.reshape(n, nblk).T.copy() if interleave else c.reshape(nblk, n).copy()
    S = ((cw @ H.T) & 1) @ weights
    bad = np.flatnonzero(S)
    cw[bad, S[bad] - 1] ^= 1
    return cw[:, data_idx].reshape(-1).astype(np.uint8), int(bad.size)


def _selftest():
    """-> '' if every kernel matches the NumPy code, else what differs"""
    rng = np.random.default_rng(7)
    # carrier sense
    nfft, sr = 256, 1e6
    f = np.fft.fftfreq(nfft, 1.0 / sr)
    bins = np.flatnonzero((np.abs(f) <= 200e3) & (np.abs(f) >= 10e3))
    win = np.hanning(nfft).astype(np.float32)
    norm = 1.0 / (np.log(2.0) * nfft * float(np.mean(win ** 2)))
    x = ((rng.standard_normal(nfft * 40) + 1j * rng.standard_normal(nfft * 40)) * 1e-3).astype(np.complex64)
    x[nfft * 10:nfft * 20] *= 300
    cs = CsFront(nfft, win, bins, norm)
    if not cs.ok:
        return "carrier sense init"
    a, b = cs(x, 40), _ref_cs(x, 40, nfft, win, bins, norm)
    if np.max(np.abs(10 * np.log10(a) - 10 * np.log10(b))) > 1e-3:
        return "carrier sense"
    import collections
    for maxlen, pre in ((4, []), (4, [1e-6, 2e-6, 3e-6, 4e-6]), (3, [5e-7])):
        dq = collections.deque(pre, maxlen=maxlen)
        med = np.abs(rng.standard_normal(20)) * 1e-6
        got = cs.smooth_db(dq, med)
        want = []
        for v in med:
            dq.append(float(v))
            want.append(10.0 * np.log10(max(sum(dq) / len(dq), 1e-20)))
        if not np.array_equal(got, np.array(want)):
            return "MAC smoothing"
    # correlator
    ref = (rng.standard_normal(63) + 1j * rng.standard_normal(63)).astype(np.complex64)
    E = float(np.sum(np.abs(ref) ** 2))
    y = ((rng.standard_normal(5000) + 1j * rng.standard_normal(5000)) * 0.3).astype(np.complex64)
    y[1000:1063] += ref
    cm = CorrMetric(ref, E)
    a, b = cm(y), _ref_corr(y, ref, E)
    if a.shape != b.shape or np.max(np.abs(a - b)) > 1e-4:
        return "correlator"
    for thr in (0.05, 0.3, 0.7):
        n0, n1 = 5, len(b) - 3
        seg = b[n0 - 2:n1 + 3]
        mid = seg[2:-2]
        want = np.flatnonzero((mid > thr) & (mid > seg[0:-4]) & (mid > seg[1:-3]) & (mid >= seg[3:-1]) & (mid >= seg[4:]))
        got, mx = cm.peaks(b, n0, n1, thr)
        if not np.array_equal(got, want) or mx != float(b.max()):
            return "correlator peaks"
    # sync search
    bits = rng.integers(0, 2, 4000).astype(np.uint8)
    sw = bytes([0x1A, 0xCF, 0xFC, 0x1D])
    s = np.unpackbits(np.frombuffer(sw, np.uint8))
    bits[2345:2377] = s
    bits[2350] ^= 1
    pm = bits.astype(np.int16) * 2 - 1
    corr = np.lib.stride_tricks.sliding_window_view(pm, 32) @ (s.astype(np.int16) * 2 - 1)
    for me in (0, 1, 2):
        cand = np.flatnonzero(corr >= 32 - 2 * me)
        if find_sync(bits, 0, sw, me) != (int(cand[0]) if cand.size else -1):
            return f"sync search ({me} errors)"
    # Hamming
    for n in (15, 31):
        for il in (True, False):
            coded = rng.integers(0, 2, n * 37).astype(np.uint8)
            o1, c1 = hamming_decode_bits(coded, n, 37, il)
            o2, c2 = _ref_hamming(coded, n, 37, il)
            if c1 != c2 or not np.array_equal(o1, o2):
                return f"Hamming({n})"
    # noise slices
    ns = NoiseSlices(1000)
    z = ((rng.standard_normal(5500) + 1j * rng.standard_normal(5500)) * 0.01).astype(np.complex64)
    got = np.concatenate([ns(z[:1700]), ns(z[1700:5500])])
    p = z.real.astype(np.float32) ** 2 + z.imag.astype(np.float32) ** 2
    want = p[:5000].reshape(5, 1000).mean(axis=1, dtype=np.float64)
    if got.shape != want.shape or np.max(np.abs(got / want - 1)) > 1e-5:
        return "noise slices"
    return ""


def main():
    t = time.time()
    L = lib()
    if L is None:
        print(f"C++ kernels NOT available: {why()}")
        print("The flowgraph still works (original Python code), only slower.")
        sys.exit(1)
    print(f"C++ kernels built/loaded and self-tested in {time.time() - t:.1f} s ({SRC.name}, version {VERSION})")
    rng = np.random.default_rng(1)
    nfft, sr = 256, 1e6
    f = np.fft.fftfreq(nfft, 1.0 / sr)
    bins = np.flatnonzero((np.abs(f) <= 200e3) & (np.abs(f) >= 10e3))
    win = np.hanning(nfft).astype(np.float32)
    norm = 1.0 / (np.log(2.0) * nfft * float(np.mean(win ** 2)))
    x = ((rng.standard_normal(nfft * 32) + 1j * rng.standard_normal(nfft * 32)) * 1e-3).astype(np.complex64)
    cs = CsFront(nfft, win, bins, norm)

    def bench(fn, reps=200):
        t0 = time.perf_counter()
        for _ in range(reps):
            fn()
        return (time.perf_counter() - t0) / reps

    a, b = bench(lambda: _ref_cs(x, 32, nfft, win, bins, norm)), bench(lambda: cs(x, 32))
    print(f"  carrier sense, 8192 samples: NumPy {a * 1e6:7.0f} us   C++ {b * 1e6:7.0f} us   x{a / b:.1f}")
    ref = (rng.standard_normal(63) + 1j * rng.standard_normal(63)).astype(np.complex64)
    E = float(np.sum(np.abs(ref) ** 2))
    cm = CorrMetric(ref, E)
    a, b = bench(lambda: _ref_corr(x, ref, E)), bench(lambda: cm(x))
    print(f"  correlator,    8192 samples: NumPy {a * 1e6:7.0f} us   C++ {b * 1e6:7.0f} us   x{a / b:.1f}")
    bits = rng.integers(0, 2, 8192).astype(np.uint8)
    sync = np.unpackbits(np.frombuffer(bytes([0x1A, 0xCF, 0xFC, 0x1D]), np.uint8)).astype(np.int16) * 2 - 1
    a = bench(lambda: np.flatnonzero(np.lib.stride_tricks.sliding_window_view(bits.astype(np.int16) * 2 - 1, 32)
                                      @ sync >= 30))
    b = bench(lambda: find_sync(bits, 0, bytes([0x1A, 0xCF, 0xFC, 0x1D]), 1))
    print(f"  sync search,   8192 bits:    NumPy {a * 1e6:7.0f} us   C++ {b * 1e6:7.0f} us   x{a / b:.1f}")


if __name__ == "__main__":
    main()
