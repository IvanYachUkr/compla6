// codec.cpp — PZ1 fast LZ encoder (encode-dir / encode-stream).
//
// Pipeline per 4 MiB chunk: greedy+lazy hash-chain parse (min match 4,
// depth 32, 4 repeated-offset slots) -> token stream -> per-chunk static
// canonical Huffman (length-limited to 15 bits) with extra bits inline.
// Chunks are compressed independently in parallel; the archive body is the
// ordered concatenation of self-delimiting chunk records.
#include "pzformat.hpp"
#include <time.h>
#ifdef PZTIME
static double g_tparse = 0, g_temit = 0;
static inline double now_s() { struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts); return ts.tv_sec + 1e-9*ts.tv_nsec; }
#endif
#include <atomic>
#include <thread>
#include <dirent.h>
#include <unistd.h>
#include <sys/stat.h>

static int get_threads() {
    int t = 4;
    const char* e = getenv("COMPRESSION_LAB_THREADS");
    if (e && *e) { int v = atoi(e); if (v >= 1 && v <= 64) t = v; }
    return t;
}

// ---------------- Huffman (encoder side) ----------------
struct HuffCode { uint8_t len[MAIN_N]; uint32_t code[MAIN_N]; };

// Build Huffman code lengths, length-limited to 15 by iterative frequency
// halving. A single used symbol gets a dummy partner at length 1 so the
// canonical code stays complete (decoders validate completeness).
static void huff_lengths(const uint32_t* freq, int n, uint8_t* lens) {
    static thread_local uint32_t f[MAIN_N];
    for (int i = 0; i < n; i++) f[i] = freq[i];
    int nz = 0;
    for (int i = 0; i < n; i++) if (f[i]) nz++;
    if (nz == 0) { memset(lens, 0, n); return; }
    static thread_local uint32_t w[2 * MAIN_N];
    static thread_local uint16_t hn[2 * MAIN_N], lf[2 * MAIN_N], rt[2 * MAIN_N], stk[2 * MAIN_N];
    static thread_local uint16_t symof[2 * MAIN_N];
    static thread_local uint8_t sd[2 * MAIN_N];
    for (;;) {
        int nn = 0, heapn_ = 0;
        auto push = [&](int id) {
            int i = heapn_++;
            hn[i] = (uint16_t)id;
            while (i > 0) {
                int par = (i - 1) / 2;
                if (w[hn[par]] <= w[hn[i]]) break;
                std::swap(hn[par], hn[i]); i = par;
            }
        };
        auto pop = [&]() {
            int id = hn[0];
            hn[0] = hn[--heapn_];
            int i = 0;
            for (;;) {
                int l = 2*i+1, r = 2*i+2, s = i;
                if (l < heapn_ && w[hn[l]] < w[hn[s]]) s = l;
                if (r < heapn_ && w[hn[r]] < w[hn[s]]) s = r;
                if (s == i) break;
                std::swap(hn[s], hn[i]); i = s;
            }
            return id;
        };
        for (int i = 0; i < n; i++) if (f[i]) { w[nn] = f[i]; lf[nn] = rt[nn] = 0xFFFF; symof[nn] = (uint16_t)i; push(nn); nn++; }
        while (heapn_ > 1) {
            int a = pop(), b = pop();
            w[nn] = w[a] + w[b];
            lf[nn] = (uint16_t)a; rt[nn] = (uint16_t)b;
            symof[nn] = 0xFFFF;
            push(nn); nn++;
        }
        memset(lens, 0, n);
        int sp = 0, maxlen = 0;
        stk[sp] = (uint16_t)(nn - 1); sd[sp] = 0; sp++;
        while (sp > 0) {
            sp--;
            int id = stk[sp]; uint8_t dep = sd[sp];
            if (lf[id] == 0xFFFF) {
                int s2 = symof[id];
                lens[s2] = dep ? dep : 1;
                if (dep > maxlen) maxlen = dep;
            } else {
                stk[sp] = lf[id]; sd[sp] = dep + 1; sp++;
                stk[sp] = rt[id]; sd[sp] = dep + 1; sp++;
            }
        }
        if (maxlen <= MAX_CODE_LEN) break;
        for (int i = 0; i < n; i++) if (f[i]) f[i] = (f[i] + 1) >> 1;
    }
    int used = -1, nused = 0;
    for (int i = 0; i < n; i++) if (lens[i]) { nused++; used = i; }
    if (nused == 1) lens[used == 0 ? 1 : 0] = 1;
}

