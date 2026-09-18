// ybz encoder: strict template parse of the yelp business JSONL schema into
// columnar streams + custom interleaved rANS. Any parse/limit failure falls
// back to a raw archive (mode 1) so the codec stays total on arbitrary bytes.
#include "ybz_common.h"
#include "ybz_enc.h"
#include <algorithm>
#include <fcntl.h>
#include <memory>
#include <string>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

struct Span { const uint8_t* s; uint32_t len; };

static const uint32_t NKEYS_MAX = 200;
static const char* DAY_NAMES[7] = {"Monday", "Tuesday", "Wednesday", "Thursday",
                                   "Friday", "Saturday", "Sunday"};
static const uint8_t DAY_LEN[7] = {6, 7, 9, 8, 6, 8, 6};

// ---------------- interning ----------------
struct Interner {
    struct Ent { uint32_t hash; uint32_t id; };
    std::vector<Ent> tab;
    size_t mask = 0;
    std::vector<Span> items;
    bool copy = false;                    // store item bytes in a stable arena
    std::vector<std::vector<uint8_t>> chunks;  // sealed 64KB chunks; spans stay valid
    const uint8_t* copy_in(const uint8_t* s, uint32_t len) {
        if (chunks.empty() || chunks.back().size() + len > 65536) {
            chunks.emplace_back();
            chunks.back().reserve(65536);
        }
        auto& ch = chunks.back();
        const uint8_t* p = ch.data() + ch.size();
        ch.insert(ch.end(), s, s + len);
        return p;
    }

    void init() {
        tab.assign(1024, {0, UINT32_MAX});
        mask = tab.size() - 1;
    }
    static uint32_t hashb(const uint8_t* s, size_t n) {
        uint64_t h = 1469598103934665603ull;
        for (size_t i = 0; i < n; i++) { h ^= s[i]; h *= 1099511628211ull; }
        return (uint32_t)(h >> 32);
    }
    uint32_t intern(const uint8_t* s, uint32_t len, bool* fresh) {
        uint32_t h = hashb(s, len);
        size_t i = h & mask;
        for (;;) {
            if (tab[i].id == UINT32_MAX) {
                uint32_t id = (uint32_t)items.size();
                if (copy) s = copy_in(s, len);
                items.push_back({s, len});
                tab[i] = {h, id};
                if (items.size() * 10 >= (mask + 1) * 7) grow();
                *fresh = true;
                return id;
            }
            Span& sp = items[tab[i].id];
            if (sp.len == len && tab[i].hash == h && !std::memcmp(sp.s, s, len)) {
                *fresh = false;
                return tab[i].id;
            }
            i = (i + 1) & mask;
        }
    }
    void grow() {
        std::vector<Ent> nt(tab.size() * 2, {0, UINT32_MAX});
        size_t nmask = nt.size() - 1;
        for (size_t t = 0; t < tab.size(); t++) {
            if (tab[t].id == UINT32_MAX) continue;
            size_t i = tab[t].hash & nmask;
            while (nt[i].id != UINT32_MAX) i = (i + 1) & nmask;
            nt[i] = tab[t];
        }
        tab.swap(nt);
        mask = nmask;
    }
};

// value interner that also records the JSON quoting tag per entry
struct VInterner {
    Interner in;
    std::vector<uint8_t> tags;
    uint32_t intern(const uint8_t* s, uint32_t len, uint8_t tag, bool* fresh) {
        uint32_t id = in.intern(s, len, fresh);
        if (*fresh) tags.push_back(tag);
        return id;
    }
};

// ---------------- strict scan helpers ----------------
struct Cur { const uint8_t* p; const uint8_t* end; };

static bool expect(Cur& c, const char* lit, size_t n) {
    if ((size_t)(c.end - c.p) < n || std::memcmp(c.p, lit, n) != 0) return false;
    c.p += n;
    return true;
}

static const uint8_t* find_str_end(const uint8_t* p, const uint8_t* end) {
    const uint8_t* q = p;
    for (;;) {
        if (q >= end) return nullptr;
        const uint8_t* r = (const uint8_t*)std::memchr(q, '"', (size_t)(end - q));
        if (!r) return nullptr;
        size_t bs = 0;
        const uint8_t* b = r;
        while (b > p && b[-1] == '\\') { bs++; b--; }
        if ((bs & 1) == 0) return r;
        q = r + 1;
    }
}

static const uint8_t* find_bare_end(const uint8_t* p, const uint8_t* end) {
    const uint8_t* q = p;
    while (q < end && *q != ',' && *q != '}') q++;
    return q;
}

static bool parse_coord(const uint8_t* s, uint32_t len, Span& ip, Span& frac) {
    uint32_t i = 0;
    if (len && s[0] == '-') i = 1;
    uint32_t digs = 0;
    while (i < len && s[i] >= '0' && s[i] <= '9') { i++; digs++; }
    if (digs == 0 || digs > 3 || i >= len || s[i] != '.') return false;
    ip = {s, i};
    uint32_t fs = i + 1;
    uint32_t fdigs = 0;
    while (fs + fdigs < len && s[fs + fdigs] >= '0' && s[fs + fdigs] <= '9') fdigs++;
    if (fdigs == 0 || fdigs > YB_NPOS || fs + fdigs != len) return false;
    frac = {s + fs, fdigs};
    return true;
}

