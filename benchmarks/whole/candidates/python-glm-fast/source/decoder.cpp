// decoder.cpp — PZ1 decoder (decode-dir / decode-stream).
//
// Reconstructs every byte from the archive plus this decoder alone. Each
// chunk is validated: mode/size bounds, table completeness, distance/length
// bounds, sequence count, and an FNV-1a64 checksum of the raw chunk bytes.
#include "pzformat.hpp"
#include <atomic>
#include <thread>
#include <new>
#include <dirent.h>
#include <unistd.h>
#include <sys/stat.h>

static int get_threads() {
    int t = 4;
    const char* e = getenv("COMPRESSION_LAB_THREADS");
    if (e && *e) { int v = atoi(e); if (v >= 1 && v <= 64) t = v; }
    return t;
}

// ---------------- bit reader (MSB-first) ----------------
struct BR {
    const uint8_t* p; const uint8_t* end;
    uint64_t acc = 0; int n = 0;
    BR(const uint8_t* d, const uint8_t* e) : p(d), end(e) {}
    inline void refill() { while (n <= 56) { uint64_t b = (p < end) ? *p++ : 0; acc = (acc << 8) | b; n += 8; } }
    inline uint32_t peek(int k) const { return uint32_t((acc >> (n - k)) & ((1u << k) - 1)); }
    inline void skip(int k) { n -= k; }
    inline uint32_t get(int k) { refill(); uint32_t v = peek(k); n -= k; return v; }
};

// Decode table: single-level 15-bit lookup, entry = (len<<9)|sym, 0 = hole.
static const int DBITS = 15;
static bool build_dtable(const uint8_t* lens, int n, std::vector<uint16_t>& t) {
    t.assign(1 << DBITS, 0);
    int bl_count[MAX_CODE_LEN + 1] = {0};
    for (int i = 0; i < n; i++) {
        if (lens[i] > MAX_CODE_LEN) return false;
        bl_count[lens[i]]++;
    }
    bl_count[0] = 0;
    uint32_t code = 0, total = 0;
    uint32_t first[MAX_CODE_LEN + 1];
    for (int b = 1; b <= MAX_CODE_LEN; b++) {
        code = (code + bl_count[b-1]) << 1;
        first[b] = code;
        total += uint32_t(bl_count[b]) << (DBITS - b);
    }
    bool any = false;
    for (int i = 0; i < n; i++) if (lens[i]) any = true;
    if (!any) return true;                      // empty table (unused)
    if (total != (1u << DBITS)) return false;   // incomplete/oversized code
    uint32_t cur[MAX_CODE_LEN + 1];
    for (int b = 1; b <= MAX_CODE_LEN; b++) cur[b] = first[b];
    for (int i = 0; i < n; i++) {
        int l = lens[i];
        if (!l) continue;
        uint32_t c = cur[l]++;
        int shift = DBITS - l;
        uint16_t e = uint16_t((l << 9) | i);
        uint32_t lo = c << shift, hi = (c + 1) << shift;
        if (hi > (1u << DBITS) || hi <= lo) return false;
        for (uint32_t x = lo; x < hi; x++) t[x] = e;
    }
    return true;
}

