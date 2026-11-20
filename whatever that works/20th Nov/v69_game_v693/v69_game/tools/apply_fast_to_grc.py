#!/usr/bin/env python3
"""
apply_fast_to_grc.py - adds the C++ kernel hooks (cdp_fast.py) to the four per-sample Python blocks of a v6.9.1
flowgraph: MAC (epy_csma), correlator (epy_block_corr), deframer (epy_block_0_0_0), noise floor (epy_noise).

The v6.9_cdp_link.grc in this folder already has them; use this only for another copy of the v6.9.1 flowgraph:
    python3 tools/apply_fast_to_grc.py my_flowgraph.grc            (edits the file, keeps my_flowgraph.grc.bak)

It edits only the code of those four blocks (as text, so the rest of the file stays byte-identical), checks that
the result parses and compiles, and stops without writing anything if a block's code is not the v6.9.1 code.
With CDP_FAST=0, or without a compiler, the blocks then run exactly their original code.
"""
import shutil
import sys
from pathlib import Path

import yaml

GRC = Path(sys.argv[1]) if len(sys.argv) > 1 else None

HELPER = '''# ---- end debug bus ----

# ---- C++ kernels (cdp_fast.py / cdp_fast.cpp next to the flowgraph); None -> the NumPy code of this block ----
def _load_fast():
    import os as _os
    if _os.environ.get("CDP_FAST", "1").strip().lower() in ("0", "no", "off", "false"):
        return None
    try:
        import cdp_fast
        return cdp_fast
    except ImportError:
        pass
    for d in (_os.environ.get("CDP_HOME"), _os.getcwd(),
              _os.path.dirname(_os.path.abspath(_sys.argv[0])) if _sys.argv and _sys.argv[0] else None):
        if d and _os.path.isfile(_os.path.join(d, "cdp_fast.py")):
            if d not in _sys.path:
                _sys.path.insert(0, d)
            try:
                import cdp_fast
                return cdp_fast
            except Exception:
                return None
    return None
# ---- end C++ kernels ----'''