static bool all_digits(const uint8_t* s, uint32_t len) {
    if (!len) return false;
    for (uint32_t i = 0; i < len; i++)
        if (s[i] < '0' || s[i] > '9') return false;
    return true;
}

struct Fail {};

// ---------------- parsed corpus ----------------
struct Parsed {
    uint64_t nrec = 0;
    bool trailing = false;
    std::vector<uint8_t> ids;
    std::vector<uint32_t> name_lens, addr_lens;
    std::vector<uint8_t> name_text, addr_text;
    std::vector<uint32_t> city, zip, state;
    Interner city_i, zip_i, state_i;
    std::vector<uint32_t> lat_ip, lat_fl, lon_ip, lon_fl;
    std::vector<uint32_t> lat_d[YB_NPOS], lon_d[YB_NPOS];
    Interner latip_i, lonip_i;
    std::vector<uint32_t> stars, rc, io;
    Interner stars_i, rc_i;
    std::vector<uint32_t> ks;                 // per record: keyset appearance id
    std::vector<uint8_t> ks_null;             // per record: attributes==null flag
    std::vector<std::vector<uint32_t>> val;   // per key: value ids, record order
    std::vector<std::unique_ptr<VInterner>> val_i;
    Interner key_i;
    Interner ks_i;
    std::vector<uint8_t> ks_arena;            // keyset sequences (key ids)
    std::vector<std::pair<uint32_t, uint32_t>> ks_seq;  // appearance order: (arena off, len)
    std::vector<uint32_t> cat_null, cat_cnt, cat_tok;
    Interner cat_i;
    std::vector<uint32_t> hr_null, hr_pat;
    Interner hr_i, pat_i;
    std::vector<uint32_t> hr_same, hr_val[7];

    void init() {
        city_i.init(); zip_i.init(); state_i.init(); latip_i.init(); lonip_i.init();
        stars_i.init(); rc_i.init(); key_i.init(); ks_i.init(); cat_i.init();
        hr_i.init(); pat_i.init();
        pat_i.copy = true;
        ks_i.copy = true;
        val.resize(NKEYS_MAX);
    }
};

// ---------------- parser ----------------
struct Parser {
    Parsed P;
    int8_t IDLUT[256];

    Parser() {
        P.init();
        for (int i = 0; i < 256; i++) IDLUT[i] = -1;
        for (int i = 0; i < 64; i++) IDLUT[(uint8_t)YB_ID_ALPHABET[i]] = (int8_t)i;
    }

    void run(const uint8_t* data, size_t len) {
        P.trailing = len > 0 && data[len - 1] == '\n';
        Cur c{data, data + len};
        while (c.p < c.end) {
            parse_record(c);
            if (c.p < c.end) {
                if (!expect(c, "\n", 1)) throw Fail{};
            }
        }
    }

    void str_into(std::vector<uint8_t>& text, std::vector<uint32_t>& lens, Cur& c) {
        const uint8_t* q = find_str_end(c.p, c.end);
        if (!q) throw Fail{};
        uint32_t n = (uint32_t)(q - c.p);
        if (n > 255) throw Fail{};
        text.insert(text.end(), c.p, q);
        lens.push_back((uint8_t)n);
        c.p = q + 1;
    }

    uint32_t str_int(Interner& ii, uint32_t cap, Cur& c) {
        const uint8_t* q = find_str_end(c.p, c.end);
        if (!q) throw Fail{};
        bool f;
        uint32_t id = ii.intern(c.p, (uint32_t)(q - c.p), &f);
        if (ii.items.size() > cap) throw Fail{};
        c.p = q + 1;
        return id;
    }

    void coord(Cur& c, Interner& ipi, std::vector<uint32_t>& ipa, std::vector<uint32_t>& fla,
               std::vector<uint32_t>* darr) {
        const uint8_t* q = find_bare_end(c.p, c.end);
        uint32_t len = (uint32_t)(q - c.p);
        Span ip, frac;
        if (!parse_coord(c.p, len, ip, frac)) throw Fail{};
        bool f;
        uint32_t ipid = ipi.intern(ip.s, ip.len, &f);
        if (ipi.items.size() > 255) throw Fail{};
        ipa.push_back(ipid);
        fla.push_back(frac.len);
        for (uint32_t i = 0; i < frac.len; i++) darr[i].push_back(frac.s[i] - '0');
        c.p = q;
    }

    void bare_int(Interner& ii, uint32_t cap, Cur& c, std::vector<uint32_t>& arr) {
        const uint8_t* q = find_bare_end(c.p, c.end);
        uint32_t len = (uint32_t)(q - c.p);
        if (len > 255) throw Fail{};
        bool f;
        uint32_t id = ii.intern(c.p, len, &f);
        if (ii.items.size() > cap) throw Fail{};
        arr.push_back(id);
        c.p = q;
    }

