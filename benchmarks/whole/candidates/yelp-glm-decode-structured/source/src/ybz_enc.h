// ybz — encoder-side rANS and stream serialization. Not included by the decoder.
#pragma once

#include "ybz_common.h"

struct YbRansEnc {
    uint32_t x;
    uint32_t logM = YB_RANS_SCALE;
    void init() { x = YB_RANS_L; }
    // Renorm bound (16-bit-lane generalization of ryg_rans): x_max = (L>>logM)<<16 * freq.
    // Entry x < x_max keeps the post-step state below 2^32; since
    // x_max >= (2^16>>logM)<<16 >= 2^16 for logM <= 12, at most one lane is
    // emitted per put, and the decoder's single-lane renorm restores x >= 2^(16 + 16 - logM) >= L.
    inline void put(uint16_t freq, uint16_t cum, uint8_t*& ptr) {
        if (freq == (1u << logM)) return;  // certain symbol: identity step (x_max would wrap 2^32)
        uint32_t x_max = ((YB_RANS_L >> logM) << 16) * (uint32_t)freq;
        while (x >= x_max) {
            ptr -= 2;
            yb_put16(ptr, (uint16_t)x);
            x >>= 16;
        }
        x = ((x / freq) << logM) + (x % freq) + cum;
    }
    inline void flush(uint8_t*& ptr) { ptr -= 4; yb_put32(ptr, x); }
};

// Encode-side model: dense freq/cum over (order0: D) or (order1: nctx*symD) cells.
struct YbEncModel {
    bool order1 = false;
    uint32_t nctx = 1;
    uint32_t symD = 0;
    uint32_t logM = YB_RANS_SCALE;
    uint32_t M = YB_RANS_M;
    std::vector<uint16_t> freq;
    std::vector<uint16_t> cum;

    void set_scale(uint32_t logM_) {
        logM = logM_;
        M = 1u << logM;
    }
    bool build(bool order1_, uint32_t nctx_, uint32_t symD_, const std::vector<uint32_t>& counts) {
        order1 = order1_;
        nctx = nctx_;
        symD = symD_;
        size_t cells = order1 ? (size_t)nctx * symD : symD;
        freq.assign(cells, 0);
        cum.assign(cells, 0);
        if (!order1) return norm_ctx(counts.data(), symD, 0);
        for (uint32_t c = 0; c < nctx; c++)
            if (!norm_ctx(counts.data() + (size_t)c * symD, symD, (size_t)c * symD)) return false;
        return true;
    }

private:
    // normalize one context's counts to sum M (floor + min 1, remainder to the
    // largest counts); cum is the prefix sum in ascending symbol order
    bool norm_ctx(const uint32_t* cnt, uint32_t D, size_t off) {
        uint64_t total = 0;
        uint32_t distinct = 0;
        for (uint32_t i = 0; i < D; i++) {
            total += cnt[i];
            if (cnt[i]) distinct++;
        }
        const uint32_t M = this->M;
        if (distinct == 0) return true;  // unused context
        if (distinct > M) return false;
        uint32_t sum = 0;
        for (uint32_t i = 0; i < D; i++) {
            uint32_t f = cnt[i] ? (uint32_t)((uint64_t)cnt[i] * M / total) : 0;
            if (cnt[i] && f == 0) f = 1;
            freq[off + i] = (uint16_t)f;
            sum += f;
        }
        // rounding + min-1 can overshoot M (many rare symbols); trim from the
        // least-count symbols with f >= 2 first
        while (sum > M) {
            uint32_t best = UINT32_MAX;
            uint64_t bestcnt = UINT64_MAX;
            for (uint32_t i = 0; i < D; i++) {
                if (freq[off + i] >= 2 && cnt[i] < bestcnt) {
                    bestcnt = cnt[i];
                    best = i;
                }
            }
            if (best == UINT32_MAX) return false;  // cannot happen: sum > M implies mass
            freq[off + best]--;
            sum--;
        }
        uint32_t diff = M - sum;
        while (diff > 0) {
            uint32_t best = UINT32_MAX;
            uint64_t bestcnt = 0;
            for (uint32_t i = 0; i < D; i++) {
                if (cnt[i] > bestcnt) {
                    bestcnt = cnt[i];
                    best = i;
                }
            }
            if (best == UINT32_MAX) return false;
            freq[off + best]++;
            diff--;
        }
        uint32_t c = 0;
        for (uint32_t i = 0; i < D; i++) {
            cum[off + i] = (uint16_t)c;
            c += freq[off + i];
        }
        return true;
    }
};