MAC_FAST = '''    # ------------------------------------------------------------------ stream
    def _work_fast(self, buf, nb, first_abs, blank):
        """The loop of work() with the C++ kernels (cdp_fast.cpp): the same steps, the same arithmetic, the same
        results (tools/test_fast_blocks.py compares them event by event).
        - carrier sense: the FFT medians and the smoothed cs_db of all nb sense steps at once (C++)
        - steps that only do bookkeeping run in C++ (_fast_steps): carrier-sense statistics, the contention timers
          (p-persistence slot timer, DIFS, backoff countdown) and the TX time while the own burst is on the air.
          C++ stops before every step that has a side effect - busy/idle change, random draw, event, start of a
          transmission, Block ACK deadline, received frame - and _step() runs that step exactly as before.
          Messages (app_in, rx_in, phy_in, ctrl) are handled by GNU Radio between work() calls, never during one,
          so nothing can change under a stretch of steps."""
        med = self._cs_fast(buf, nb)
        cs_all = self._cs_fast.smooth_db(self._smooth, med)
        self._smooth.extend(med.tolist())
        i = 0
        while i < nb:
            k = self._fast_steps(cs_all, i, first_abs, blank)
            if k:
                i += k
                if self._t >= self._floor_next:
                    self._floor_next = self._t + 0.05
                    self._update_floor()
                continue
            self._cs_db = cs_all[i]
            self._t = (first_abs + (i + 1) * self._nfft) / self.samp_rate
            was_tx = self._self_tx
            self._step(self._cs_db)
            if self.blank_rx and (was_tx or self._self_tx):
                s = first_abs + i * self._nfft
                blank.append((s, s + self._nfft))
            if self._t >= self._floor_next:
                self._floor_next = self._t + 0.05
                self._update_floor()
            i += 1

    def _fast_steps(self, cs_all, i, first_abs, blank):
        """-> number of steps (from step i) done in C++; 0 = step i needs _step()"""
        if self._rx_ev or self._arq_inbox:
            return 0
        F = self._cs_fast
        if self._self_tx:                                  # own burst on the air: only the TX time is counted
            tx = self._tx
            if tx.get("phase") != "air":
                return 0
            echo_thr = (self._floor_db if self._floor_db is not None else 1e9) + \\
                (max(self.cs_margin_db, self.echo_margin_db) if tx["mute"] else self.cs_margin_db)
            st = np.array([self._dbg.get("tx_s", 0), self._t], np.float64)
            k = F.air(cs_all, i, first_abs, self._nfft, self.samp_rate, tx["echo_t"] is None, echo_thr,
                      tx["win_end"], tx["t0"], self._floor_next, self._dt, st)
            if k:
                self._dbg["tx_s"] = float(st[0])
                self._t = float(st[1])
                self._cs_db = cs_all[i + k - 1]
                if self.blank_rx:
                    blank.append((first_abs + i * self._nfft, first_abs + (i + k) * self._nfft))
            return k
        if self._ba_due or self._prev_busy is None or self._prev_busy != self._busy:
            return 0
        aw = self._await
        if aw is not None:                                 # waiting for a Block ACK (until its deadline)
            contend, deadline = False, aw["deadline"]
        else:
            contend, deadline = self._has_work(), math.inf
            if contend and self._ph not in F.PHASES:
                return 0
        st = np.array([self._busy_time, self._dbg.get("busy_s", 0), self._busy_max, self._nav_time,
                       self._dbg.get("nav_s", 0), self._t, self._att_busy, self._att_nav, self._p_acc,
                       self._idle_d, self._bo_acc], np.float64)
        ist = np.array([F.PHASES.get(self._ph, 0), 1 if self._p_first else 0, -1 if self._bo is None else self._bo],
                       np.int32)
        k, cnt = F.steps(cs_all, i, first_abs, self._nfft, self.samp_rate, self._floor_db, self.cs_margin_db,
                         self.cs_hyst_db, self._busy, self._nav_until, deadline, self._floor_next, self._dt,
                         contend, self.slot_s, self.difs(), st, ist)
        if k == 0:
            return 0
        self._hist.extend(cs_all[i:i + k])
        if cnt[0]:
            self._busy_time, self._dbg["busy_s"], self._busy_max = float(st[0]), float(st[1]), st[2]
        if cnt[1]:
            self._nav_time, self._dbg["nav_s"] = float(st[3]), float(st[4])
        self._t = float(st[5])
        self._cs_db = cs_all[i + k - 1]
        if contend:
            self._att_busy, self._att_nav = float(st[6]), float(st[7])
            self._p_acc, self._idle_d, self._bo_acc = float(st[8]), float(st[9]), float(st[10])
            self._p_first = bool(ist[1])
            self._bo = None if ist[2] < 0 else int(ist[2])
        elif aw is None:
            self._reset_contention()                       # what each of these steps did (nothing to send)
        return k
'''