    void parse_record(Cur& c) {
        if (!expect(c, YB_T1, sizeof(YB_T1) - 1)) throw Fail{};
        {
            const uint8_t* q = find_str_end(c.p, c.end);
            if (!q || q - c.p != YB_ID_LEN) throw Fail{};
            size_t base = (size_t)P.nrec * YB_ID_PACKED;
            P.ids.resize(base + YB_ID_PACKED, 0);
            for (int i = 0; i < YB_ID_LEN; i++) {
                int8_t v = IDLUT[c.p[i]];
                if (v < 0) throw Fail{};
                uint32_t bit = 6 * i;
                uint16_t sh = (uint16_t)(v << (bit & 7));
                P.ids[base + (bit >> 3)] |= (uint8_t)(sh & 0xFF);
                P.ids[base + (bit >> 3) + 1] |= (uint8_t)(sh >> 8);
            }
            c.p = q + 1;
        }
        if (!expect(c, YB_T2, sizeof(YB_T2) - 1)) throw Fail{};
        str_into(P.name_text, P.name_lens, c);
        if (!expect(c, YB_T3, sizeof(YB_T3) - 1)) throw Fail{};
        str_into(P.addr_text, P.addr_lens, c);
        if (!expect(c, YB_T4, sizeof(YB_T4) - 1)) throw Fail{};
        P.city.push_back(str_int(P.city_i, 65535, c));
        if (!expect(c, YB_T5, sizeof(YB_T5) - 1)) throw Fail{};
        P.state.push_back(str_int(P.state_i, 255, c));
        if (!expect(c, YB_T6, sizeof(YB_T6) - 1)) throw Fail{};
        P.zip.push_back(str_int(P.zip_i, 65535, c));
        if (!expect(c, YB_T7, sizeof(YB_T7) - 1)) throw Fail{};
        coord(c, P.latip_i, P.lat_ip, P.lat_fl, P.lat_d);
        if (!expect(c, YB_T8, sizeof(YB_T8) - 1)) throw Fail{};
        coord(c, P.lonip_i, P.lon_ip, P.lon_fl, P.lon_d);
        if (!expect(c, YB_T9, sizeof(YB_T9) - 1)) throw Fail{};
        bare_int(P.stars_i, 255, c, P.stars);
        if (!expect(c, YB_T10, sizeof(YB_T10) - 1)) throw Fail{};
        {
            const uint8_t* q = find_bare_end(c.p, c.end);
            uint32_t len = (uint32_t)(q - c.p);
            if (!all_digits(c.p, len)) throw Fail{};
            bool f;
            uint32_t id = P.rc_i.intern(c.p, len, &f);
            if (P.rc_i.items.size() > 4096) throw Fail{};
            P.rc.push_back(id);
            c.p = q;
        }
        if (!expect(c, YB_T11, sizeof(YB_T11) - 1)) throw Fail{};
        {
            const uint8_t* q = find_bare_end(c.p, c.end);
            if (q - c.p != 1 || (*c.p != '0' && *c.p != '1')) throw Fail{};
            P.io.push_back((uint32_t)(*c.p - '0'));
            c.p = q;
        }
        if (!expect(c, YB_T12, sizeof(YB_T12) - 1)) throw Fail{};
        attrs(c);
        if (!expect(c, YB_T13, sizeof(YB_T13) - 1)) throw Fail{};
        categories(c);
        if (!expect(c, YB_T14, sizeof(YB_T14) - 1)) throw Fail{};
        hours(c);
        if (!expect(c, YB_T15, sizeof(YB_T15) - 1)) throw Fail{};
        P.nrec++;
    }

    void attrs(Cur& c) {
        if ((size_t)(c.end - c.p) >= 4 && !std::memcmp(c.p, "null", 4)) {
            c.p += 4;
            P.ks.push_back(0);
            P.ks_null.push_back(1);
            return;
        }
        if (!expect(c, "{", 1)) throw Fail{};
        uint8_t seqbuf[NKEYS_MAX + 1];
        uint32_t seqn = 0;
        while (c.p < c.end && *c.p != '}') {
            if (!expect(c, "\"", 1)) throw Fail{};
            const uint8_t* q = find_str_end(c.p, c.end);
            if (!q) throw Fail{};
            uint32_t klen = (uint32_t)(q - c.p);
            bool f;
            uint32_t kid = P.key_i.intern(c.p, klen, &f);
            if (P.key_i.items.size() > NKEYS_MAX) throw Fail{};
            if (f) {
                P.val_i.push_back(std::unique_ptr<VInterner>(new VInterner()));
                P.val_i.back()->in.init();
            }
            c.p = q + 1;
            if (!expect(c, ":", 1)) throw Fail{};
            uint8_t tag;
            const uint8_t* vs;
            uint32_t vl;
            if (c.p < c.end && *c.p == '"') {
                c.p++;
                const uint8_t* e = find_str_end(c.p, c.end);
                if (!e) throw Fail{};
                tag = 0;
                vs = c.p;
                vl = (uint32_t)(e - c.p);
                c.p = e + 1;
            } else {
                const uint8_t* e = find_bare_end(c.p, c.end);
                tag = 1;
                vs = c.p;
                vl = (uint32_t)(e - c.p);
                c.p = e;
            }
            if (vl == 0 || vl > 65535) throw Fail{};
            uint32_t vid = P.val_i[kid]->intern(vs, vl, tag, &f);
            if (P.val_i[kid]->in.items.size() > 4096) throw Fail{};
            P.val[kid].push_back(vid);
            if (seqn >= NKEYS_MAX) throw Fail{};
            seqbuf[seqn++] = (uint8_t)kid;
            if (c.p < c.end && *c.p == ',') { c.p++; continue; }
            break;
        }
        if (!expect(c, "}", 1)) throw Fail{};
        bool f;
        uint32_t ksid = P.ks_i.intern(seqbuf, seqn, &f);
        if (P.ks_i.items.size() > 65535) throw Fail{};
        P.ks.push_back(ksid);
        P.ks_null.push_back(0);
    }