static void canonical_codes(const uint8_t* lens, int n, HuffCode& hc) {
    int bl_count[MAX_CODE_LEN + 1] = {0};
    for (int i = 0; i < n; i++) bl_count[lens[i]]++;
    bl_count[0] = 0;
    uint32_t next_code[MAX_CODE_LEN + 2];
    uint32_t c = 0;
    for (int b = 1; b <= MAX_CODE_LEN; b++) { c = (c + bl_count[b-1]) << 1; next_code[b] = c; }
    for (int i = 0; i < n; i++) {
        hc.len[i] = lens[i];
        hc.code[i] = lens[i] ? next_code[lens[i]]++ : 0;
    }
}

// ---------------- bit writer (MSB-first) ----------------
struct BW {
    std::vector<uint8_t>& buf;
    uint64_t acc = 0; int n = 0;
    explicit BW(std::vector<uint8_t>& b) : buf(b) {}
    inline void put(uint32_t v, int k) {
        acc = (acc << k) | v; n += k;
        while (n >= 8) { n -= 8; buf.push_back(uint8_t(acc >> n)); }
    }
    inline void flush() { if (n) { buf.push_back(uint8_t(acc << (8 - n))); n = 0; } acc = 0; }
};

// ---------------- per-chunk compression ----------------
struct Tok { uint32_t lit_len, mlen, lexv, aexv; uint8_t lsym, lexb, asym, aexb; };

#ifndef HBBITS
#define HBBITS 18
#endif
#ifndef DEPTH
#define DEPTH 8
#endif
#ifndef LAZYMAX
#define LAZYMAX 0    // only lazy-check matches shorter than this
#endif
#ifndef INSMAX
#define INSMAX 0      // 0 = insert every position inside matches
#endif
static const int HB = HBBITS;      // hash table entries = 1<<HB
static const int CHDEPTH = DEPTH;       // hash-chain depth
static const uint32_t MAX_LITRUN = 1u << 24;

static inline uint32_t h4(const uint8_t* p) {
    uint32_t v; memcpy(&v, p, 4);
    return (v * 2654435761u) >> (32 - HB);
}