// Decode one chunk record (p at mode byte, pend = archive/stream end).
// Writes exactly `expect` raw bytes to dest; returns expect on success, 0 on
// any violation. Chunk payload must end within [p, pend).
static uint64_t decode_chunk_at(const uint8_t* p, const uint8_t* pend, uint8_t* dest, uint32_t expect) {
    if (expect == 0 || expect > CHUNK_SIZE || p + 5 > pend) return 0;
    uint8_t mode = *p++;
    uint32_t cbytes = rd32(p); p += 4;
    if (cbytes > uint32_t(pend - p)) return 0;
    const uint8_t* q = p;
    if (mode == 0) {
        if (cbytes != 8 + expect) return 0;
        if (fnv1a(q + 8, expect) != rd64(q)) return 0;
        memcpy(dest, q + 8, expect);
        return expect;
    }
    if (mode != 1) return 0;
    if (cbytes < 16 + MAIN_NIB + A_NIB) return 0;
    uint32_t raw_len = rd32(q);
    uint32_t nseq = rd32(q + 4);
    uint64_t sum = rd64(q + 8);
    q += 16;
    if (raw_len != expect) return 0;
    uint8_t lensM[MAIN_N], lensA[A_N];
    read_nibbles(q, MAIN_N, lensM);
    read_nibbles(q + MAIN_NIB, A_N, lensA);
    q += MAIN_NIB + A_NIB;
    std::vector<uint16_t> tM, tA;
    if (!build_dtable(lensM, MAIN_N, tM)) return 0;
    if (!build_dtable(lensA, A_N, tA)) return 0;
    BR br(q, p + cbytes);
    uint32_t reps[4] = {1, 2, 4, 8};
    size_t on = 0;
    uint32_t seqs = 0;
    while (on < raw_len) {
        br.refill();
        uint32_t e = tM[br.peek(DBITS)];
        if (!e) return 0;
        br.skip(e >> 9);
        uint32_t sym = e & 511;
        if (sym < 256) { dest[on++] = (uint8_t)sym; continue; }
        uint32_t ls = sym - 256, len;
        if (ls < 16) len = ls + 4;
        else { uint32_t b = ls - 16; len = 20 + ((1u << b) - 1) + (b ? br.get(b) : 0); }
        if (++seqs > nseq) return 0;
        br.refill();
        uint32_t ea = tA[br.peek(DBITS)];
        if (!ea) return 0;
        br.skip(ea >> 9);
        uint32_t asym = ea & 511;
        uint32_t dist;
        if (asym < 4) {
            dist = reps[asym];
            if (asym) { uint32_t t = reps[asym]; reps[asym] = reps[0]; reps[0] = t; }
        } else {
            uint32_t ds = asym - 4;
            if (ds < 16) dist = ds + 1;
            else {
                uint32_t b = ds - 12;
                if (b > 21) return 0;
                dist = 1 + (1u << b) + br.get(b);
            }
            if (dist > on) return 0;
            reps[3] = reps[2]; reps[2] = reps[1]; reps[1] = reps[0]; reps[0] = dist;
        }
        if (dist > on || len > raw_len - on) return 0;
        uint8_t* dst = dest + on;
        const uint8_t* src = dst - dist;
        if (dist >= len) {
            memcpy(dst, src, len);
        } else if (dist >= 8) {
            uint32_t done = 0;
            while (done < len) {
                uint32_t step = len - done < dist ? len - done : dist;
                memcpy(dst + done, src + done, step);
                done += step;
            }
        } else {
            for (uint32_t k = 0; k < len; k++) dst[k] = src[k];
        }
        on += len;
    }
    if (seqs != nseq) return 0;
    if (fnv1a(dest, raw_len) != sum) return 0;
    // strict padding: every unconsumed bit and byte must be zero
    {
        uint64_t remmask = br.n >= 64 ? ~0ULL : (br.n ? ((1ULL << br.n) - 1) : 0);
        if (br.acc & remmask) return 0;
        for (const uint8_t* z = br.p; z < br.end; z++) if (*z) return 0;
    }
    return raw_len;
}

// ---------------- body walking ----------------
struct WalkChunk { const uint8_t* p; const uint8_t* pend; uint8_t* dest; uint32_t expect; };
static void run_decode_jobs(std::vector<WalkChunk>& jobs, std::vector<uint64_t>& got) {
    got.assign(jobs.size(), 0);
    int nt = get_threads();
    if ((size_t)nt > jobs.size()) nt = jobs.size() ? (int)jobs.size() : 1;
    if (nt <= 1) {
        for (size_t i = 0; i < jobs.size(); i++)
            got[i] = decode_chunk_at(jobs[i].p, jobs[i].pend, jobs[i].dest, jobs[i].expect);
        return;
    }
    std::atomic<size_t> next(0);
    auto worker = [&]() {
        for (;;) {
            size_t i = next.fetch_add(1);
            if (i >= jobs.size()) break;
            got[i] = decode_chunk_at(jobs[i].p, jobs[i].pend, jobs[i].dest, jobs[i].expect);
        }
    };
    std::vector<std::thread> th;
    for (int t = 0; t < nt - 1; t++) th.emplace_back(worker);  // main thread is the nth worker
    for (auto& x : th) x.join();
    for (size_t i = 0; i < jobs.size(); i++) if (!got[i]) got[i] = 0;
}