    void categories(Cur& c) {
        if ((size_t)(c.end - c.p) >= 4 && !std::memcmp(c.p, "null", 4)) {
            c.p += 4;
            P.cat_null.push_back(0);
            P.cat_cnt.push_back(0);
            return;
        }
        if (!expect(c, "\"", 1)) throw Fail{};
        const uint8_t* q = find_str_end(c.p, c.end);
        if (!q) throw Fail{};
        P.cat_null.push_back(1);
        const uint8_t* t = c.p;
        const uint8_t* e = q;
        uint32_t cnt = 0;
        for (;;) {
            const uint8_t* f = t;
            for (;;) {
                if (f >= e) { f = nullptr; break; }
                const uint8_t* m = (const uint8_t*)std::memchr(f, ',', (size_t)(e - f));
                if (!m || m + 1 >= e) { f = nullptr; break; }
                if (m[1] == ' ') { f = m; break; }
                f = m + 1;
            }
            uint32_t tlen = f ? (uint32_t)(f - t) : (uint32_t)(e - t);
            if (tlen > 255) throw Fail{};
            bool fr;
            uint32_t id = P.cat_i.intern(t, tlen, &fr);
            if (P.cat_i.items.size() > 4096) throw Fail{};
            P.cat_tok.push_back(id);
            cnt++;
            if (!f) break;
            t = f + 2;
        }
        P.cat_cnt.push_back(cnt);
        c.p = e + 1;
    }

    void hours(Cur& c) {
        if ((size_t)(c.end - c.p) >= 4 && !std::memcmp(c.p, "null", 4)) {
            c.p += 4;
            P.hr_null.push_back(0);
            return;
        }
        if (!expect(c, "{", 1)) throw Fail{};
        uint8_t patbuf[8];
        uint32_t patn = 0;
        uint32_t prev_val = UINT32_MAX;
        while (c.p < c.end && *c.p != '}') {
            if (!expect(c, "\"", 1)) throw Fail{};
            const uint8_t* q = find_str_end(c.p, c.end);
            if (!q) throw Fail{};
            int8_t day = -1;
            for (uint8_t d = 0; d < 7; d++)
                if (DAY_LEN[d] == (uint32_t)(q - c.p) && !std::memcmp(c.p, DAY_NAMES[d], q - c.p))
                    day = (int8_t)d;
            if (day < 0) throw Fail{};
            c.p = q + 1;
            if (!expect(c, ":", 1) || c.p >= c.end || *c.p != '"') throw Fail{};
            c.p++;
            const uint8_t* e = find_str_end(c.p, c.end);
            if (!e) throw Fail{};
            uint32_t vlen = (uint32_t)(e - c.p);
            if (vlen == 0 || vlen > 255) throw Fail{};
            bool f;
            uint32_t vid = P.hr_i.intern(c.p, vlen, &f);
            if (P.hr_i.items.size() > 4096) throw Fail{};
            c.p = e + 1;
            bool same = patn > 0 && vid == prev_val;
            if (patn > 0) P.hr_same.push_back(same ? 0 : 1);
            if (!same) P.hr_val[patn].push_back(vid);
            patbuf[patn++] = (uint8_t)day;
            prev_val = vid;
            if (patn > 7) throw Fail{};
            if (c.p < c.end && *c.p == ',') { c.p++; continue; }
            break;
        }
        if (!expect(c, "}", 1)) throw Fail{};
        bool f;
        uint32_t pid = P.pat_i.intern(patbuf, patn, &f);
        if (P.pat_i.items.size() > 255) throw Fail{};
        P.hr_pat.push_back(pid);
        P.hr_null.push_back(1);
    }
};