struct YbStreamOut {
    // order-0 stream from a symbol array
    static void write0(std::vector<uint8_t>& out, const uint32_t* syms, uint32_t nsyms,
                       const YbEncModel& m) {
        const uint32_t ilv = nsyms >= 4096 ? 8 : 1;
        push_header(out, nsyms, ilv, 0, m, 1);
        const size_t cap = (((size_t)nsyms * 2 / ilv + 16) + 7) & ~(size_t)7;
        const size_t base = out.size();
        out.resize(base + (size_t)ilv * cap);
        uint8_t* ends[8];
        uint8_t* ptrs[8];
        for (uint32_t k = 0; k < ilv; k++) {
            ends[k] = out.data() + base + (size_t)(k + 1) * cap;
            ptrs[k] = ends[k];
        }
        YbRansEnc r[8];
        for (uint32_t k = 0; k < ilv; k++) { r[k].init(); r[k].logM = m.logM; }
        for (uint32_t j = nsyms; j-- > 0;) {
            uint32_t k = j & (ilv - 1);
            uint32_t s = syms[j];
            r[k].put(m.freq[s], m.cum[s], ptrs[k]);
        }
        for (uint32_t k = 0; k < ilv; k++) r[k].flush(ptrs[k]);
        finish(out, base, ilv, ptrs, ends);
    }

    // order-1 stream over byte symbols; ctx of char j is text[j-1], init nctx-1
    static void write1(std::vector<uint8_t>& out, const uint8_t* text, uint32_t n,
                       const YbEncModel& m) {
        const uint32_t ilv = n >= 4096 ? 8 : 1;
        push_header(out, n, ilv, 1, m, m.nctx);
        // lane writes are 2 bytes each; keep per-substream capacity a multiple
        // of 8 so all writes stay inside [base, base+ilv*cap)
        const size_t cap = (((size_t)n * 3 / ilv + 32) + 7) & ~(size_t)7;
        const size_t base = out.size();
        out.resize(base + (size_t)ilv * cap);
        uint8_t* ends[8];
        uint8_t* ptrs[8];
        for (uint32_t k = 0; k < ilv; k++) {
            ends[k] = out.data() + base + (size_t)(k + 1) * cap;
            ptrs[k] = ends[k];
        }
        YbRansEnc r[8];
        for (uint32_t k = 0; k < ilv; k++) { r[k].init(); r[k].logM = m.logM; }
        for (uint32_t j = n; j-- > 0;) {
            uint32_t k = j & (ilv - 1);
            uint32_t c = j ? text[j - 1] : m.nctx - 1;
            size_t off = (size_t)c * m.symD + text[j];
            r[k].put(m.freq[off], m.cum[off], ptrs[k]);
        }
        for (uint32_t k = 0; k < ilv; k++) r[k].flush(ptrs[k]);
        finish(out, base, ilv, ptrs, ends);
    }

private:
    static void push_header(std::vector<uint8_t>& out, uint32_t nsyms, uint32_t ilv, uint32_t order,
                            const YbEncModel& m, uint32_t ntab) {
        uint8_t b[4];
        yb_put32(b, nsyms);
        out.insert(out.end(), b, b + 4);
        out.push_back((uint8_t)ilv);
        out.push_back((uint8_t)order);
        out.push_back((uint8_t)m.logM);
        out.push_back(0);
        if (!m.order1) {
            uint32_t dc = 0;
            for (uint32_t i = 0; i < m.symD; i++)
                if (m.freq[i]) dc++;
            yb_put16(b, (uint16_t)dc);
            out.insert(out.end(), b, b + 2);
            for (uint32_t i = 0; i < m.symD; i++) {
                if (m.freq[i]) {
                    yb_put16(b, (uint16_t)i);
                    out.insert(out.end(), b, b + 2);
                    yb_put16(b, m.freq[i]);
                    out.insert(out.end(), b, b + 2);
                }
            }
        } else {
            yb_put16(b, (uint16_t)m.symD);
            out.insert(out.end(), b, b + 2);
            yb_put16(b, (uint16_t)ntab);
            out.insert(out.end(), b, b + 2);
            for (uint32_t c = 0; c < ntab; c++) {
                uint32_t dc = 0;
                for (uint32_t i = 0; i < m.symD; i++)
                    if (m.freq[(size_t)c * m.symD + i]) dc++;
                yb_put16(b, (uint16_t)dc);
                out.insert(out.end(), b, b + 2);
                for (uint32_t i = 0; i < m.symD; i++) {
                    uint16_t f = m.freq[(size_t)c * m.symD + i];
                    if (f) {
                        yb_put16(b, (uint16_t)i);
                        out.insert(out.end(), b, b + 2);
                        yb_put16(b, f);
                        out.insert(out.end(), b, b + 2);
                    }
                }
            }
        }
        out.resize(out.size() + (size_t)ilv * 4);  // substream sizes, patched below
    }

    static void finish(std::vector<uint8_t>& out, size_t base, uint32_t ilv, uint8_t* ptrs[4],
                       uint8_t* ends[4]) {
        uint32_t len[8];
        size_t total = 0;
        for (uint32_t k = 0; k < ilv; k++) {
            len[k] = (uint32_t)(ends[k] - ptrs[k]);
            total += len[k];
        }
        const size_t sizes_at = base - (size_t)ilv * 4;
        for (uint32_t k = 0; k < ilv; k++) yb_put32(out.data() + sizes_at + (size_t)k * 4, len[k]);
        size_t w = base;
        for (uint32_t k = 0; k < ilv; k++) {
            std::memmove(out.data() + w, ptrs[k], len[k]);
            w += len[k];
        }
        out.resize(w);
    }
};