static void compress_chunk(const uint8_t* d, uint32_t len, std::vector<uint8_t>& out) {
    out.clear();

    static thread_local std::vector<int32_t> head, prevv;
    head.assign(size_t(1) << HB, -1);
    prevv.assign(len, -1);
    int32_t* H = head.data();
    int32_t* P = prevv.data();
    const long end = len;

    std::vector<Tok> toks;
    toks.reserve(len / 6 + 16);
    uint32_t freqM[MAIN_N] = {0}, freqA[A_N] = {0};
    uint32_t reps[4] = {1, 2, 4, 8};
    uint32_t litrun = 0, maxrun = 0, nseq = 0;

    // chain match search at position i (i itself may be in the table; skip it)
    auto chain_best = [&](long i, long& bl, long& bp) {
        bl = 0; bp = -1;
        int32_t c = H[h4(d + i)];
        int depth = CHDEPTH;
        const uint8_t* a = d + i;
        while (c >= 0 && depth-- > 0) {
            if (c == (long)i) { c = P[c]; continue; }
            const uint8_t* b = d + c;
            if (bl < end - i && a[bl] == b[bl]) {
                long l = 0;
                while (i + l + 8 <= end && memcmp(a + l, b + l, 8) == 0) l += 8;
                while (i + l < end && a[l] == b[l]) l++;
                if (l > bl) { bl = l; bp = c; }
            }
            c = P[c];
        }
    };
    auto insert = [&](long p) {
        if (p + 4 <= end) { uint32_t h = h4(d + p); P[p] = H[h]; H[h] = (int32_t)p; }
    };

    long i = 0;
#ifdef PZTIME
    double t0 = now_s();
#endif
    while (i + 4 <= end) {
        insert(i);
        long rbl = 0; int bri = -1;
        for (int r = 0; r < 4; r++) {
            long cp = i - (long)reps[r];
            if (cp < 0) continue;
            const uint8_t* a = d + i; const uint8_t* b = d + cp;
            long l = 0;
            while (i + l + 8 <= end && memcmp(a + l, b + l, 8) == 0) l += 8;
            while (i + l < end && a[l] == b[l]) l++;
            if (l > rbl) { rbl = l; bri = r; }
        }
        long bl = 0, bp = -1;
        if (rbl < 8) chain_best(i, bl, bp);
        bool userep = rbl >= 4 && (bl < 4 || rbl > bl || (rbl == bl && bri == 0));
        uint32_t mlen; long mp;
        if (userep) { mlen = (uint32_t)rbl; mp = i - (long)reps[bri]; }
        else { mlen = (uint32_t)bl; mp = bp; }
        if (mlen < 4) {
            freqM[d[i]]++; litrun++;
            if (litrun > maxrun) maxrun = litrun;
            if (litrun > MAX_LITRUN) break;
            i++;
            continue;
        }
#if LAZYMAX > 0
        if (mlen < LAZYMAX && i + 5 <= end) {
            long b2, p2;
            chain_best(i + 1, b2, p2);
            if (b2 > (long)mlen && !userep) {
                freqM[d[i]]++; litrun++;
                if (litrun > maxrun) maxrun = litrun;
                if (litrun > MAX_LITRUN) break;
                i++;
                continue;
            }
        }
#endif
        Tok t;
        t.lit_len = litrun; litrun = 0;
        t.mlen = mlen;
        uint32_t l = mlen;
        if (l < 20) { t.lsym = uint8_t(l - 4); t.lexb = 0; t.lexv = 0; }
        else {
            uint32_t lv = l - 20;
            uint32_t b = lv ? 31 - __builtin_clz(lv + 1) : 0;
            t.lsym = uint8_t(16 + b); t.lexb = uint8_t(b);
            t.lexv = lv - ((1u << b) - 1);
        }
        freqM[256 + t.lsym]++;
        if (userep) {
            t.asym = uint8_t(bri); t.aexb = 0; t.aexv = 0;
            if (bri > 0) { uint32_t tmp = reps[bri]; reps[bri] = reps[0]; reps[0] = tmp; }
        } else {
            uint32_t dist = uint32_t(i - mp);
            uint32_t v = dist - 1;
            if (v < 16) { t.asym = uint8_t(4 + v); t.aexb = 0; t.aexv = 0; }
            else {
                uint32_t b = 31 - __builtin_clz(v);
                t.asym = uint8_t(16 + b); t.aexb = uint8_t(b);
                t.aexv = v - (1u << b);
            }
            reps[3] = reps[2]; reps[2] = reps[1]; reps[1] = reps[0]; reps[0] = dist;
        }
        freqA[t.asym]++;
        toks.push_back(t);
#if INSMAX > 0
        if ((long)mlen <= INSMAX) {
            for (long p = i + 1; p < i + (long)mlen; p++) insert(p);
        } else {
            long lim = i + (long)mlen;
            long half = INSMAX / 2;
            for (long p = i + 1; p <= i + half; p++) insert(p);
            for (long p = lim - half; p < lim; p++) insert(p);
        }
#else
        for (long p = i + 1; p < i + (long)mlen; p++) insert(p);
#endif
        i += mlen;
        nseq++;
    }
    while (i < end) { freqM[d[i]]++; litrun++; i++; }
    if (litrun > maxrun) maxrun = litrun;
#ifdef PZTIME
    g_tparse += now_s() - t0; double t1 = now_s();
#endif

    if (maxrun > MAX_LITRUN) {
        out.push_back(0); append_be32(out, 8 + len);
        append_be64(out, fnv1a(d, len));
        size_t s = out.size(); out.resize(s + len); memcpy(out.data() + s, d, len);
        return;
    }
    uint8_t lensM[MAIN_N], lensA[A_N];
    huff_lengths(freqM, MAIN_N, lensM);
    huff_lengths(freqA, A_N, lensA);
    HuffCode cm, ca;
    canonical_codes(lensM, MAIN_N, cm);
    canonical_codes(lensA, A_N, ca);

    std::vector<uint8_t> bits;
    bits.reserve(len / 4);
    BW bw(bits);
    size_t cur = 0;
    for (const Tok& t : toks) {
        for (uint32_t k = 0; k < t.lit_len; k++) { uint8_t c = d[cur + k]; bw.put(cm.code[c], cm.len[c]); }
        cur += t.lit_len;
        bw.put(cm.code[256 + t.lsym], cm.len[256 + t.lsym]);
        if (t.lexb) bw.put(t.lexv, t.lexb);
        bw.put(ca.code[t.asym], ca.len[t.asym]);
        if (t.aexb) bw.put(t.aexv, t.aexb);
        cur += t.mlen;
    }
    while (cur < end) { uint8_t c = d[cur++]; bw.put(cm.code[c], cm.len[c]); }
    bw.flush();

    size_t need = 16 + MAIN_NIB + A_NIB + bits.size();
    if (need + 8 >= len) {
        out.push_back(0); append_be32(out, 8 + len);
        append_be64(out, fnv1a(d, len));
        size_t s = out.size(); out.resize(s + len); memcpy(out.data() + s, d, len);
        return;
    }
    out.push_back(1);
    append_be32(out, uint32_t(need));
    append_be32(out, len);
    append_be32(out, nseq);
    append_be64(out, fnv1a(d, len));
    write_nibbles(out, lensM, MAIN_N);
    write_nibbles(out, lensA, A_N);
    out.insert(out.end(), bits.begin(), bits.end());
#ifdef PZTIME
    g_temit += now_s() - t1;
#endif
}