// ---------------- stream emission ----------------
static bool emit_stream0(std::vector<uint8_t>& out, const std::vector<uint32_t>& syms,
                         uint32_t symD) {
    if (syms.size() == 0) {
        uint8_t b[4];
        yb_put32(b, 0);
        out.insert(out.end(), b, b + 4);
        out.push_back(1);
        out.push_back(0);
        out.push_back(YB_RANS_SCALE);
        out.push_back(0);
        yb_put16(b, 0);
        out.insert(out.end(), b, b + 2);
        yb_put32(b, 0);
        out.insert(out.end(), b, b + 4);
        return true;
    }
    std::vector<uint32_t> counts(symD, 0);
    for (uint32_t s : syms) counts[s]++;
    YbEncModel m;
    if (!m.build(false, 1, symD, counts)) return false;
    YbStreamOut::write0(out, syms.data(), (uint32_t)syms.size(), m);
    return true;
}

static bool emit_stream1(std::vector<uint8_t>& out, const std::vector<uint8_t>& text,
                         uint32_t nctx, uint32_t symD) {
    if (text.size() == 0) {
        uint8_t b[4];
        yb_put32(b, 0);
        out.insert(out.end(), b, b + 4);
        out.push_back(1);
        out.push_back(1);
        out.push_back(YB_RANS_SCALE1);
        out.push_back(0);
        yb_put16(b, (uint16_t)symD);
        out.insert(out.end(), b, b + 2);
        yb_put16(b, (uint16_t)nctx);
        out.insert(out.end(), b, b + 2);
        for (uint32_t c = 0; c < nctx; c++) { yb_put16(b, 0); out.insert(out.end(), b, b + 2); }
        yb_put32(b, 0);
        out.insert(out.end(), b, b + 4);
        return true;
    }
    std::vector<uint32_t> counts((size_t)nctx * symD, 0u);
    uint32_t prev = nctx - 1;
    for (uint8_t ch : text) {
        counts[(size_t)prev * symD + ch]++;
        prev = ch;
    }
    YbEncModel m;
    m.set_scale(YB_RANS_SCALE1);
    if (!m.build(true, nctx, symD, counts)) return false;
    YbStreamOut::write1(out, text.data(), (uint32_t)text.size(), m);
    return true;
}


// ---------------- archive build ----------------
static void put_u16(std::vector<uint8_t>& o, uint16_t v) { uint8_t b[2]; yb_put16(b, v); o.insert(o.end(), b, b + 2); }
static void put_u32(std::vector<uint8_t>& o, uint32_t v) { uint8_t b[4]; yb_put32(b, v); o.insert(o.end(), b, b + 4); }
static void put_str8(std::vector<uint8_t>& o, const Span& s) {
    o.push_back((uint8_t)s.len);
    o.insert(o.end(), s.s, s.s + s.len);
}

