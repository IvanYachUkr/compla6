// ybz — schema-aware columnar codec for the yelp business JSONL corpus.
// Shared definitions: archive format constants, little-endian IO, and the
// interleaved rANS range decoder used by both encoder and decoder binaries.
#pragma once

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

// ---------------- archive format ----------------
// Header (24 bytes, little-endian):
//   u8[4] magic "YB01" | u8 mode (0=template,1=raw) | u8 flags (bit0: trailing
//   newline) | u16 zero | u64 orig_len | u64 nrec | u32 nkeys | u32 zero
// mode 1: the original payload bytes follow the header verbatim.
// mode 0: a fixed sequence of self-describing rANS sections (see decoder.cpp).

static const uint8_t YBZ_MAGIC[4] = {'Y', 'B', '0', '1'};
static const uint8_t HBI_MAGIC[4] = {'H', 'B', 'I', '1'};
static const uint8_t HBA_MAGIC[4] = {'H', 'B', 'A', '1'};

static const int YB_ID_LEN = 22;                    // business_id chars
static const int YB_ID_PACKED = 17;                 // 22*6 bits -> 132 -> 17 bytes
static const int YB_NPOS = 10;                      // lat/lon fraction digit streams
static const int YB_TOPKS = 1024;                   // common keyset symbols
static const int YB_KS_ESC = YB_TOPKS;              // escape symbol
static const int YB_KS_NULL = YB_TOPKS + 1;         // attributes:null symbol

static const char YB_ID_ALPHABET[65] =
    "-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz";

// template pieces between values, in field order (each piece starts after the
// previous value's closing quote)
static const char YB_T1[] = "{\"business_id\":\"";
static const char YB_T2[] = ",\"name\":\"";
static const char YB_T3[] = ",\"address\":\"";
static const char YB_T4[] = ",\"city\":\"";
static const char YB_T5[] = ",\"state\":\"";
static const char YB_T6[] = ",\"postal_code\":\"";
static const char YB_T7[] = ",\"latitude\":";
static const char YB_T8[] = ",\"longitude\":";
static const char YB_T9[] = ",\"stars\":";
static const char YB_T10[] = ",\"review_count\":";
static const char YB_T11[] = ",\"is_open\":";
static const char YB_T12[] = ",\"attributes\":";
static const char YB_T13[] = ",\"categories\":";
static const char YB_T14[] = ",\"hours\":";
static const char YB_T15[] = "}";
static const char YB_NULL[] = "null";

#define YB_FAIL(msg)                                     \
    do {                                                 \
        std::fprintf(stderr, "ybz: %s\n", msg);          \
        std::exit(1);                                    \
    } while (0)

// ---------------- little-endian IO ----------------
static inline uint16_t yb_get16(const uint8_t* p) { uint16_t v; std::memcpy(&v, p, 2); return v; }
static inline uint32_t yb_get32(const uint8_t* p) { uint32_t v; std::memcpy(&v, p, 4); return v; }
static inline uint64_t yb_get64(const uint8_t* p) { uint64_t v; std::memcpy(&v, p, 8); return v; }