// ---------------- parallel job runner ----------------
static void run_jobs(std::vector<std::pair<const uint8_t*, uint32_t>>& jobs,
                     std::vector<std::vector<uint8_t>>& outs,
                     void (*fn)(const uint8_t*, uint32_t, std::vector<uint8_t>&)) {
    outs.resize(jobs.size());
    int nt = get_threads();
    if ((size_t)nt > jobs.size()) nt = jobs.size() ? (int)jobs.size() : 1;
    if (nt <= 1) {
        for (size_t i = 0; i < jobs.size(); i++) fn(jobs[i].first, jobs[i].second, outs[i]);
        return;
    }
    std::atomic<size_t> next(0);
    auto worker = [&]() {
        for (;;) {
            size_t i = next.fetch_add(1);
            if (i >= jobs.size()) break;
            fn(jobs[i].first, jobs[i].second, outs[i]);
        }
    };
    std::vector<std::thread> th;
    for (int t = 0; t < nt - 1; t++) th.emplace_back(worker);  // main thread is the nth worker
    for (auto& x : th) x.join();
}

// ---------------- encode-dir ----------------
static int cmd_encode_dir(const char* indir, const char* outdir) {
    std::string names_path = std::string(indir) + "/names.bin";
    std::vector<uint8_t> names;
    if (!read_all(names_path.c_str(), names)) { fprintf(stderr, "pz1: cannot read names.bin\n"); return 1; }
    std::vector<PackedRec> recs;
    if (!parse_packed(names, recs)) { fprintf(stderr, "pz1: bad names.bin\n"); return 1; }
    size_t nrec = recs.size();
    std::vector<std::vector<uint8_t>> payloads(nrec);
    for (size_t r = 0; r < nrec; r++) {
        if (recs[r].plen != 0) { fprintf(stderr, "pz1: names.bin payloads must be empty\n"); return 1; }
        char fn[64];
        snprintf(fn, sizeof fn, "%s/%08zu.bin", indir, r);
        if (!read_all(fn, payloads[r])) { fprintf(stderr, "pz1: cannot read %s\n", fn); return 1; }
    }
    std::vector<uint32_t> rec_nch(nrec);
    size_t total_chunks = 0;
    for (size_t r = 0; r < nrec; r++) {
        rec_nch[r] = uint32_t((payloads[r].size() + CHUNK_SIZE - 1) / CHUNK_SIZE);
        total_chunks += rec_nch[r];
    }
    std::vector<std::pair<const uint8_t*, uint32_t>> jobs;
    jobs.reserve(total_chunks);
    for (size_t r = 0; r < nrec; r++) {
        const uint8_t* d = payloads[r].data();
        uint64_t n = payloads[r].size();
        for (uint32_t c = 0; c < rec_nch[r]; c++) {
            uint64_t off = uint64_t(c) * CHUNK_SIZE;
            uint32_t l = uint32_t(n - off > CHUNK_SIZE ? CHUNK_SIZE : n - off);
            jobs.push_back({d + off, l});
        }
    }
    std::vector<std::vector<uint8_t>> outs;
    run_jobs(jobs, outs, compress_chunk);

    // archive layout (HBA1): names.bin with empty payloads + index files
    // holding the compressed record bodies.
    std::vector<uint8_t> onames;
    onames.insert(onames.end(), MAGIC_HBA1, MAGIC_HBA1 + 4);
    onames.push_back(1);
    append_be32(onames, uint32_t(nrec));
    for (size_t r = 0; r < nrec; r++) {
        const std::string& a = recs[r].alias;
        append_be32(onames, uint32_t(a.size()));
        onames.insert(onames.end(), a.begin(), a.end());
        append_be64(onames, 0);
    }
    if (!write_all((std::string(outdir) + "/names.bin").c_str(), onames.data(), onames.size())) { fprintf(stderr, "pz1: cannot write names\n"); return 1; }
    size_t oi = 0;
    for (size_t r = 0; r < nrec; r++) {
        std::vector<uint8_t> body;
        append_be32(body, rec_nch[r]);
        for (uint32_t c = 0; c < rec_nch[r]; c++) {
            const std::vector<uint8_t>& b = outs[oi++];
            body.insert(body.end(), b.begin(), b.end());
        }
        char fn[64];
        snprintf(fn, sizeof fn, "%s/%08zu.bin", outdir, r);
        if (!write_all(fn, body.data(), body.size())) { fprintf(stderr, "pz1: cannot write body\n"); return 1; }
    }
#ifdef PZTIME
    fprintf(stderr, "parse %.3fs emit-serialize %.3fs (thread-parallel sums)\n", g_tparse, g_temit);
#endif
    return 0;
}

