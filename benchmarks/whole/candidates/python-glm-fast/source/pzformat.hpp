// pzformat.hpp — shared container/format core for PZ1 fast LZ codec.
// Format: deflate-style LZ77 with 4 repeated-offset slots, per-chunk static
// canonical Huffman (max code length 15), extra bits inline MSB-first.
//
// Token alphabets:
//   MAIN (294 syms): 0..255 literal bytes; 256..293 match lengths:
//     ls<16      -> len = ls + 4                       (4..19)
//     ls = 16+b  -> len = 20 + (2^b - 1) + extra, eb=b (b = 0..21)
//   A (38 syms): 0..3 rep0..3; 4..37 new distances:
//     asym=4+v, v=dist-1<16  -> dist = v + 1               (1..16)
//     asym=16+b              -> dist = 1 + 2^b + extra, eb=b (b = 4..21)
// Rep state per chunk: {1,2,4,8}; rep_i>0 swaps with rep0; new dist shifts.
#pragma once
#include <cstdint>
#include <cstring>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>
#include <algorithm>
#include <utility>

static const uint32_t CHUNK_SIZE = 4u << 20;   // 4 MiB chunks
static const int MAX_CODE_LEN = 15;
static const int MAIN_N = 294;
static const int A_N = 38;

// ---------------- I/O helpers ----------------
static bool read_all(const char* path, std::vector<uint8_t>& out) {
    FILE* f = fopen(path, "rb");
    if (!f) return false;
    if (fseek(f, 0, SEEK_END) != 0) { fclose(f); return false; }
    long sz = ftell(f);
    if (sz < 0) { fclose(f); return false; }
    if (fseek(f, 0, SEEK_SET) != 0) { fclose(f); return false; }
    out.resize((size_t)sz);
    size_t got = sz ? fread(out.data(), 1, (size_t)sz, f) : 0;
    fclose(f);
    return got == (size_t)sz;
}

static bool write_all(const char* path, const void* d, size_t n) {
    FILE* f = fopen(path, "wb");
    if (!f) return false;
    bool ok = n == 0 || fwrite(d, 1, n, f) == n;
    ok &= fclose(f) == 0;
    return ok;
}

// ---------------- strict HBI1/HBA1 packed parser ----------------
// Header: 4B magic, 1B version 1, 4B BE positive record count (<=1e6).
// Record: 4B BE alias len, alias bytes, 8B BE payload len, payload bytes.
static inline bool alias_ok(const uint8_t* a, uint32_t n) {
    if (n == 0 || n > 1048576) return false;
    bool word = false;
    for (uint32_t i = 0; i < n; i++) {
        uint8_t c = a[i];
        if ((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')) { word = true; continue; }
        if (c == '-') { if (!word) return false; word = false; continue; }
        return false;
    }
    return word;
}

static const uint8_t MAGIC_HBI1[4] = {'H', 'B', 'I', '1'};
static const uint8_t MAGIC_HBA1[4] = {'H', 'B', 'A', '1'};

// Parse packed stream; payload offsets/lengths reported per record (payload
// left in `d` so names files with empty payloads and object streams both work).
struct PackedRec { std::string alias; uint64_t plen; size_t poff; };
static bool parse_packed(const std::vector<uint8_t>& d, std::vector<PackedRec>& recs) {
    if (d.size() < 9) return false;
    if (memcmp(d.data(), MAGIC_HBI1, 4) != 0 && memcmp(d.data(), MAGIC_HBA1, 4) != 0) return false;
    if (d[4] != 1) return false;
    uint32_t cnt = (uint32_t(d[5]) << 24) | (uint32_t(d[6]) << 16) | (uint32_t(d[7]) << 8) | d[8];
    if (cnt == 0 || cnt > 1000000) return false;
    size_t p = 9;
    recs.reserve(cnt);
    for (uint32_t i = 0; i < cnt; i++) {
        if (p + 4 > d.size()) return false;
        uint32_t alen = (uint32_t(d[p]) << 24) | (uint32_t(d[p+1]) << 16) | (uint32_t(d[p+2]) << 8) | d[p+3];
        p += 4;
        if (alen == 0 || alen > 1048576 || p + alen > d.size()) return false;
        if (!alias_ok(d.data() + p, alen)) return false;
        PackedRec r;
        r.alias.assign((const char*)d.data() + p, alen);
        p += alen;
        if (p + 8 > d.size()) return false;
        uint64_t plen = 0;
        for (int k = 0; k < 8; k++) plen = (plen << 8) | d[p + k];
        p += 8;
        if (p + plen > d.size()) return false;
        r.plen = plen;
        r.poff = p;
        p += plen;
        if (!recs.empty() && !(r.alias > recs.back().alias)) return false;  // strictly increasing
        recs.push_back(std::move(r));
    }
    return p == d.size();
}

static void be32(uint8_t* o, uint32_t v) { o[0]=v>>24; o[1]=v>>16; o[2]=v>>8; o[3]=v; }
static uint32_t rd32(const uint8_t* p) { return (uint32_t(p[0])<<24)|(uint32_t(p[1])<<16)|(uint32_t(p[2])<<8)|p[3]; }
static void be64(uint8_t* o, uint64_t v) { for (int k = 0; k < 8; k++) o[k] = uint8_t(v >> (56 - 8*k)); }
static uint64_t rd64(const uint8_t* p) { uint64_t v=0; for (int k=0;k<8;k++) v=(v<<8)|p[k]; return v; }

static void append_be32(std::vector<uint8_t>& o, uint32_t v) { size_t s=o.size(); o.resize(s+4); be32(o.data()+s, v); }
static void append_be64(std::vector<uint8_t>& o, uint64_t v) { size_t s=o.size(); o.resize(s+8); be64(o.data()+s, v); }

// ---------------- checksum ----------------
static inline uint64_t fnv1a(const uint8_t* d, size_t n) {
    uint64_t h = 0xcbf29ce484222325ull;
    for (size_t i = 0; i < n; i++) { h ^= d[i]; h *= 0x100000001b3ull; }
    return h;
}

// ---------------- chunk payload layout ----------------
// mode 0 (raw):  payload = raw bytes
// mode 1 (lz):   u32 raw_len | u32 nseq | u64 fnv(raw chunk) |
//                MAIN nibbles (147 B) | A nibbles (17 B) | bitstream
static const int MAIN_NIB = (MAIN_N + 1) / 2;  // 147
static const int A_NIB = (A_N + 1) / 2;        // 19

static void write_nibbles(std::vector<uint8_t>& o, const uint8_t* lens, int n) {
    size_t s = o.size();
    o.resize(s + (size_t)((n + 1) / 2));
    for (int i = 0; i < n; i += 2) {
        o[s + i/2] = uint8_t(lens[i] | (i + 1 < n ? lens[i+1] << 4 : 0));
    }
}
static void read_nibbles(const uint8_t* p, int n, uint8_t* lens) {
    for (int i = 0; i < n; i += 2) {
        lens[i] = p[i/2] & 0xF;
        if (i + 1 < n) lens[i+1] = p[i/2] >> 4;
    }
}

// ---------------- container (archive / stream bodies) ----------------
// Archive file: "PZ1A" | u32 nrec | per rec {u32 alen | alias | u64 raw_size}
//               then per rec body: u32 nchunks | chunks...
// Stream body (per record payload inside HBA1 we emit): same "body" layout.
// Chunk: u8 mode | u32 cbytes (payload size) | payload
static const uint8_t MAGIC_PZ1A[4] = {'P', 'Z', '1', 'A'};