static bool build_template(const uint8_t* data, size_t len, std::vector<uint8_t>& out) {
    Parser ps;
    try {
        ps.run(data, len);
    } catch (Fail&) {
            return false;
    }
    Parsed& P = ps.P;
    if (P.nrec == 0 || P.nrec > 100000000ull) return false;

    const uint64_t nrec = P.nrec;
    const uint32_t nkeys = (uint32_t)P.key_i.items.size();
    const uint32_t ncity = (uint32_t)P.city_i.items.size();
    const uint32_t nzip = (uint32_t)P.zip_i.items.size();
    const uint32_t nstate = (uint32_t)P.state_i.items.size();
    const uint32_t ncat = (uint32_t)P.cat_i.items.size();
    const uint32_t nhour = (uint32_t)P.hr_i.items.size();
    const uint32_t npat = (uint32_t)P.pat_i.items.size();
    const uint32_t nstars = (uint32_t)P.stars_i.items.size();
    const uint32_t nrc = (uint32_t)P.rc_i.items.size();
    const uint32_t nlatip = (uint32_t)P.latip_i.items.size();
    const uint32_t nlonip = (uint32_t)P.lonip_i.items.size();
    const uint32_t nrows = (uint32_t)P.ks_i.items.size();

    // per-city zip lists (first-appearance order) and symbols
    std::vector<std::vector<uint32_t>> city_zip_list(ncity);
    std::vector<uint32_t> zip_sym(nrec);
    {
        // per-city small linear scan (lists are short)
        for (uint64_t r = 0; r < nrec; r++) {
            uint32_t c = P.city[r], z = P.zip[r];
            std::vector<uint32_t>& L = city_zip_list[c];
            uint32_t idx = (uint32_t)L.size();
            for (uint32_t j = 0; j < L.size(); j++)
                if (L[j] == z) { idx = j; break; }
            if (idx == L.size()) {
                if (L.size() > 65535) { return false; }
                L.push_back(z);
            }
            zip_sym[r] = idx;
        }
    }

    // per-city state + exception stream
    std::vector<uint32_t> state_ex;
    std::vector<int32_t> city_state(ncity, -1);
    std::vector<uint8_t> ambiguous(ncity, 0);
    for (uint64_t r = 0; r < nrec; r++) {
        uint32_t c = P.city[r], st = P.state[r];
        if (city_state[c] < 0) city_state[c] = (int32_t)st;
        else if ((uint32_t)city_state[c] != st) ambiguous[c] = 1;
    }
    for (uint64_t r = 0; r < nrec; r++) {
        uint32_t c = P.city[r];
        if (ambiguous[c]) state_ex.push_back(P.state[r]);
    }

    // keyset rows: sort appearance sequences lexicographically
    std::vector<uint32_t> row_of_ks(nrows);
    std::vector<uint32_t> order_(nrows);
    {
        std::vector<uint32_t>& order = order_;
        for (uint32_t i = 0; i < nrows; i++) order[i] = i;
        std::sort(order.begin(), order.end(), [&](uint32_t a, uint32_t b) {
            Span sa = P.ks_i.items[a];
            Span sb = P.ks_i.items[b];
            int m = std::memcmp(sa.s, sb.s, std::min(sa.len, sb.len));
            if (m != 0) return m < 0;
            return sa.len < sb.len;
        });
        for (uint32_t i = 0; i < nrows; i++) row_of_ks[order[i]] = i;
    }
    // counts per row (via appearance ids)
    std::vector<uint32_t> row_cnt(nrows, 0);
    for (uint64_t r = 0; r < nrec; r++)
        if (!P.ks_null[r]) row_cnt[row_of_ks[P.ks[r]]]++;
    // top-1024 rows by count (desc), ties by row asc
    std::vector<uint32_t> top(YB_TOPKS, 0);
    std::vector<int32_t> rank_of_row(nrows, -1);
    {
        std::vector<uint32_t> bycnt(nrows);
        for (uint32_t i = 0; i < nrows; i++) bycnt[i] = i;
        std::sort(bycnt.begin(), bycnt.end(), [&](uint32_t a, uint32_t b) {
            if (row_cnt[a] != row_cnt[b]) return row_cnt[a] > row_cnt[b];
            return a < b;
        });
        uint32_t t = 0;
        for (uint32_t i = 0; i < nrows && t < YB_TOPKS; i++) {
            if (row_cnt[bycnt[i]] == 0) break;
            top[t] = bycnt[i];
            rank_of_row[bycnt[i]] = (int32_t)t;
            t++;
        }
        for (uint32_t i = t; i < YB_TOPKS; i++) top[i] = 0;  // unused slots (never decoded:
        // ks symbols < YB_TOPKS always map to ranks < t because such symbols
        // only arise when row_cnt > 0)
    }
    // ks stream symbols + escapes
    std::vector<uint32_t> ks_sym(nrec), escapes;
    for (uint64_t r = 0; r < nrec; r++) {
        uint32_t ksid = P.ks[r];
        if (P.ks_null[r]) { ks_sym[r] = YB_KS_NULL; continue; }
        uint32_t row = row_of_ks[ksid];
        int32_t rk = rank_of_row[row];
        if (rk >= 0) ks_sym[r] = (uint32_t)rk;
        else {
            ks_sym[r] = YB_KS_ESC;
            escapes.push_back(row);
        }
    }
    // keyset row prefix/suffix coding
    std::vector<uint32_t> ks_pfx(nrows), ks_sfx_len(nrows);
    std::vector<uint8_t> ks_sfx;
    {
        // order_[i] = appearance id of the i-th row in sorted order
        Span prev{nullptr, 0};
        for (uint32_t i = 0; i < nrows; i++) {
            Span cur = P.ks_i.items[order_[i]];
            uint32_t pfx = 0;
            while (pfx < prev.len && pfx < cur.len && prev.s[pfx] == cur.s[pfx]) pfx++;
            ks_pfx[i] = pfx;
            ks_sfx_len[i] = cur.len - pfx;
            ks_sfx.insert(ks_sfx.end(), cur.s + pfx, cur.s + cur.len);
            prev = cur;
        }
    }

    // ---- serialize ----
    out.clear();
    uint8_t h[40];
    std::memcpy(h, YBZ_MAGIC, 4);
    h[4] = 0;                 // mode template
    h[5] = P.trailing ? 1 : 0;
    h[6] = h[7] = 0;
    yb_put64(h + 8, (uint64_t)len);
    yb_put64(h + 16, nrec);
    yb_put32(h + 24, nkeys);
    yb_put32(h + 28, 0);
    yb_put64(h + 32, 0);
    out.insert(out.end(), h, h + 40);
    out.insert(out.end(), P.ids.begin(), P.ids.end());

    auto ok0 = [&](const std::vector<uint32_t>& s, uint32_t D) {
        return emit_stream0(out, s, D);
    };

    if (!ok0(P.name_lens, 256)) return false;
    if (!ok0(P.addr_lens, 256)) return false;
    if (!emit_stream1(out, P.name_text, 257, 256)) return false;
    if (!emit_stream1(out, P.addr_text, 257, 256)) return false;
    if (!ok0(P.city, ncity)) return false;
    if (!ok0(zip_sym, nzip)) return false;
    if (!ok0(P.lat_ip, nlatip)) return false;
    if (!ok0(P.lat_fl, YB_NPOS + 1)) return false;
    for (int p = 0; p < YB_NPOS; p++)
        if (!ok0(P.lat_d[p], 10)) return false;
    if (!ok0(P.lon_ip, nlonip)) return false;
    if (!ok0(P.lon_fl, YB_NPOS + 1)) return false;
    for (int p = 0; p < YB_NPOS; p++)
        if (!ok0(P.lon_d[p], 10)) return false;
    if (!ok0(P.stars, nstars)) return false;
    if (!ok0(P.rc, nrc)) return false;
    if (!ok0(P.io, 2)) return false;
    if (!ok0(ks_sym, YB_KS_NULL + 1)) return false;
    put_u32(out, (uint32_t)escapes.size());
    for (uint32_t e : escapes) put_u16(out, (uint16_t)e);
    if (!ok0(ks_pfx, 256)) return false;
    if (!ok0(ks_sfx_len, 256)) return false;
    if (!emit_stream1(out, ks_sfx, nkeys + 1, nkeys + 1)) return false;
    for (uint32_t k = 0; k < nkeys; k++)
        if (!ok0(P.val[k], P.val_i[k]->in.items.size())) { return false; }
    if (!ok0(P.cat_null, 2)) return false;
    if (!ok0(P.cat_cnt, 256)) return false;
    if (!ok0(P.cat_tok, ncat)) return false;
    if (!ok0(P.hr_null, 2)) return false;
    if (!ok0(P.hr_pat, npat)) return false;
    if (!ok0(P.hr_same, 2)) return false;
    for (int p = 0; p < 7; p++)
        if (!ok0(P.hr_val[p], nhour)) { return false; }
    if (!ok0(state_ex, nstate)) return false;

    // dict blob
    std::vector<uint8_t> blob;
    put_u16(blob, (uint16_t)ncity);
    for (uint32_t i = 0; i < ncity; i++) put_str8(blob, P.city_i.items[i]);
    blob.push_back((uint8_t)nstate);
    for (uint32_t i = 0; i < nstate; i++) put_str8(blob, P.state_i.items[i]);
    for (uint32_t i = 0; i < ncity; i++)
        blob.push_back(ambiguous[i] ? 255 : (uint8_t)city_state[i]);
    put_u16(blob, (uint16_t)nzip);
    for (uint32_t i = 0; i < nzip; i++) put_str8(blob, P.zip_i.items[i]);
    for (uint32_t i = 0; i < ncity; i++) {
        put_u16(blob, (uint16_t)city_zip_list[i].size());
        for (uint32_t z : city_zip_list[i]) put_u16(blob, (uint16_t)z);
    }
    blob.push_back((uint8_t)nkeys);
    for (uint32_t i = 0; i < nkeys; i++) put_str8(blob, P.key_i.items[i]);
    for (uint32_t k = 0; k < nkeys; k++) {
        const VInterner& vi = *P.val_i[k];
        put_u16(blob, (uint16_t)vi.in.items.size());
        for (uint32_t j = 0; j < vi.in.items.size(); j++) {
            blob.push_back(vi.tags[j]);
            put_u16(blob, vi.in.items[j].len);
            blob.insert(blob.end(), vi.in.items[j].s, vi.in.items[j].s + vi.in.items[j].len);
        }
    }
    put_u16(blob, (uint16_t)ncat);
    for (uint32_t i = 0; i < ncat; i++) put_str8(blob, P.cat_i.items[i]);
    put_u16(blob, (uint16_t)nhour);
    for (uint32_t i = 0; i < nhour; i++) put_str8(blob, P.hr_i.items[i]);
    put_u16(blob, (uint16_t)npat);
    for (uint32_t i = 0; i < npat; i++) put_str8(blob, P.pat_i.items[i]);
    blob.push_back((uint8_t)nstars);
    for (uint32_t i = 0; i < nstars; i++) put_str8(blob, P.stars_i.items[i]);
    put_u16(blob, (uint16_t)nrc);
    for (uint32_t i = 0; i < nrc; i++) put_str8(blob, P.rc_i.items[i]);
    blob.push_back((uint8_t)nlatip);
    for (uint32_t i = 0; i < nlatip; i++) put_str8(blob, P.latip_i.items[i]);
    blob.push_back((uint8_t)nlonip);
    for (uint32_t i = 0; i < nlonip; i++) put_str8(blob, P.lonip_i.items[i]);
    for (uint32_t i = 0; i < YB_TOPKS; i++) put_u32(blob, top[i]);

    put_u32(out, (uint32_t)blob.size());
    if (!emit_stream1(out, blob, 257, 256)) return false;
    return true;
}