// ---------------- encode-stream ----------------
static int read_fd_all(int fd, std::vector<uint8_t>& buf) {
    char tmp[1 << 16];
    for (;;) {
        ssize_t got = read(fd, tmp, sizeof tmp);
        if (got < 0) return 0;
        if (got == 0) return 1;
        buf.insert(buf.end(), tmp, tmp + got);
    }
}
static int write_fd(int fd, const uint8_t* d, size_t n) {
    while (n) { ssize_t w = write(fd, d, n); if (w <= 0) return 0; d += w; n -= (size_t)w; }
    return 1;
}

static int cmd_encode_stream() {
    std::vector<uint8_t> in;
    if (!read_fd_all(0, in)) { fprintf(stderr, "pz1: read error\n"); return 1; }
    std::vector<PackedRec> recs;
    if (!parse_packed(in, recs)) { fprintf(stderr, "pz1: bad HBI1 input\n"); return 1; }
    size_t nrec = recs.size();
    std::vector<uint32_t> rec_nch(nrec);
    std::vector<std::pair<const uint8_t*, uint32_t>> jobs;
    for (size_t r = 0; r < nrec; r++) {
        rec_nch[r] = uint32_t((recs[r].plen + CHUNK_SIZE - 1) / CHUNK_SIZE);
        const uint8_t* d = in.data() + recs[r].poff;
        uint64_t n = recs[r].plen;
        for (uint32_t c = 0; c < rec_nch[r]; c++) {
            uint64_t off = uint64_t(c) * CHUNK_SIZE;
            uint32_t l = uint32_t(n - off > CHUNK_SIZE ? CHUNK_SIZE : n - off);
            jobs.push_back({d + off, l});
        }
    }
    std::vector<std::vector<uint8_t>> outs;
    run_jobs(jobs, outs, compress_chunk);
    std::vector<uint8_t> out;
    out.reserve(outs.size() * 1024 + 1024);
    out.insert(out.end(), MAGIC_HBA1, MAGIC_HBA1 + 4);
    out.push_back(1);
    append_be32(out, uint32_t(nrec));
    size_t oi = 0;
    for (size_t r = 0; r < nrec; r++) {
        const std::string& a = recs[r].alias;
        append_be32(out, uint32_t(a.size()));
        out.insert(out.end(), a.begin(), a.end());
        size_t lenpos = out.size();
        append_be64(out, 0);
        size_t start = out.size();
        append_be32(out, rec_nch[r]);
        for (uint32_t c = 0; c < rec_nch[r]; c++) {
            const std::vector<uint8_t>& b = outs[oi++];
            out.insert(out.end(), b.begin(), b.end());
        }
        be64(out.data() + lenpos, out.size() - start);
    }
    if (!write_fd(1, out.data(), out.size())) { fprintf(stderr, "pz1: write error\n"); return 1; }
    return 0;
}

int main(int argc, char** argv) {
    if (argc == 4 && !strcmp(argv[1], "encode-dir")) return cmd_encode_dir(argv[2], argv[3]);
    if (argc == 2 && !strcmp(argv[1], "encode-stream")) return cmd_encode_stream();
    fprintf(stderr, "usage: codec encode-dir <in> <out> | codec encode-stream\n");
    return 2;
}