EDITS = {
    "epy_csma": [
        ("# ---- end debug bus ----", HELPER),
        ('''        self._smooth = collections.deque(maxlen=max(1, int(round(self._sense_s / self._dt))))''',
         '''        self._smooth = collections.deque(maxlen=max(1, int(round(self._sense_s / self._dt))))
        self._cs_fast = None                    # C++ carrier sense front end (set in start)'''),
        ('''    def start(self):
        self._muter = TxMuter(self.iio_uri, self.verbose)''',
         '''    def start(self):
        F = _load_fast()
        if F is not None:
            cs = F.CsFront(self._nfft, self._win, self._bins, self._norm)
            self._cs_fast = cs if cs.ok else None
        print(F.status("MAC carrier sense") if F is not None else "[FAST] MAC carrier sense: original Python code",
              flush=True)
        self._muter = TxMuter(self.iio_uri, self.verbose)'''),
        ('''        blank = []
        if nb:
            blocks = buf[:nb * self._nfft].reshape(nb, self._nfft) * self._win''',
         '''        blank = []
        if nb and self._cs_fast is not None:
            self._work_fast(buf, nb, first_abs, blank)     # C++ front end + quiet steps: same results, see below
        elif nb:
            blocks = buf[:nb * self._nfft].reshape(nb, self._nfft) * self._win'''),
        ('''    # ------------------------------------------------------------------ stream
    def work(self, input_items, output_items):''',
         MAC_FAST + '''
    def work(self, input_items, output_items):'''),
    ],
    "epy_noise": [
        ("# ---- end debug bus ----", HELPER),
        ('''        self._printed = None
''', '''        self._printed = None
        self._fast = None                      # C++ slice means (set in start)
'''),
        ('''    def start(self):
        _bus().sources["noise"] = self''', '''    def start(self):
        F = _load_fast()
        if F is not None:
            ns = F.NoiseSlices(self._slice)
            self._fast = ns if ns.ok else None
        if self.verbose:
            print(F.status("noise floor") if F is not None else "[FAST] noise floor: original Python code", flush=True)
        _bus().sources["noise"] = self'''),
        ('''        p = x.real.astype(np.float32) ** 2 + x.imag.astype(np.float32) ** 2
        if self._carry.size:''', '''        if self._fast is not None:                                     # C++: carries the partial slice itself
            means = self._fast(x)
            if len(means):
                with self._lock:
                    self._ring.extend(means)
            return n
        p = x.real.astype(np.float32) ** 2 + x.imag.astype(np.float32) ** 2
        if self._carry.size:'''),
    ],
    "epy_block_corr": [
        ("# ---- end debug bus ----", HELPER),
        ('''        self._dbg_n = 0

    def start(self):
        _bus().sources["corr"] = self''', '''        self._dbg_n = 0
        self._fast = None              # C++ correlation (set in start)

    def start(self):
        F = _load_fast()
        if F is not None:
            cm = F.CorrMetric(self._ref, self._E)
            self._fast = cm if cm.ok else None
        if self.verbose:
            print(F.status("correlator") if F is not None else "[FAST] correlator: original Python code", flush=True)
        _bus().sources["corr"] = self'''),
        ('''        self._best = max(self._best, float(m.max()))
        self._dbg_best = max(self._dbg_best, float(m.max()))
        self._dbg_n += n_out
        self._seen += n_out

        n0 = max(self._next_p - base, 2, -base)       # first window start to examine (p >= 0)
        n1 = len(m) - 3                               # last start with 2 neighbours on the right
        if n1 >= n0:
            seg = m[n0 - 2 : n1 + 3]
            mid = seg[2:-2]
            thr_abs = self.thr
            cond = (
                (mid > thr_abs)
                & (mid > seg[0:-4]) & (mid > seg[1:-3])
                & (mid >= seg[3:-1]) & (mid >= seg[4:])
            )
            for j in np.flatnonzero(cond):''', '''        n0 = max(self._next_p - base, 2, -base)       # first window start to examine (p >= 0)
        n1 = len(m) - 3                               # last start with 2 neighbours on the right
        if self._fast is not None:                    # C++: the same comparisons on the same values
            hits, m_max = self._fast.peaks(m, n0, n1, self.thr)
        else:
            m_max = float(m.max())
        self._best = max(self._best, m_max)
        self._dbg_best = max(self._dbg_best, m_max)
        self._dbg_n += n_out
        self._seen += n_out

        if n1 >= n0:
            mid = m[n0:n1 + 1]
            if self._fast is None:
                seg = m[n0 - 2 : n1 + 3]
                mid = seg[2:-2]
                thr_abs = self.thr
                cond = (
                    (mid > thr_abs)
                    & (mid > seg[0:-4]) & (mid > seg[1:-3])
                    & (mid >= seg[3:-1]) & (mid >= seg[4:])
                )
                hits = np.flatnonzero(cond)
            for j in hits:'''),
        ('''        c = np.correlate(x, self._ref, "valid")                  # window starting at x[n]
        w = np.convolve(np.abs(x).astype(np.float64) ** 2, np.ones(self._L), "valid")
        m = (np.abs(c).astype(np.float64) ** 2) / (self._E * w + 1e-12)    # normalized, 0..1''',
         '''        if self._fast is not None:
            m = self._fast(x)                                    # C++: the same metric, without the GIL
        else:
            c = np.correlate(x, self._ref, "valid")                  # window starting at x[n]
            w = np.convolve(np.abs(x).astype(np.float64) ** 2, np.ones(self._L), "valid")
            m = (np.abs(c).astype(np.float64) ** 2) / (self._E * w + 1e-12)    # normalized, 0..1'''),
    ],
    "epy_block_0_0_0": [
        ("# ---- end debug bus ----", HELPER),
        ('''        self._last = {}                       # src -> {"msg": msg_id, "reps": set, "t": time}
''', '''        self._last = {}                       # src -> {"msg": msg_id, "reps": set, "t": time}
        self._fast = None                     # C++ sync search + Hamming decoder (set in start)
'''),
        ('''    def start(self):
        _bus().sources["deframer"] = self''', '''    def start(self):
        F = _load_fast()
        self._fast = F if F is not None and F.lib() is not None else None
        print(F.status("deframer") if F is not None else "[FAST] deframer: original Python code", flush=True)
        _bus().sources["deframer"] = self'''),
        ('''        bits, corr = hamming_decode_bits(coded, self.fec, nblk, self.interleave)''',
         '''        if self._fast is not None:
            bits, corr = self._fast.hamming_decode_bits(coded, self.fec, nblk, self.interleave)
        else:
            bits, corr = hamming_decode_bits(coded, self.fec, nblk, self.interleave)'''),
        ('''            pm = buf.astype(np.int16) * 2 - 1
            corr = np.lib.stride_tricks.sliding_window_view(pm, 32) @ self._sync
            cand = np.flatnonzero(corr >= thr)

            if cand.size == 0:
                self._keep(buf, len(buf) - 31)          # keep trailing bits that might begin a sync word
                return n_in

            i = int(cand[0])''', '''            if self._fast is not None:                  # C++: first sync word with <= max_bit_errors errors
                i = self._fast.find_sync(buf, 0, SYNC_WORD, self.max_bit_errors)
            else:
                pm = buf.astype(np.int16) * 2 - 1
                corr = np.lib.stride_tricks.sliding_window_view(pm, 32) @ self._sync
                cand = np.flatnonzero(corr >= thr)
                i = int(cand[0]) if cand.size else -1

            if i < 0:
                self._keep(buf, len(buf) - 31)          # keep trailing bits that might begin a sync word
                return n_in
'''),
    ],
}