static void build_raw(const uint8_t* data, size_t len, std::vector<uint8_t>& out) {
    uint8_t h[40];
    std::memcpy(h, YBZ_MAGIC, 4);
    h[4] = 1;
    h[5] = 0;
    h[6] = h[7] = 0;
    yb_put64(h + 8, (uint64_t)len);
    yb_put64(h + 16, 0);
    yb_put32(h + 24, 0);
    yb_put32(h + 28, 0);
    yb_put64(h + 32, 0);
    out.clear();
    out.insert(out.end(), h, h + 40);
    out.insert(out.end(), data, data + len);
}

static void build_archive(const uint8_t* data, size_t len, std::vector<uint8_t>& out) {
    if (len > 0 && !build_template(data, len, out)) build_raw(data, len, out);
    if (len == 0) build_raw(data, len, out);
    if (out.size() >= 40) {
        uint64_t hv = yb_hash64(out.data(), out.size());  // hash field is zero at this point
        yb_put64(out.data() + 32, hv);
    }
}

// ---------------- file IO ----------------
static uint8_t* map_file(const char* path, size_t* len_out) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) YB_FAIL("cannot open input");
    struct stat st;
    if (fstat(fd, &st) != 0) YB_FAIL("fstat failed");
    size_t n = (size_t)st.st_size;
    void* p = mmap(nullptr, n ? n : 1, PROT_READ, MAP_PRIVATE, fd, 0);
    if (p == MAP_FAILED) YB_FAIL("mmap failed");
    close(fd);
    *len_out = n;
    return (uint8_t*)p;
}

