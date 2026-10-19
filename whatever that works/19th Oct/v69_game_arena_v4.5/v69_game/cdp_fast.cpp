// cdp_fast.cpp - C++ kernels for the per-sample work of the v6.9 flowgraph's Python blocks.
//
// The blocks keep their logic (MAC state machine, ARQ, framing decisions) in Python; only the loops that touch
// every sample / bit run here. Python calls these through ctypes, which releases the GIL for the duration of the
// call, so they run in parallel with the other blocks on other cores.
//
//   cs_*        carrier sense front end of the CSMA/CA MAC (epy_csma): Hann window, FFT, |X|^2 over the sensed
//               bins, median per FFT block
//   corr_metric training-word correlator (epy_block_corr): normalized sliding correlation
//   find_sync   deframer (epy_block_0_0_0): first 32-bit sync word with <= max_err bit errors
//   hamming_dec deframer: Hamming(15,11)/(31,26) decode, with or without the block interleaver
//   noise_slices noise-floor tracker (epy_noise): mean power of each ~1 ms slice
//
// Built automatically by cdp_fast.py (g++ -O3 -march=native); see cdp_fast.py for the Python side.

#include <algorithm>
#include <cmath>
#include <complex>
#include <cstdint>
#include <cstring>
#include <vector>

extern "C" {

int cdp_fast_version() { return 9; }

// ------------------------------------------------------------------------------------------------ FFT
struct CsFront {
    int nfft = 0, logn = 0;
    std::vector<float> win;              // Hann window (the block's own values)
    std::vector<int> bins;               // bins to sense (numpy fftfreq order)
    double norm = 1.0;
    std::vector<int> rev;                // bit reversal
    std::vector<double> twr, twi;        // per stage (len = 8, 16, ... n): exp(-2 pi i k / len), k < len/2, contiguous
    std::vector<int> tw_off;             // offset of each stage's table
    std::vector<double> ar, ai;          // work buffers (split real / imaginary)
    std::vector<double> p;               // |X|^2 of the sensed bins
};

void* cdp_cs_new(int nfft, const float* win, const int* bins, int nbins, double norm) {
    if (nfft < 8 || (nfft & (nfft - 1)) || nbins < 1) return nullptr;
    CsFront* h = new CsFront();
    h->nfft = nfft;
    while ((1 << h->logn) < nfft) h->logn++;
    h->win.assign(win, win + nfft);
    h->bins.assign(bins, bins + nbins);
    h->norm = norm;
    h->rev.resize(nfft);
    for (int i = 0; i < nfft; i++) {
        int r = 0;
        for (int b = 0; b < h->logn; b++) r |= ((i >> b) & 1) << (h->logn - 1 - b);
        h->rev[i] = r;
    }
    for (int len = 8; len <= nfft; len <<= 1) {
        h->tw_off.push_back((int)h->twr.size());
        for (int k = 0; k < len / 2; k++) {
            h->twr.push_back(std::cos(-2.0 * M_PI * k / len));
            h->twi.push_back(std::sin(-2.0 * M_PI * k / len));
        }
    }
    h->ar.resize(nfft);
    h->ai.resize(nfft);
    h->p.resize(nbins);
    return h;
}

void cdp_cs_free(void* hp) { delete static_cast<CsFront*>(hp); }

// x: nb*nfft complex64 samples (interleaved re, im). med_out[nb] = median(|FFT(win * block)|^2 over bins) * norm
int cdp_cs_run(void* hp, const float* x, int nb, double* med_out) {
    CsFront* h = static_cast<CsFront*>(hp);
    const int n = h->nfft;
    double* ar = h->ar.data();
    double* ai = h->ai.data();
    const double* twr = h->twr.data();
    const double* twi = h->twi.data();
    for (int blk = 0; blk < nb; blk++) {
        const float* xb = x + 2 * (size_t)blk * n;
        for (int i = 0; i < n; i++) {
            // numpy: complex64 * float32 -> complex64 (rounded to float), then the FFT
            float re = xb[2 * i] * h->win[i], im = xb[2 * i + 1] * h->win[i];
            const int r = h->rev[i];
            ar[r] = re;
            ai[r] = im;
        }
        // iterative radix-2 (double). Stages 1 and 2 have the trivial twiddles 1, -i; later stages use a
        // contiguous twiddle table so the inner loop vectorizes.
        for (int s = 0; s < n; s += 2) {
            const double r0 = ar[s], i0 = ai[s], r1 = ar[s + 1], i1 = ai[s + 1];
            ar[s] = r0 + r1; ai[s] = i0 + i1; ar[s + 1] = r0 - r1; ai[s + 1] = i0 - i1;
        }
        for (int s = 0; s < n; s += 4) {
            double r0 = ar[s], i0 = ai[s], r2 = ar[s + 2], i2 = ai[s + 2];
            ar[s] = r0 + r2; ai[s] = i0 + i2; ar[s + 2] = r0 - r2; ai[s + 2] = i0 - i2;
            double r1 = ar[s + 1], i1 = ai[s + 1], r3 = ai[s + 3], i3 = -ar[s + 3];     // x3 * (-i)
            ar[s + 1] = r1 + r3; ai[s + 1] = i1 + i3; ar[s + 3] = r1 - r3; ai[s + 3] = i1 - i3;
        }
        int stage = 0;
        for (int len = 8; len <= n; len <<= 1, stage++) {
            const int half = len >> 1;
            const double* __restrict wr = twr + h->tw_off[stage];
            const double* __restrict wi = twi + h->tw_off[stage];
            for (int s = 0; s < n; s += len) {
                double* __restrict ur = ar + s; double* __restrict ui = ai + s;
                double* __restrict vr = ar + s + half; double* __restrict vi = ai + s + half;
                for (int k = 0; k < half; k++) {
                    const double tr = wr[k] * vr[k] - wi[k] * vi[k];
                    const double ti = wr[k] * vi[k] + wi[k] * vr[k];
                    const double a = ur[k], b = ui[k];
                    vr[k] = a - tr;
                    vi[k] = b - ti;
                    ur[k] = a + tr;
                    ui[k] = b + ti;
                }
            }
        }
        const int nbins = (int)h->bins.size();
        for (int j = 0; j < nbins; j++) {
            const int b = h->bins[j];
            h->p[j] = ar[b] * ar[b] + ai[b] * ai[b];
        }
        // numpy.median: middle value, or the mean of the two middle values
        double med;
        const int mid = nbins / 2;
        std::nth_element(h->p.begin(), h->p.begin() + mid, h->p.end());
        if (nbins & 1) {
            med = h->p[mid];
        } else {
            double hi = h->p[mid];
            double lo = *std::max_element(h->p.begin(), h->p.begin() + mid);
            med = 0.5 * (lo + hi);
        }
        med_out[blk] = med * h->norm;
    }
    return nb;
}

// Smoothing of the MAC: for each new median, the mean of the last `maxlen` medians (the block's deque, oldest
// first, then the new ones), summed in the same order as Python's sum(deque). means_out[nb].
// compensated = 1: Python >= 3.12, whose sum() of floats uses Neumaier's compensated summation (same steps as
// CPython's builtin_sum_impl); 0: plain left-to-right additions (older Python).
int cdp_smooth_means(const double* prev, int nprev, int maxlen, const double* med, int nb, double* means_out,
                     int compensated) {
    std::vector<double> all(prev, prev + nprev);
    all.insert(all.end(), med, med + nb);
    for (int i = 0; i < nb; i++) {
        const int end = nprev + i + 1;
        const int len = std::min(maxlen, end);
        double s = 0.0, c = 0.0;
        for (int j = end - len; j < end; j++) {
            const double x = all[j];
            if (compensated) {
                const double t = s + x;
                if (std::fabs(s) >= std::fabs(x)) c += (s - t) + x;
                else c += (x - t) + s;
                s = t;
            } else {
                s += x;
            }
        }
        if (compensated && c != 0.0 && std::isfinite(c)) s += c;
        means_out[i] = s / len;
    }
    return nb;
}

// MAC sense steps that need no Python. Copies the arithmetic of SrArqMac._step / work() (same operations, same
// order) for the steps where nothing but bookkeeping happens:
//   contend = 0 ("quiet"): no transmission, nothing received, no Block ACK owed, and nothing to send or waiting
//               for a Block ACK: carrier sense bookkeeping only.
//   contend = 1: something to send, not waiting for a Block ACK: the same, plus the contention bookkeeping of the
//               current phase (p-persistence slot timer, DIFS idle timer, backoff countdown).
// It stops BEFORE a step that needs Python - its busy state changes (busy/idle event), it reaches the Block ACK
// deadline, it draws a random number (persistence draw, new backoff), it emits an event (backoff frozen) or it
// starts a transmission (backoff reaches 0) - and AFTER a step that is due for the noise floor update.
// Python then runs that step itself, so every decision with a side effect stays in the block's own code.
// st  in/out: 0 busy_time, 1 busy_s, 2 busy_max, 3 nav_time, 4 nav_s, 5 t_last, 6 att_busy, 7 att_nav, 8 p_acc,
//             9 idle_d, 10 bo_acc
// ist in/out: 0 phase (0 persist, 1 difs, 2 backoff), 1 p_first, 2 bo (-1 = None)
// cnt out:    0 busy steps, 1 NAV steps, 2 steps with the medium busy (contention), 3 of those with NAV
int cdp_mac_steps(const double* cs_db, int nb, int i0, long long first_abs, int nfft, double samp_rate,
                  int floor_known, double floor_db, double margin, double hyst, int busy, double nav_until,
                  double deadline, double floor_next, double dt, int contend, double slot, double difs,
                  double* st, int* ist, int* cnt) {
    double busy_time = st[0], busy_s = st[1], busy_max = st[2], nav_time = st[3], nav_s = st[4], t_last = st[5];
    double att_busy = st[6], att_nav = st[7], p_acc = st[8], idle_d = st[9], bo_acc = st[10];
    int ph = ist[0], p_first = ist[1], bo = ist[2];
    int n_busy = 0, n_nav = 0, n_att = 0, n_attnav = 0, k = 0;
    const double thr_on = (floor_known ? floor_db : 1e9) + margin;
    for (int i = i0; i < nb; i++) {
        const double t = (double)(first_abs + (long long)(i + 1) * nfft) / samp_rate;
        if (t >= deadline) break;
        const double cs = cs_db[i];
        int nbusy;
        if (!floor_known) nbusy = 1;
        else if (busy) nbusy = cs > thr_on - hyst;
        else nbusy = cs > thr_on;
        if (nbusy != busy) break;
        const int nav = t < nav_until;
        const int medium_busy = busy || nav;
        // contention: decide first (nothing is changed if this step must go to Python)
        double n_p_acc = p_acc, n_idle_d = idle_d, n_bo_acc = bo_acc;
        int n_p_first = p_first, n_bo = bo;
        if (contend) {
            if (ph == 0) {                                   // p-persistence
                if (medium_busy) {
                    n_p_first = 1;
                    n_p_acc = 0.0;
                } else {
                    if (p_first) break;                      // first draw
                    n_p_acc = p_acc + dt;
                    if (n_p_acc >= slot) break;              // a slot ends: draw
                }
            } else if (ph == 1) {                            // DIFS
                if (medium_busy) {
                    n_idle_d = 0.0;
                } else {
                    n_idle_d = idle_d + dt;
                    if (!(n_idle_d + 1e-9 < difs)) break;    // DIFS complete: backoff starts (maybe a draw)
                }
            } else if (ph == 2) {                            // backoff
                if (medium_busy) break;                      // frozen: 'defer' event
                if (bo <= 0) break;
                n_bo_acc = bo_acc + dt;
                while (n_bo_acc >= slot && n_bo > 0) {
                    n_bo_acc -= slot;
                    n_bo -= 1;
                }
                if (n_bo == 0) break;                        // backoff done: the TXOP starts
            } else {
                break;
            }
        }
        // commit the step
        if (busy) {
            busy_time += dt;
            busy_s += dt;
            if (cs > busy_max) busy_max = cs;
            n_busy++;
        }
        if (nav) {
            nav_time += dt;
            nav_s += dt;
            n_nav++;
        }
        if (contend) {
            if (medium_busy) {
                att_busy += dt;
                n_att++;
                if (nav) {
                    att_nav += dt;
                    n_attnav++;
                }
            }
            p_acc = n_p_acc; idle_d = n_idle_d; bo_acc = n_bo_acc; p_first = n_p_first; bo = n_bo;
        }
        t_last = t;
        k++;
        if (t >= floor_next) break;
    }
    st[0] = busy_time; st[1] = busy_s; st[2] = busy_max; st[3] = nav_time; st[4] = nav_s; st[5] = t_last;
    st[6] = att_busy; st[7] = att_nav; st[8] = p_acc; st[9] = idle_d; st[10] = bo_acc;
    ist[0] = ph; ist[1] = p_first; ist[2] = bo;
    cnt[0] = n_busy; cnt[1] = n_nav; cnt[2] = n_att; cnt[3] = n_attnav;
    return k;
}

// MAC steps while this node's burst is on the air (phase "air"): only the TX time is counted. Stops BEFORE the
// step that sees the own echo (event) or reaches the end of the receive-off window, AFTER a floor-update step.
// st in/out: 0 tx_s, 1 t_last
int cdp_mac_air(const double* cs_db, int nb, int i0, long long first_abs, int nfft, double samp_rate,
                int echo_none, double echo_thr, double win_end, double t0, double floor_next, double dt, double* st) {
    double tx_s = st[0], t_last = st[1];
    int k = 0;
    for (int i = i0; i < nb; i++) {
        const double t = (double)(first_abs + (long long)(i + 1) * nfft) / samp_rate;
        if (echo_none && cs_db[i] > echo_thr && t < win_end && t - t0 >= 0.002) break;
        if (t >= win_end) break;
        tx_s += dt;
        t_last = t;
        k++;
        if (t >= floor_next) break;
    }
    st[0] = tx_s;
    st[1] = t_last;
    return k;
}

// ------------------------------------------------------------------------------------------------ correlator
// x: nx complex64; ref: L complex64; m_out[nx-L+1]:
//   c[n] = sum_k conj(ref[k]) x[n+k]   (numpy.correlate(x, ref, "valid"))
//   w[n] = sum_k |x[n+k]|^2
//   m[n] = |c[n]|^2 / (E * w[n] + 1e-12)
// Work buffers are passed in (p: nx + 1, the others >= nx) so the call allocates nothing.
int cdp_corr_metric(const float* x, int nx, const float* ref, int L, double E, double* m_out,
                    float* xr, float* xi, double* p, float* cr, float* ci, double* w) {
    if (nx < L || L < 1) return 0;
    const int nv = nx - L + 1;
    // p = running sum of |x|^2 (double), so w[n] = p[n+L] - p[n]
    p[0] = 0.0;
    for (int i = 0; i < nx; i++) {
        xr[i] = x[2 * i];
        xi[i] = x[2 * i + 1];
        float mag = std::sqrt(xr[i] * xr[i] + xi[i] * xi[i]);   // numpy: abs() in float32, then float64 ** 2
        p[i + 1] = p[i] + (double)mag * (double)mag;
    }
    std::fill(cr, cr + nv, 0.0f);
    std::fill(ci, ci + nv, 0.0f);
    for (int k = 0; k < L; k++) {                   // k outer, n inner: vectorizes over n, fixed summation order
        const float rr = ref[2 * k], ri = ref[2 * k + 1];
        const float* __restrict a = xr + k;
        const float* __restrict b = xi + k;
        float* __restrict o_r = cr;
        float* __restrict o_i = ci;
        for (int n = 0; n < nv; n++) {
            o_r[n] += rr * a[n] + ri * b[n];
            o_i[n] += rr * b[n] - ri * a[n];
        }
    }
    for (int n = 0; n < nv; n++) {
        double ww = p[n + L] - p[n];
        w[n] = ww > 0.0 ? ww : 0.0;
    }
    for (int n = 0; n < nv; n++) {
        double mag = std::sqrt((double)cr[n] * cr[n] + (double)ci[n] * ci[n]);
        m_out[n] = (mag * mag) / (E * w[n] + 1e-12);
    }
    return nv;
}

// Peak search of the correlator on m (from cdp_corr_metric), exactly the NumPy expression of the block:
//   for j in [n0, n1]: m[j] > thr and m[j] > m[j-2] and m[j] > m[j-1] and m[j] >= m[j+1] and m[j] >= m[j+2]
// idx_out gets j - n0 (the index into 'mid' that the block uses). Returns the count; *mmax = max(m[0..nm)).
int cdp_corr_peaks(const double* m, int nm, int n0, int n1, double thr, int* idx_out, double* mmax) {
    double mx = nm > 0 ? m[0] : 0.0;
    for (int j = 1; j < nm; j++)
        if (m[j] > mx || std::isnan(m[j])) mx = std::isnan(mx) ? mx : m[j];
    *mmax = mx;
    int c = 0;
    if (n0 < 2) n0 = 2;
    if (n1 > nm - 3) n1 = nm - 3;
    for (int j = n0; j <= n1; j++) {
        const double v = m[j];
        if (v > thr && v > m[j - 2] && v > m[j - 1] && v >= m[j + 1] && v >= m[j + 2]) idx_out[c++] = j - n0;
    }
    return c;
}

// ------------------------------------------------------------------------------------------------ deframer
// bits: 0/1 per byte. -> first i >= start with popcount(bits[i..i+31] ^ sync) <= max_err, or -1.
int cdp_find_sync(const uint8_t* bits, int n, int start, uint32_t sync, int max_err) {
    if (start < 0) start = 0;
    if (n - start < 32) return -1;
    uint32_t r = 0;
    for (int i = start; i < start + 31; i++) r = (r << 1) | (bits[i] & 1u);
    for (int i = start + 31; i < n; i++) {
        r = (r << 1) | (bits[i] & 1u);
        if (__builtin_popcount(r ^ sync) <= max_err) return i - 31;
    }
    return -1;
}

// coded: nblk*n bits (0/1). Same layout as the Python hamming_decode_bits: interleaved = column-major
// (coded[j*nblk + b] is bit j of block b). out: nblk*k data bits. Returns the number of corrected blocks.
int cdp_hamming_dec(const uint8_t* coded, int n, int nblk, int interleave, uint8_t* out) {
    if (n != 15 && n != 31) return -1;
    int corrected = 0;
    uint8_t cw[32];
    int o = 0;
    for (int b = 0; b < nblk; b++) {
        int s = 0;
        for (int j = 0; j < n; j++) {
            uint8_t v = interleave ? coded[(size_t)j * nblk + b] : coded[(size_t)b * n + j];
            cw[j] = v & 1;
            if (cw[j]) s ^= (j + 1);
        }
        if (s) {
            cw[s - 1] ^= 1;
            corrected++;
        }
        for (int j = 0; j < n; j++) {
            int pos = j + 1;
            if (pos & (pos - 1)) out[o++] = cw[j];      // not a power of two: data bit
        }
    }
    return corrected;
}

// ------------------------------------------------------------------------------------------------ noise floor
// Mean of |x|^2 over consecutive slices of `slice` samples; a partial slice is carried in *acc / *cnt.
// Returns the number of means written to means_out.
int cdp_noise_slices(const float* x, int n, int slice, double* acc, int* cnt, double* means_out) {
    int k = 0;
    double s = *acc;
    int c = *cnt;
    for (int i = 0; i < n; i++) {
        float re = x[2 * i], im = x[2 * i + 1];
        s += (double)(re * re + im * im);               // numpy: float32 power, float64 mean
        if (++c == slice) {
            means_out[k++] = s / slice;
            s = 0.0;
            c = 0;
        }
    }
    *acc = s;
    *cnt = c;
    return k;
}

}  // extern "C"