def block_span(lines, name):
    """-> (first code line, end line (exclusive), indent) of the _source_code block scalar of block `name`"""
    start = lines.index(f"- name: {name}\n")
    i = start + 1
    while not lines[i].lstrip().startswith("_source_code:"):
        i += 1
    key_indent = len(lines[i]) - len(lines[i].lstrip())
    j = i + 1
    while j < len(lines) and lines[j].strip() == "":
        j += 1
    ind = len(lines[j]) - len(lines[j].lstrip())
    assert ind > key_indent
    k = j
    while k < len(lines) and (lines[k].strip() == "" or len(lines[k]) - len(lines[k].lstrip()) >= ind):
        k += 1
    while lines[k - 1].strip() == "":       # trailing blank lines belong to the next key's spacing
        k -= 1
    return i + 1, k, ind


def main():
    if GRC is None or not GRC.is_file():
        raise SystemExit(__doc__)
    text = GRC.read_text()
    if "def _load_fast():" in text:
        print(f"{GRC.name} already has the C++ kernel hooks")
        return
    lines = text.splitlines(keepends=True)
    before = {b["name"]: b["parameters"].get("_source_code") for b in yaml.safe_load(text)["blocks"]}
    want = {}
    for name, edits in EDITS.items():
        a, b, ind = block_span(lines, name)
        code = "".join(l[ind:] if l.strip() else "\n" for l in lines[a:b])
        for old, new in edits:
            if code.count(old) != 1:
                raise SystemExit(f"{name}: its code is not the v6.9.1 code this script knows - nothing written")
            code = code.replace(old, new)
        want[name] = code
        new_lines = [(" " * ind + l) if l.strip() else "\n" for l in code.splitlines(keepends=True)]
        lines[a:b] = new_lines
    out = "".join(lines)
    after = {b["name"]: b["parameters"].get("_source_code") for b in yaml.safe_load(out)["blocks"]}
    for name in before:
        if name in want:
            got = after[name]
            assert got.rstrip("\n") == want[name].rstrip("\n"), name
            compile(got, name, "exec")
        else:
            assert after[name] == before[name], f"{name} changed"
    shutil.copy2(GRC, GRC.with_name(GRC.name + ".bak"))
    GRC.write_text(out)
    print(f"{GRC.name}: C++ kernel hooks added to", ", ".join(want), "| other blocks unchanged |",
          f"backup: {GRC.name}.bak")


if __name__ == "__main__":
    main()