static void write_file(const char* path, const uint8_t* p, size_t n) {
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) YB_FAIL("cannot open output");
    size_t off = 0;
    while (off < n) {
        ssize_t w = write(fd, p + off, n - off);
        if (w <= 0) YB_FAIL("write error");
        off += (size_t)w;
    }
    close(fd);
}

static std::vector<uint8_t> read_fd(int fd) {
    std::vector<uint8_t> data;
    uint8_t buf[1 << 20];
    for (;;) {
        ssize_t n = read(fd, buf, sizeof buf);
        if (n < 0) YB_FAIL("read error");
        if (n == 0) break;
        data.insert(data.end(), buf, buf + n);
    }
    return data;
}

int main(int argc, char** argv) {
    if (argc < 2) YB_FAIL("usage: codec encode-dir IN OUT | encode-stream");
    if (!std::strcmp(argv[1], "encode-dir")) {
        if (argc != 4) YB_FAIL("usage: codec encode-dir IN OUT");
        size_t names_len;
        uint8_t* names = map_file((std::string(argv[2]) + "/names.bin").c_str(), &names_len);
        std::vector<uint8_t> names_out;
        if (!yb_container_transcode(names, names_len, HBA_MAGIC, names_out))
            YB_FAIL("bad names.bin");
        write_file((std::string(argv[3]) + "/names.bin").c_str(), names_out.data(), names_out.size());
        uint32_t count = yb_get32be(names + 5);
        munmap(names, names_len);
        for (uint32_t i = 0; i < count; i++) {
            char pbuf[512], obuf[512];
            std::snprintf(pbuf, sizeof pbuf, "%s/%08u.bin", argv[2], i);
            std::snprintf(obuf, sizeof obuf, "%s/%08u.bin", argv[3], i);
            size_t plen;
            uint8_t* payload = map_file(pbuf, &plen);
            std::vector<uint8_t> arc;
            build_archive(payload, plen, arc);
            munmap(payload, plen);
            write_file(obuf, arc.data(), arc.size());
        }
        return 0;
    }
    if (!std::strcmp(argv[1], "encode-stream")) {
        std::vector<uint8_t> in = read_fd(0);
        if (in.size() < 9) YB_FAIL("stream too short");
        if (std::memcmp(in.data(), HBI_MAGIC, 4) != 0) YB_FAIL("bad stream magic");
        if (in[4] != 1) YB_FAIL("bad stream version");
        uint32_t count = yb_get32be(in.data() + 5);
        if (count == 0 || count > 1000000u) YB_FAIL("bad stream count");
        std::vector<uint8_t> out;
        out.insert(out.end(), HBA_MAGIC, HBA_MAGIC + 4);
        out.push_back(1);
        out.push_back((uint8_t)(count >> 24));
        out.push_back((uint8_t)(count >> 16));
        out.push_back((uint8_t)(count >> 8));
        out.push_back((uint8_t)count);
        size_t pos = 9;
        for (uint32_t i = 0; i < count; i++) {
            if (in.size() - pos < 4) YB_FAIL("truncated stream");
            uint32_t alen = yb_get32be(in.data() + pos);
            pos += 4;
            if (in.size() - pos < (size_t)alen + 8) YB_FAIL("truncated stream");
            if (!yb_alias_ok(in.data() + pos, alen)) YB_FAIL("bad alias");
            const uint8_t* alias = in.data() + pos;
            pos += alen;
            uint64_t plen = yb_get64be(in.data() + pos);
            pos += 8;
            if (in.size() - pos < plen) YB_FAIL("truncated stream");
            std::vector<uint8_t> arc;
            build_archive(in.data() + pos, (size_t)plen, arc);
            pos += (size_t)plen;
            uint8_t b[4];
            yb_put32be(b, alen);
            out.insert(out.end(), b, b + 4);
            out.insert(out.end(), alias, alias + alen);
            uint8_t b8[8];
            yb_put64be(b8, arc.size());
            out.insert(out.end(), b8, b8 + 8);
            out.insert(out.end(), arc.begin(), arc.end());
        }
        size_t off = 0;
        while (off < out.size()) {
            ssize_t w = write(1, out.data() + off, out.size() - off);
            if (w <= 0) YB_FAIL("stdout write error");
            off += (size_t)w;
        }
        return 0;
    }
    YB_FAIL("unknown command");
}