// ---------------- decode-dir ----------------
static int cmd_decode_dir(const char* indir, const char* outdir) {
    // archive dir: names.bin (HBA1, empty payloads) + index files with
    // compressed record bodies. Output: names.bin (HBI1) + raw index files.
    std::vector<uint8_t> names_in;
    if (!read_all((std::string(indir) + "/names.bin").c_str(), names_in)) { fprintf(stderr, "pz1: cannot read names.bin\n"); return 1; }
    std::vector<PackedRec> recs;
    if (!parse_packed(names_in, recs)) { fprintf(stderr, "pz1: bad names.bin\n"); return 1; }
    uint32_t nrec = (uint32_t)recs.size();
    for (uint32_t r = 0; r < nrec; r++) {
        if (recs[r].plen != 0) { fprintf(stderr, "pz1: archive names payloads must be empty\n"); return 1; }
    }
    // strict dir scan: exactly names.bin + %08u.bin, no extras
    {
        DIR* dp = opendir(indir);
        if (!dp) { fprintf(stderr, "pz1: cannot open input dir\n"); return 1; }
        struct stat st;
        struct dirent* de;
        std::vector<std::string> found;
        while ((de = readdir(dp))) {
            if (!strcmp(de->d_name, ".") || !strcmp(de->d_name, "..")) continue;
            found.push_back(de->d_name);
            std::string full = std::string(indir) + "/" + de->d_name;
            if (lstat(full.c_str(), &st) != 0 || !S_ISREG(st.st_mode)) { closedir(dp); fprintf(stderr, "pz1: non-regular entry\n"); return 1; }
        }
        closedir(dp);
        if (found.size() != (size_t)nrec + 1) { fprintf(stderr, "pz1: unexpected archive dir contents\n"); return 1; }
        for (const std::string& f : found) {
            if (f == "names.bin") continue;
            bool ok = false;
            if (f.size() == 12) {
                ok = true;
                uint32_t idx = 0;
                for (int k = 0; k < 8; k++) {
                    if (f[k] < '0' || f[k] > '9') ok = false;
                    else idx = idx * 10 + (f[k] - '0');
                }
                if (f[8] != '.' || f[9] != 'b' || f[10] != 'i' || f[11] != 'n' || idx >= nrec) ok = false;
            }
            if (!ok) { fprintf(stderr, "pz1: unexpected file %s\n", f.c_str()); return 1; }
        }
    }
    std::vector<std::vector<uint8_t>> bodies(nrec);
    for (uint32_t r = 0; r < nrec; r++) {
        char fn[64];
        snprintf(fn, sizeof fn, "%s/%08u.bin", indir, r);
        if (!read_all(fn, bodies[r])) { fprintf(stderr, "pz1: cannot read body %u\n", r); return 1; }
    }
    // walk each body to learn raw sizes, then parallel decode
    std::vector<uint64_t> sizes(nrec);
    std::vector<std::vector<WalkChunk>> per_rec(nrec);
    size_t total_jobs = 0;
    for (uint32_t r = 0; r < nrec; r++) {
        const uint8_t* bp = bodies[r].data();
        size_t bn = bodies[r].size();
        if (bn < 4) { fprintf(stderr, "pz1: bad body\n"); return 1; }
        uint32_t nch = rd32(bp);
        if (nch > (bn - 4) / 5 + 1) { fprintf(stderr, "pz1: bad chunk count\n"); return 1; }
        const uint8_t* p = bp + 4;
        const uint8_t* pend = bp + bn;
        uint64_t total = 0;
        for (uint32_t c = 0; c < nch; c++) {
            if (p + 5 > pend) { fprintf(stderr, "pz1: truncated chunk\n"); return 1; }
            uint8_t mode = p[0];
            uint32_t cbytes = rd32(p + 1);
            if (cbytes > uint32_t(pend - p - 5)) { fprintf(stderr, "pz1: chunk overflow\n"); return 1; }
            if (mode == 0) { if (cbytes < 8) { fprintf(stderr, "pz1: bad raw chunk\n"); return 1; } total += cbytes - 8; }
            else if (mode == 1) {
                if (cbytes < 16 + MAIN_NIB + A_NIB) { fprintf(stderr, "pz1: bad chunk\n"); return 1; }
                total += rd32(p + 5);
            } else { fprintf(stderr, "pz1: bad chunk mode\n"); return 1; }
            p += 5 + cbytes;
        }
        if (p != pend) { fprintf(stderr, "pz1: bad body end\n"); return 1; }
        sizes[r] = total;
        per_rec[r].reserve(nch);
    }
    std::vector<std::vector<uint8_t>> outs(nrec);
    try {
        for (uint32_t r = 0; r < nrec; r++) outs[r].resize((size_t)sizes[r]);
    } catch (const std::bad_alloc&) { fprintf(stderr, "pz1: allocation failed\n"); return 1; }
    std::vector<WalkChunk> jobs;
    for (uint32_t r = 0; r < nrec; r++) {
        const uint8_t* bp = bodies[r].data();
        uint32_t nch = rd32(bp);
        const uint8_t* p = bp + 4;
        for (uint32_t c = 0; c < nch; c++) {
            uint64_t off = uint64_t(c) * CHUNK_SIZE;
            uint32_t cl = uint32_t(sizes[r] - off > CHUNK_SIZE ? CHUNK_SIZE : sizes[r] - off);
            (void)cl;
            uint8_t mode = p[0];
            uint32_t cbytes = rd32(p + 1);
            uint32_t expect = mode == 0 ? cbytes - 8 : rd32(p + 5);
            jobs.push_back({p, p + 5 + cbytes, outs[r].data() + off, expect});
            p += 5 + cbytes;
        }
    }
    std::vector<uint64_t> got;
    run_decode_jobs(jobs, got);
    for (size_t i = 0; i < got.size(); i++) {
        if (!got[i]) { fprintf(stderr, "pz1: chunk decode failed\n"); return 1; }
    }
    // output: names.bin (HBI1, empty payloads) + raw index files
    std::vector<uint8_t> names;
    names.insert(names.end(), MAGIC_HBI1, MAGIC_HBI1 + 4);
    names.push_back(1);
    append_be32(names, nrec);
    for (uint32_t r = 0; r < nrec; r++) {
        append_be32(names, uint32_t(recs[r].alias.size()));
        names.insert(names.end(), recs[r].alias.begin(), recs[r].alias.end());
        append_be64(names, 0);
    }
    if (!write_all((std::string(outdir) + "/names.bin").c_str(), names.data(), names.size())) { fprintf(stderr, "pz1: write names failed\n"); return 1; }
    for (uint32_t r = 0; r < nrec; r++) {
        char fn[64];
        snprintf(fn, sizeof fn, "%s/%08u.bin", outdir, r);
        if (!write_all(fn, outs[r].data(), outs[r].size())) { fprintf(stderr, "pz1: write payload failed\n"); return 1; }
    }
    return 0;
}