// alias contract: [a-z0-9]+(-[a-z0-9]+)*, 1..1048576 bytes
static inline bool yb_alias_ok(const uint8_t* a, uint32_t n) {
    if (n < 1 || n > 1048576) return false;
    for (uint32_t i = 0; i < n; i++) {
        uint8_t ch = a[i];
        bool ok = (ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9') ||
                  (ch == '-' && i > 0 && i + 1 < n && a[i - 1] != '-');
        if (!ok) return false;
    }
    return true;
}
static inline uint64_t yb_get64be(const uint8_t* p) {
    return ((uint64_t)p[0] << 56) | ((uint64_t)p[1] << 48) | ((uint64_t)p[2] << 40) |
           ((uint64_t)p[3] << 32) | ((uint64_t)p[4] << 24) | ((uint64_t)p[5] << 16) |
           ((uint64_t)p[6] << 8) | (uint64_t)p[7];
}
static inline uint32_t yb_get32be(const uint8_t* p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}
static inline void yb_put64be(uint8_t* p, uint64_t v) {
    for (int i = 0; i < 8; i++) p[i] = (uint8_t)(v >> (56 - 8 * i));
}
static inline void yb_put32be(uint8_t* p, uint32_t v) {
    p[0] = (uint8_t)(v >> 24); p[1] = (uint8_t)(v >> 16); p[2] = (uint8_t)(v >> 8); p[3] = (uint8_t)v;
}
static inline void yb_put16(uint8_t* p, uint16_t v) { std::memcpy(p, &v, 2); }
static inline void yb_put32(uint8_t* p, uint32_t v) { std::memcpy(p, &v, 4); }
static inline void yb_put64(uint8_t* p, uint64_t v) { std::memcpy(p, &v, 8); }

// ---------------- integrity hash (word-mixed, xxhash-like) ----------------
static inline uint64_t yb_hash64(const uint8_t* p, size_t n) {
    uint64_t h = 0x9E3779B97F4A7C15ull ^ (uint64_t)n;
    while (n >= 8) {
        h ^= yb_get64(p);
        h *= 0xff51afd7ed558ccdull;
        h ^= h >> 31;
        p += 8;
        n -= 8;
    }
    uint64_t t = 0;
    for (size_t i = 0; i < n; i++) t |= (uint64_t)p[i] << (8 * i);
    h ^= t;
    h *= 0xff51afd7ed558ccdull;
    h ^= h >> 29;
    h *= 0xc4ceb9fe1a85ec53ull;
    h ^= h >> 32;
    h ^= h >> 29;
    return h;
}


// bounds-checked sequential reader (decoder side: truncation is fatal)
struct YbRd {
    const uint8_t* p;
    const uint8_t* end;
    void need(size_t n) const {
        if ((size_t)(end - p) < n) YB_FAIL("truncated archive");
    }
    uint8_t u8() { need(1); return *p++; }
    uint16_t u16() { need(2); uint16_t v = yb_get16(p); p += 2; return v; }
    uint32_t u32() { need(4); uint32_t v = yb_get32(p); p += 4; return v; }
    uint64_t u64() { need(8); uint64_t v = yb_get64(p); p += 8; return v; }
    const uint8_t* raw(size_t n) { need(n); const uint8_t* q = p; p += n; return q; }
};

// ---------------- interleaved rANS (32-bit state, 16-bit lanes, 10..12-bit probs) ----------------
#define YB_RANS_SCALE 12
#define YB_RANS_M 4096u
#define YB_RANS_L 65536u
#define YB_RANS_SCALE1 10   // order-1 text streams use 1024-slot tables (cache-friendly)

struct YbRansDec {
    uint32_t x;
    const uint8_t* ptr;
    const uint8_t* end;
    void init(const uint8_t* p, const uint8_t* end_) {
        if (end_ - p < 4) YB_FAIL("rans stream too short");
        x = yb_get32(p);
        ptr = p + 4;
        end = end_;
    }
    inline void renorm() {
        if (x < YB_RANS_L) {
            if (end - ptr < 2) YB_FAIL("rans stream overrun");
            x = (x << 16) | yb_get16(ptr);
            ptr += 2;
        }
    }
};

// slot table entry: for state slot s in [cum, cum+freq), the decoded symbol is sym.
struct YbSlot {
    uint16_t cum, freq, sym;
};

// order-0 model: one slot table
struct YbTab0 {
    std::vector<YbSlot> slots;
    void build(const std::vector<uint16_t>& syms, const std::vector<uint16_t>& freqs) {
        slots.assign(YB_RANS_M, {0, 0, 0});
        uint32_t cum = 0;
        for (size_t i = 0; i < syms.size(); i++) {
            uint32_t f = freqs[i];
            for (uint32_t s = 0; s < f; s++) slots[cum + s] = {(uint16_t)cum, (uint16_t)f, syms[i]};
            cum += f;
        }
            if (cum != YB_RANS_M && syms.size() > 0) {
                std::fprintf(stderr, "t0 cum=%u M=%u dc=%zu\n", cum, YB_RANS_M, syms.size());
                YB_FAIL("freq table does not sum to M");
            }
    }
};

// order-1 model: nctx slot tables back to back
struct YbTab1 {
    std::vector<YbSlot> slots;  // nctx * M
    uint32_t nctx;
    uint32_t M;
    void build(uint32_t nctx_, uint32_t M_, const std::vector<std::vector<uint16_t>>& syms,
               const std::vector<std::vector<uint16_t>>& freqs) {
        nctx = nctx_;
        M = M_;
        slots.assign((size_t)nctx * M, {0, 0, 0});
        for (uint32_t c = 0; c < nctx; c++) {
            YbSlot* t = &slots[(size_t)c * M];
            uint32_t cum = 0;
            const std::vector<uint16_t>& sy = syms[c];
            const std::vector<uint16_t>& fr = freqs[c];
            for (size_t i = 0; i < sy.size(); i++) {
                uint32_t f = fr[i];
                for (uint32_t s = 0; s < f; s++) t[cum + s] = {(uint16_t)cum, (uint16_t)f, sy[i]};
                cum += f;
            }
            if (cum != M && sy.size() > 0) YB_FAIL("freq table does not sum to M");
        }
    }
    inline const YbSlot* tab(uint32_t ctx) const { return &slots[(size_t)ctx * M]; }
};

// decode one symbol with a parameterized precision (logM) slot table
static inline uint32_t yb_rans_dec_sym_m(YbRansDec* d, const YbSlot* tab, uint32_t logM) {
    d->renorm();
    uint32_t M = 1u << logM;
    uint32_t slot = d->x & (M - 1);
    const YbSlot& s = tab[slot];
    if (s.freq == 0) YB_FAIL("invalid rANS state");
    d->x = (uint32_t)s.freq * (d->x >> logM) + slot - s.cum;
    return s.sym;
}
static inline uint32_t yb_rans_dec_sym(YbRansDec* d, const YbSlot* tab) {
    return yb_rans_dec_sym_m(d, tab, YB_RANS_SCALE);
}

// ---------------- stream container ----------------
// stream := u32 nsyms | u8 ilv (1|4|8) | u8 order (0|1) | u8 logM | u8 zero | u16 ntab
//   order 0: ntab==1: u16 D, D x {u16 sym, u16 freq}
//   order 1: u16 symD; ntab groups of (u16 D, D x {u16 sym, u16 freq})
//   then ilv x u32 substream nbytes, then payload bytes.
struct YbStreamIn {
    uint32_t nsyms;
    uint32_t ilv;
    bool order1;
    std::vector<uint32_t> sub_off;   // offsets of each substream payload
    std::vector<uint32_t> sub_len;
    const uint8_t* base;             // first payload byte
    YbTab0 t0;
    YbTab1 t1;
    uint16_t symD;
    uint32_t M = YB_RANS_M;
    uint32_t logM = YB_RANS_SCALE;
};

static inline void yb_stream_read_header(YbRd& rd, YbStreamIn& st) {
    st.nsyms = rd.u32();
    st.ilv = rd.u8();
    uint32_t order = rd.u8();
    st.logM = rd.u8();
    rd.u8();  // reserved
    if (st.ilv > 8 || st.ilv == 0 || (st.ilv & (st.ilv - 1)) != 0) YB_FAIL("bad ilv");
    if (order > 1) YB_FAIL("bad order");
    if (st.logM < 8 || st.logM > 12) YB_FAIL("bad logM");
    st.M = 1u << st.logM;
    st.order1 = order == 1;
    if (st.order1) {
        st.symD = rd.u16();
        uint32_t nctx = rd.u16();
        std::vector<std::vector<uint16_t>> syms(nctx), freqs(nctx);
        for (uint32_t c = 0; c < nctx; c++) {
            uint32_t dc = rd.u16();
            syms[c].resize(dc);
            freqs[c].resize(dc);
            for (uint32_t i = 0; i < dc; i++) {
                syms[c][i] = rd.u16();
                freqs[c][i] = rd.u16();
            }
        }
        st.t1.build(nctx, st.M, syms, freqs);
    } else {
        uint32_t dc = rd.u16();
        std::vector<uint16_t> syms(dc), freqs(dc);
        for (uint32_t i = 0; i < dc; i++) {
            syms[i] = rd.u16();
            freqs[i] = rd.u16();
        }
        st.t0.build(syms, freqs);
    }
    st.sub_len.resize(st.ilv);
    st.sub_off.resize(st.ilv);
    size_t off = 0;
    for (uint32_t k = 0; k < st.ilv; k++) {
        uint32_t n = rd.u32();
        st.sub_off[k] = (uint32_t)off;
        st.sub_len[k] = n;
        off += n;
    }
    st.base = rd.raw(off);
}

// interleaved symbol decode; out must hold nsyms entries
static inline void yb_stream_decode_syms(const YbStreamIn& st, uint32_t* out) {
    uint32_t n = st.nsyms, ilv = st.ilv;
    if (n == 0) return;
    YbRansDec r[8];
    for (uint32_t k = 0; k < ilv; k++) {
        uint32_t cnt_k = (n - k + ilv - 1) / ilv;
        if (cnt_k == 0) continue;
        r[k].init(st.base + st.sub_off[k], st.base + st.sub_off[k] + st.sub_len[k]);
    }
    if (!st.order1) {
        const YbSlot* tab = st.t0.slots.data();
        uint32_t j = 0;
        for (; j + ilv <= n; j += ilv)
            for (uint32_t k = 0; k < ilv; k++) out[j + k] = yb_rans_dec_sym_m(&r[k], tab, st.logM);
        for (; j < n; j++) out[j] = yb_rans_dec_sym_m(&r[j % ilv], tab, st.logM);
    } else {
        YB_FAIL("order-1 symbol stream not supported here");
    }
}

// sequential order-1 byte decoder used while assembling output
struct YbTextDec {
    YbRansDec r[8];
    uint32_t ilv;
    const YbSlot* tabs;
    uint32_t j;       // absolute symbol index (for interleave)
    uint32_t prev;    // previous char, init = nctx-1
    void start(const YbStreamIn& st) {
        ilv = st.ilv;
        M = st.M;
        logM = st.logM;
        tabs = st.t1.slots.data();
        for (uint32_t k = 0; k < ilv; k++) {
            uint32_t cnt_k = (st.nsyms - k + ilv - 1) / ilv;
            if (cnt_k == 0) continue;
            r[k].init(st.base + st.sub_off[k], st.base + st.sub_off[k] + st.sub_len[k]);
        }
        j = 0;
        prev = st.t1.nctx - 1;
    }
    inline uint8_t next() {
        uint32_t k = j & (ilv - 1);
        const YbSlot* tab = tabs + (size_t)prev * M;
        uint32_t s = yb_rans_dec_sym_m(&r[k], tab, logM);
        prev = s;
        j++;
        return (uint8_t)s;
    }
    uint32_t M = YB_RANS_M, logM = YB_RANS_SCALE;
};

// decode an entire order-1 byte stream into out (declen bytes)
static inline void yb_stream_decode_text(const YbStreamIn& st, uint8_t* out, uint32_t declen) {
    if (st.nsyms != declen) YB_FAIL("text stream length mismatch");
    if (declen == 0) return;
    YbTextDec td;
    td.start(st);
    for (uint32_t i = 0; i < declen; i++) out[i] = td.next();
}

// ---------------- names.bin container (HBI1 <-> HBA1) ----------------
struct YbNameFile {
    std::vector<uint8_t> bytes;  // whole file with magic swapped as requested
};

// Parse the packed container in `in`; on success write the same records with
// `magic_out` into out. Returns false on malformed input.
static inline bool yb_container_transcode(const uint8_t* in, size_t in_len, const uint8_t magic_out[4],
                                          std::vector<uint8_t>& out) {
    if (in_len < 9) return false;
    if (std::memcmp(in, HBI_MAGIC, 4) != 0 && std::memcmp(in, HBA_MAGIC, 4) != 0) return false;
    if (in[4] != 1) return false;
    uint32_t count = yb_get32be(in + 5);
    if (count == 0 || count > 1000000u) return false;
    size_t pos = 9;
    std::vector<std::pair<uint32_t, const uint8_t*>> aliases;
    aliases.reserve(count);
    std::vector<uint64_t> paylens;
    for (uint32_t i = 0; i < count; i++) {
        if (in_len - pos < 4) return false;
        uint32_t alen = (uint32_t(in[pos]) << 24) | (uint32_t(in[pos+1]) << 16) |
                        (uint32_t(in[pos+2]) << 8) | in[pos+3];
        pos += 4;
        if (alen < 1 || alen > 1048576 || in_len - pos < alen) return false;
        if (!yb_alias_ok(in + pos, alen)) return false;
        aliases.push_back({alen, in + pos});
        pos += alen;
        if (in_len - pos < 8) return false;
        uint64_t plen = yb_get64be(in + pos);
        pos += 8;
        if (in_len - pos < plen) return false;
        paylens.push_back(plen);
        pos += plen;
    }
    if (pos != in_len) return false;
    out.clear();
    out.reserve(in_len);
    out.insert(out.end(), magic_out, magic_out + 4);
    out.push_back(1);
    out.push_back((uint8_t)(count >> 24));
    out.push_back((uint8_t)(count >> 16));
    out.push_back((uint8_t)(count >> 8));
    out.push_back((uint8_t)count);
    for (uint32_t i = 0; i < count; i++) {
        uint32_t alen = aliases[i].first;
        out.push_back((uint8_t)(alen >> 24));
        out.push_back((uint8_t)(alen >> 16));
        out.push_back((uint8_t)(alen >> 8));
        out.push_back((uint8_t)alen);
        out.insert(out.end(), aliases[i].second, aliases[i].second + alen);
        uint64_t plen = paylens[i];
        uint8_t b[8];
        yb_put64be(b, plen);
        out.insert(out.end(), b, b + 8);
    }
    return true;
}