// ---------------- decode-stream ----------------
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

static int cmd_decode_stream() {
    std::vector<uint8_t> in;
    if (!read_fd_all(0, in)) { fprintf(stderr, "pz1: read error\n"); return 1; }
    if (in.size() < 9 || memcmp(in.data(), MAGIC_HBA1, 4) != 0 || in[4] != 1) { fprintf(stderr, "pz1: bad HBA1 input\n"); return 1; }
    uint32_t nrec = rd32(in.data() + 5);
    if (nrec == 0 || nrec > 1000000) { fprintf(stderr, "pz1: bad record count\n"); return 1; }
    size_t p = 9;
    std::vector<std::string> aliases(nrec);
    std::vector<size_t> body_off(nrec), body_len(nrec);
    for (uint32_t r = 0; r < nrec; r++) {
        if (p + 4 > in.size()) { fprintf(stderr, "pz1: truncated\n"); return 1; }
        uint32_t alen = rd32(in.data() + p); p += 4;
        if (alen == 0 || alen > 1048576 || p + alen + 8 > in.size()) { fprintf(stderr, "pz1: bad record\n"); return 1; }
        if (!alias_ok(in.data() + p, alen)) { fprintf(stderr, "pz1: bad alias\n"); return 1; }
        aliases[r].assign((const char*)in.data() + p, alen);
        p += alen;
        if (r && aliases[r] <= aliases[r-1]) { fprintf(stderr, "pz1: alias order\n"); return 1; }
        uint64_t blen = rd64(in.data() + p); p += 8;
        if (blen > in.size() - p) { fprintf(stderr, "pz1: payload overflow\n"); return 1; }
        body_off[r] = p;
        body_len[r] = (size_t)blen;
        p += blen;
    }
    if (p != in.size()) { fprintf(stderr, "pz1: trailing bytes\n"); return 1; }
    std::vector<std::vector<uint8_t>> outs(nrec);
    std::vector<uint64_t> raw(nrec);
    try {
        for (uint32_t r = 0; r < nrec; r++) {
            if (body_len[r] < 4) { fprintf(stderr, "pz1: bad body\n"); return 1; }
            // determine raw size by walking chunk headers
            const uint8_t* bp = in.data() + body_off[r] + 4;
            const uint8_t* bend = in.data() + body_off[r] + body_len[r];
            uint32_t nch = rd32(in.data() + body_off[r]);
            uint64_t total = 0;
            for (uint32_t c = 0; c < nch; c++) {
                if (bp + 5 > bend) { fprintf(stderr, "pz1: bad chunk\n"); return 1; }
                uint8_t mode = bp[0];
                uint32_t cbytes = rd32(bp + 1);
                if (cbytes > uint32_t(bend - bp - 5)) { fprintf(stderr, "pz1: bad chunk\n"); return 1; }
                if (mode == 0) total += cbytes > 8 ? cbytes - 8 : 0;
                else if (mode == 1) {
                    if (cbytes < 16 + MAIN_NIB + A_NIB) { fprintf(stderr, "pz1: bad chunk\n"); return 1; }
                    total += rd32(bp + 5);
                } else { fprintf(stderr, "pz1: bad chunk mode\n"); return 1; }
                bp += 5 + cbytes;
            }
            if (bp != bend) { fprintf(stderr, "pz1: bad body end\n"); return 1; }
            raw[r] = total;
            outs[r].resize((size_t)total);
        }
    } catch (const std::bad_alloc&) { fprintf(stderr, "pz1: allocation failed\n"); return 1; }
    std::vector<WalkChunk> jobs;
    for (uint32_t r = 0; r < nrec; r++) {
        const uint8_t* bp = in.data() + body_off[r];
        uint32_t nch = rd32(bp);
        const uint8_t* p = bp + 4;
        for (uint32_t c = 0; c < nch; c++) {
            uint64_t off = uint64_t(c) * CHUNK_SIZE;
            uint8_t mode = p[0];
            uint32_t cbytes = rd32(p + 1);
            uint32_t expect = mode == 0 ? cbytes - 8 : rd32(p + 5);
            jobs.push_back({p, p + 5 + cbytes, outs[r].data() + off, expect});
            p += 5 + cbytes;
        }
    }
    std::vector<uint64_t> got;
    run_decode_jobs(jobs, got);
    size_t ji = 0;
    for (uint32_t r = 0; r < nrec; r++) {
        uint64_t tot = 0;
        uint32_t nch = rd32(in.data() + body_off[r]);
        for (uint32_t c = 0; c < nch; c++) tot += got[ji++];
        if (tot != raw[r]) { fprintf(stderr, "pz1: decode failed (record %u)\n", r); return 1; }
    }
    std::vector<uint8_t> out;
    out.insert(out.end(), MAGIC_HBI1, MAGIC_HBI1 + 4);
    out.push_back(1);
    append_be32(out, nrec);
    for (uint32_t r = 0; r < nrec; r++) {
        append_be32(out, uint32_t(aliases[r].size()));
        out.insert(out.end(), aliases[r].begin(), aliases[r].end());
        append_be64(out, raw[r]);
        if (raw[r]) out.insert(out.end(), outs[r].begin(), outs[r].end());
    }
    if (!write_fd(1, out.data(), out.size())) { fprintf(stderr, "pz1: write error\n"); return 1; }
    return 0;
}

int main(int argc, char** argv) {
    if (argc == 4 && !strcmp(argv[1], "decode-dir")) return cmd_decode_dir(argv[2], argv[3]);
    if (argc == 2 && !strcmp(argv[1], "decode-stream")) return cmd_decode_stream();
    fprintf(stderr, "usage: decoder decode-dir <in> <out> | decoder decode-stream\n");
    return 2;
}
