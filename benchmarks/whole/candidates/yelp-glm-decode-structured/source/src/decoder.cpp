// ybz decoder: reconstructs the exact original payload from an archive.
// See ybz_common.h for the header/stream formats and the section list.
#include "ybz_common.h"
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#include <string>

// ---------------- dict blob ----------------
struct StrRef { const uint8_t* s; uint32_t len; };
struct ValRef { const uint8_t* s; uint32_t len; uint8_t tag; };  // tag 0: quoted, 1: bare

struct Dicts {
    uint16_t ncity = 0;
    std::vector<StrRef> city;
    std::vector<uint8_t> city_state;      // 255 = ambiguous
    std::vector<uint16_t> city_zip_off, city_zip_cnt;
    uint16_t nzip = 0;
    std::vector<StrRef> zip;
    std::vector<uint16_t> city_zips;      // flattened per-city zip dict indices
    uint8_t nstate = 0;
    std::vector<StrRef> state;
    uint8_t nkeys = 0;
    std::vector<StrRef> key;
    std::vector<uint8_t> karena;
    std::vector<StrRef> keyq;      // '"' + key + '":"'  (prebuilt, single memcpy)
    std::vector<StrRef> keyqb;     // '"' + key + '":'   (bare values)
    std::vector<StrRef> keyc;      // ',"' + key + '":"' (with leading comma)
    std::vector<StrRef> keycb;     // ',"' + key + '":'
    std::vector<uint32_t> val_off, val_cnt;
    std::vector<ValRef> vals;
    uint16_t ncat = 0;
    std::vector<StrRef> cat;
    uint16_t nhour = 0;
    std::vector<StrRef> hour;
    uint16_t npat = 0;
    std::vector<StrRef> pat;              // day-index bytes
    uint8_t nstars = 0;
    std::vector<StrRef> stars;
    uint16_t nrc = 0;
    std::vector<StrRef> rc;
    uint8_t nlatip = 0, nlonip = 0;
    std::vector<StrRef> latip, lonip;
    std::vector<uint32_t> top;            // 1024 entries: common id -> row
};

static void parse_dict_blob(const uint8_t* b, size_t n, uint32_t hdr_nkeys, Dicts& D) {
    YbRd rd{b, b + n};
    auto str_u8 = [&](std::vector<StrRef>& v, uint32_t cnt) {
        v.resize(cnt);
        for (uint32_t i = 0; i < cnt; i++) {
            uint32_t len = rd.u8();
            v[i] = {rd.raw(len), len};
        }
    };
    D.ncity = rd.u16();
    str_u8(D.city, D.ncity);
    D.nstate = rd.u8();
    str_u8(D.state, D.nstate);
    D.city_state.resize(D.ncity);
    for (uint32_t i = 0; i < D.ncity; i++) D.city_state[i] = rd.u8();
    D.nzip = rd.u16();
    str_u8(D.zip, D.nzip);
    D.city_zip_off.resize(D.ncity);
    D.city_zip_cnt.resize(D.ncity);
    for (uint32_t i = 0; i < D.ncity; i++) {
        uint32_t cnt = rd.u16();
        D.city_zip_off[i] = (uint32_t)D.city_zips.size();
        D.city_zip_cnt[i] = (uint16_t)cnt;
        for (uint32_t j = 0; j < cnt; j++) {
            uint16_t z = rd.u16();
            if (z >= D.nzip) YB_FAIL("zip index out of range");
            D.city_zips.push_back(z);
        }
    }
    D.nkeys = rd.u8();
    if (D.nkeys != hdr_nkeys) YB_FAIL("nkeys mismatch");
    str_u8(D.key, D.nkeys);
    // build quoted-key prefixes in a side arena (decoder-only convenience)
    {
        D.keyq.resize(D.nkeys); D.keyqb.resize(D.nkeys);
        D.keyc.resize(D.nkeys); D.keycb.resize(D.nkeys);
        for (uint32_t k = 0; k < D.nkeys; k++) {
            uint32_t kl = D.key[k].len;
            D.keyq[k].len = 1 + kl + 3;
            D.keyqb[k].len = 1 + kl + 2;
            D.keyc[k].len = 2 + kl + 3;
            D.keycb[k].len = 2 + kl + 2;
        }
        size_t tot = 0;
        for (uint32_t k = 0; k < D.nkeys; k++) tot += D.keyq[k].len + D.keyqb[k].len + D.keyc[k].len + D.keycb[k].len;
        D.karena.resize(tot);
        uint8_t* p = D.karena.data();
        for (uint32_t k = 0; k < D.nkeys; k++) {
            uint32_t kl = D.key[k].len;
            D.keyq[k].s = p;  *p++ = '"';  std::memcpy(p, D.key[k].s, kl); p += kl;
            *p++ = '"'; *p++ = ':'; *p++ = '"';
            D.keyqb[k].s = p; *p++ = '"'; std::memcpy(p, D.key[k].s, kl); p += kl;
            *p++ = '"'; *p++ = ':';
            D.keyc[k].s = p;  *p++ = ','; *p++ = '"'; std::memcpy(p, D.key[k].s, kl); p += kl;
            *p++ = '"'; *p++ = ':'; *p++ = '"';
            D.keycb[k].s = p; *p++ = ','; *p++ = '"'; std::memcpy(p, D.key[k].s, kl); p += kl;
            *p++ = '"'; *p++ = ':';
        }
    }
    D.val_off.resize(D.nkeys);
    D.val_cnt.resize(D.nkeys);
    for (uint32_t k = 0; k < D.nkeys; k++) {
        uint32_t nv = rd.u16();
        D.val_off[k] = (uint32_t)D.vals.size();
        D.val_cnt[k] = nv;
        for (uint32_t j = 0; j < nv; j++) {
            uint8_t tag = rd.u8();
            uint32_t len = rd.u16();
            if (tag > 1) YB_FAIL("bad value tag");
            D.vals.push_back({rd.raw(len), len, tag});
        }
    }
    D.ncat = rd.u16();
    str_u8(D.cat, D.ncat);
    D.nhour = rd.u16();
    str_u8(D.hour, D.nhour);
    D.npat = rd.u16();
    str_u8(D.pat, D.npat);
    for (uint32_t i = 0; i < D.npat; i++)
        for (uint32_t j = 0; j < D.pat[i].len; j++)
            if (D.pat[i].s[j] >= 7) YB_FAIL("bad day index");
    D.nstars = rd.u8();
    str_u8(D.stars, D.nstars);
    D.nrc = rd.u16();
    str_u8(D.rc, D.nrc);
    D.nlatip = rd.u8();
    str_u8(D.latip, D.nlatip);
    D.nlonip = rd.u8();
    str_u8(D.lonip, D.nlonip);
    D.top.resize(YB_TOPKS);
    for (uint32_t i = 0; i < YB_TOPKS; i++) D.top[i] = rd.u32();
    if (rd.p != rd.end) YB_FAIL("trailing dict blob bytes");
}

// ---------------- payload decode ----------------
static constexpr uint8_t DAY_NAMES[7][10] = {"Monday", "Tuesday", "Wednesday",
                                             "Thursday", "Friday", "Saturday", "Sunday"};
static constexpr uint8_t DAY_LEN[7] = {6, 7, 9, 8, 6, 8, 6};

struct Archive {
    std::vector<uint8_t> out;
};

// out_buf receives a malloc'd (uninitialized) buffer of *out_len bytes
static void decode_payload(const uint8_t* in, size_t in_len, uint8_t** out_buf, uint64_t* out_len) {
    if (in_len < 40) YB_FAIL("archive too short");
    if (std::memcmp(in, YBZ_MAGIC, 4) != 0) YB_FAIL("bad magic");
    // integrity: hash of the whole archive with the hash field zeroed
    uint64_t stored = yb_get64(in + 32);
    {
        // hash of the whole archive with the hash field zeroed; the field is
        // 8 bytes so hashing [0,32) + 8 zeros + [40,end) equals hashing the
        // flattened zeroed archive
        std::vector<uint8_t> tmp;
        tmp.reserve(40);
        tmp.insert(tmp.end(), in, in + 32);
        tmp.insert(tmp.end(), {0, 0, 0, 0, 0, 0, 0, 0});
        tmp.insert(tmp.end(), in + 40, in + in_len);
        if (yb_hash64(tmp.data(), tmp.size()) != stored) YB_FAIL("archive hash mismatch");
    }
    uint32_t mode = in[4];
    uint32_t flags = in[5];
    uint64_t orig_len = yb_get64(in + 8);
    uint64_t nrec = yb_get64(in + 16);
    uint32_t nkeys = yb_get32(in + 24);
    if (orig_len > (1ull << 31)) YB_FAIL("orig_len too large");
    if (mode > 1) YB_FAIL("bad mode");
    const uint8_t* body = in + 40;
    size_t body_len = in_len - 40;

    if (mode == 1) {
        if (body_len != orig_len) YB_FAIL("raw length mismatch");
        uint8_t* buf = (uint8_t*)std::malloc(body_len ? body_len : 1);
        std::memcpy(buf, body, body_len);
        *out_buf = buf;
        *out_len = body_len;
        return;
    }
    if (nrec == 0 || nkeys == 0 || nkeys > 200) YB_FAIL("bad counts");
    uint8_t* O = (uint8_t*)std::malloc((size_t)orig_len);
    if (!O) YB_FAIL("output allocation failed");
    *out_buf = O;
    *out_len = orig_len;
    size_t pos = 0;
    const bool trailing = flags & 1;

    YbRd rd{body, body + body_len};
    const uint8_t* ids = rd.raw((size_t)YB_ID_PACKED * nrec);

    auto rd_stream = [&](YbStreamIn& st) { yb_stream_read_header(rd, st); };
    auto dec_syms = [&](YbStreamIn& st) {
        std::vector<uint32_t> v(st.nsyms);
        yb_stream_decode_syms(st, v.data());
        return v;
    };

    YbStreamIn st_name_len, st_addr_len, st_name_text, st_addr_text;
    YbStreamIn st_city, st_zip, st_lat_ip, st_lat_fl, st_lon_ip, st_lon_fl;
    YbStreamIn st_lat_d[YB_NPOS], st_lon_d[YB_NPOS];
    YbStreamIn st_stars, st_rc, st_io, st_ks;
    YbStreamIn st_ks_pfx, st_ks_sfx_len, st_ks_sfx;
    std::vector<YbStreamIn> st_val(nkeys);
    YbStreamIn st_cat_null, st_cat_cnt, st_cat_tok;
    YbStreamIn st_hr_null, st_hr_pat, st_hr_same, st_hr_val[7];
    YbStreamIn st_state_excep, st_blob;

    rd_stream(st_name_len);
    rd_stream(st_addr_len);
    rd_stream(st_name_text);
    rd_stream(st_addr_text);
    rd_stream(st_city);
    rd_stream(st_zip);
    rd_stream(st_lat_ip);
    rd_stream(st_lat_fl);
    for (int p = 0; p < YB_NPOS; p++) rd_stream(st_lat_d[p]);
    rd_stream(st_lon_ip);
    rd_stream(st_lon_fl);
    for (int p = 0; p < YB_NPOS; p++) rd_stream(st_lon_d[p]);
    rd_stream(st_stars);
    rd_stream(st_rc);
    rd_stream(st_io);
    rd_stream(st_ks);
    uint32_t nesc = rd.u32();
    std::vector<uint32_t> escapes(nesc);
    for (uint32_t i = 0; i < nesc; i++) escapes[i] = rd.u16();
    rd_stream(st_ks_pfx);
    rd_stream(st_ks_sfx_len);
    rd_stream(st_ks_sfx);
    for (uint32_t k = 0; k < nkeys; k++) rd_stream(st_val[k]);
    rd_stream(st_cat_null);
    rd_stream(st_cat_cnt);
    rd_stream(st_cat_tok);
    rd_stream(st_hr_null);
    rd_stream(st_hr_pat);
    rd_stream(st_hr_same);
    for (int p = 0; p < 7; p++) rd_stream(st_hr_val[p]);
    rd_stream(st_state_excep);
    uint32_t declen = rd.u32();
    rd_stream(st_blob);
    if (rd.p != rd.end) YB_FAIL("trailing archive bytes");

    // decode symbol arrays
    auto V_name_len = dec_syms(st_name_len);
    auto V_addr_len = dec_syms(st_addr_len);
    if (V_name_len.size() != nrec || V_addr_len.size() != nrec) YB_FAIL("length stream size");
    auto V_city = dec_syms(st_city);
    auto V_zip = dec_syms(st_zip);
    auto V_lat_ip = dec_syms(st_lat_ip);
    auto V_lat_fl = dec_syms(st_lat_fl);
    std::vector<std::vector<uint32_t>> V_lat_d(YB_NPOS), V_lon_d(YB_NPOS);
    for (int p = 0; p < YB_NPOS; p++) V_lat_d[p] = dec_syms(st_lat_d[p]);
    auto V_lon_ip = dec_syms(st_lon_ip);
    auto V_lon_fl = dec_syms(st_lon_fl);
    for (int p = 0; p < YB_NPOS; p++) V_lon_d[p] = dec_syms(st_lon_d[p]);
    auto V_stars = dec_syms(st_stars);
    auto V_rc = dec_syms(st_rc);
    auto V_io = dec_syms(st_io);
    auto V_ks = dec_syms(st_ks);
    auto V_ks_pfx = dec_syms(st_ks_pfx);
    auto V_ks_sfx_len = dec_syms(st_ks_sfx_len);
    auto V_cat_null = dec_syms(st_cat_null);
    auto V_cat_cnt = dec_syms(st_cat_cnt);
    auto V_cat_tok = dec_syms(st_cat_tok);
    auto V_hr_null = dec_syms(st_hr_null);
    auto V_hr_pat = dec_syms(st_hr_pat);
    auto V_hr_same = dec_syms(st_hr_same);
    std::vector<std::vector<uint32_t>> V_hr_val(7);
    for (int p = 0; p < 7; p++) V_hr_val[p] = dec_syms(st_hr_val[p]);
    auto V_state_ex = dec_syms(st_state_excep);

    // dict blob
    std::vector<uint8_t> blob(declen);
    yb_stream_decode_text(st_blob, blob.data(), declen);
    Dicts D;
    parse_dict_blob(blob.data(), blob.size(), nkeys, D);

    // keyset rows
    uint32_t nrows = (uint32_t)V_ks_pfx.size();
    if (V_ks_sfx_len.size() != nrows) YB_FAIL("keyset row count mismatch");
    std::vector<uint32_t> row_off(nrows + 1);
    std::vector<uint8_t> rows;
    rows.reserve((size_t)nrows * 8);
    {
        std::vector<uint8_t> sfx((size_t)st_ks_sfx.nsyms);
        yb_stream_decode_text(st_ks_sfx, sfx.data(), st_ks_sfx.nsyms);
        size_t sfx_pos = 0;
        for (uint32_t i = 0; i < nrows; i++) {
            uint32_t pfx = V_ks_pfx[i];
            uint32_t sfxn = V_ks_sfx_len[i];
            if (i == 0 && pfx != 0) YB_FAIL("first keyset prefix");
            // rows[] holds rows 0..i-1; row i-1 occupies [row_off[i-1], row_off[i])
            uint32_t prev_len = i ? row_off[i] - row_off[i - 1] : 0;
            if (pfx > prev_len) YB_FAIL("keyset prefix too long");
            for (uint32_t j = 0; j < pfx; j++) rows.push_back(rows[row_off[i - 1] + j]);
            if (sfx_pos + sfxn > sfx.size()) YB_FAIL("keyset suffix overrun");
            for (uint32_t j = 0; j < sfxn; j++) {
                if (sfx[sfx_pos + j] >= D.nkeys) YB_FAIL("keyset key index");
                rows.push_back(sfx[sfx_pos + j]);
            }
            sfx_pos += sfxn;
            if (rows.size() - row_off[i] > D.nkeys) YB_FAIL("keyset too long");
            row_off[i + 1] = (uint32_t)rows.size();
        }
        if (sfx_pos != sfx.size()) YB_FAIL("keyset suffix underrun");
    }

    auto emit = [&](const void* p, size_t n) {
        std::memcpy(O + pos, p, n);
        pos += n;
    };

    // streaming text decoders
    YbTextDec tdn, tda;
    {
        uint64_t total = 0;
        for (uint64_t v : V_name_len) total += v;
        if (total != st_name_text.nsyms) YB_FAIL("name text size mismatch");
        total = 0;
        for (uint64_t v : V_addr_len) total += v;
        if (total != st_addr_text.nsyms) YB_FAIL("addr text size mismatch");
    }
    tdn.start(st_name_text);
    tda.start(st_addr_text);

    // consumption cursors
    size_t cur_cat_tok = 0, cur_hr_same = 0, cur_state = 0, cur_esc = 0, cur_pat = 0;
    size_t cur_latd[YB_NPOS] = {0}, cur_lond[YB_NPOS] = {0}, cur_hrv[7] = {0};
    std::vector<std::vector<uint32_t>> V_val(nkeys);
    std::vector<size_t> cur_val(nkeys, 0);
    for (uint32_t k = 0; k < nkeys; k++) V_val[k] = dec_syms(st_val[k]);

    for (uint64_t r = 0; r < nrec; r++) {
        emit(YB_T1, sizeof(YB_T1) - 1);
        // id
        {
            const uint8_t* ib = ids + (size_t)r * YB_ID_PACKED;
            char tmp[YB_ID_LEN];
            for (int i = 0; i < YB_ID_LEN; i++) {
                uint32_t bit = 6 * i;
                uint16_t v = (yb_get16(ib + (bit >> 3)) >> (bit & 7)) & 63;
                tmp[i] = YB_ID_ALPHABET[v];
            }
            emit(tmp, YB_ID_LEN);
            emit("\"", 1);
        }
        emit(YB_T2, sizeof(YB_T2) - 1);
        {
            uint32_t nl = V_name_len[r], al = V_addr_len[r];
            uint8_t* on = O + pos;
            O[pos + nl] = '"';
            std::memcpy(O + pos + nl + 1, YB_T3, sizeof(YB_T3) - 1);
            uint8_t* oa = O + pos + nl + 1 + (sizeof(YB_T3) - 1);
            uint32_t ni = 0, ai = 0;
            while (ni < nl || ai < al) {
                if (ni < nl) { on[ni] = tdn.next(); ni++; }
                if (ai < al) { oa[ai] = tda.next(); ai++; }
            }
            pos += nl + al + 1 + (sizeof(YB_T3) - 1);
            emit("\"", 1);
        }
        emit(YB_T4, sizeof(YB_T4) - 1);
        {
            uint32_t c = V_city[r];
            if (c >= D.ncity) YB_FAIL("city index");
            emit(D.city[c].s, D.city[c].len);
            emit("\"", 1);
        }
        emit(YB_T5, sizeof(YB_T5) - 1);
        {
            uint32_t c = V_city[r];
            uint32_t st = D.city_state[c];
            if (st == 255) {
                if (cur_state >= V_state_ex.size()) YB_FAIL("state exception underrun");
                st = V_state_ex[cur_state++];
                if (st >= D.nstate) YB_FAIL("state index");
            }
            emit(D.state[st].s, D.state[st].len);
            emit("\"", 1);
        }
        emit(YB_T6, sizeof(YB_T6) - 1);
        {
            uint32_t c = V_city[r];
            uint32_t z = V_zip[r];
            if (z >= D.city_zip_cnt[c]) YB_FAIL("zip index");
            uint32_t zi = D.city_zips[D.city_zip_off[c] + z];
            emit(D.zip[zi].s, D.zip[zi].len);
            emit("\"", 1);
        }
        emit(YB_T7, sizeof(YB_T7) - 1);
        {
            uint32_t ip = V_lat_ip[r];
            if (ip >= D.nlatip) YB_FAIL("lat int index");
            emit(D.latip[ip].s, D.latip[ip].len);
            uint32_t fl = V_lat_fl[r];
            if (fl > YB_NPOS) YB_FAIL("lat frac length");
            if (fl) {
                emit(".", 1);
                for (int p = 0; p < YB_NPOS; p++) {
                    if ((uint32_t)p >= fl) break;
                    if (cur_latd[p] >= V_lat_d[p].size()) YB_FAIL("lat digits underrun");
                    O[pos++] = (uint8_t)('0' + V_lat_d[p][cur_latd[p]++]);
                }
            }
        }
        emit(YB_T8, sizeof(YB_T8) - 1);
        {
            uint32_t ip = V_lon_ip[r];
            if (ip >= D.nlonip) YB_FAIL("lon int index");
            emit(D.lonip[ip].s, D.lonip[ip].len);
            uint32_t fl = V_lon_fl[r];
            if (fl > YB_NPOS) YB_FAIL("lon frac length");
            if (fl) {
                emit(".", 1);
                for (int p = 0; p < YB_NPOS; p++) {
                    if ((uint32_t)p >= fl) break;
                    if (cur_lond[p] >= V_lon_d[p].size()) YB_FAIL("lon digits underrun");
                    O[pos++] = (uint8_t)('0' + V_lon_d[p][cur_lond[p]++]);
                }
            }
        }
        emit(YB_T9, sizeof(YB_T9) - 1);
        {
            uint32_t s = V_stars[r];
            if (s >= D.nstars) YB_FAIL("stars index");
            emit(D.stars[s].s, D.stars[s].len);
        }
        emit(YB_T10, sizeof(YB_T10) - 1);
        {
            uint32_t s = V_rc[r];
            if (s >= D.nrc) YB_FAIL("rc index");
            emit(D.rc[s].s, D.rc[s].len);
        }
        emit(YB_T11, sizeof(YB_T11) - 1);
        {
            uint32_t s = V_io[r];
            if (s >= 2) YB_FAIL("io index");
            emit(s ? "1" : "0", 1);
        }
        emit(YB_T12, sizeof(YB_T12) - 1);
        {
            uint32_t ksym = V_ks[r];
            if (ksym == YB_KS_NULL) {
                emit(YB_NULL, 4);
            } else {
                uint32_t row;
                if (ksym == YB_KS_ESC) {
                    if (cur_esc >= escapes.size()) YB_FAIL("escape underrun");
                    row = escapes[cur_esc++];
                } else {
                    if (ksym >= YB_TOPKS) YB_FAIL("keyset symbol");
                    row = D.top[ksym];
                }
                if (row >= nrows) YB_FAIL("keyset row");
                emit("{", 1);
                for (uint32_t j = row_off[row]; j < row_off[row + 1]; j++) {
                    if (j > row_off[row]) emit(",", 1);
                    uint8_t k = rows[j];
                    if (k >= D.nkeys) YB_FAIL("key index");
                    emit("\"", 1);
                    emit(D.key[k].s, D.key[k].len);
                    emit("\":", 2);
                    if (cur_val[k] >= V_val[k].size()) YB_FAIL("attr value underrun");
                    uint32_t vi = V_val[k][cur_val[k]++];
                    if (vi >= D.val_cnt[k]) YB_FAIL("attr value index");
                    const ValRef& vr = D.vals[D.val_off[k] + vi];
                    if (vr.tag == 0) emit("\"", 1);
                    emit(vr.s, vr.len);
                    if (vr.tag == 0) emit("\"", 1);
                }
                emit("}", 1);
            }
        }
        emit(YB_T13, sizeof(YB_T13) - 1);
        {
            if (V_cat_null[r] == 0) {
                emit(YB_NULL, 4);
            } else {
                uint32_t cnt = V_cat_cnt[r];
                emit("\"", 1);
                for (uint32_t j = 0; j < cnt; j++) {
                    if (j) emit(", ", 2);
                    if (cur_cat_tok >= V_cat_tok.size()) YB_FAIL("cat token underrun");
                    uint32_t t = V_cat_tok[cur_cat_tok++];
                    if (t >= D.ncat) YB_FAIL("cat token index");
                    emit(D.cat[t].s, D.cat[t].len);
                }
                emit("\"", 1);
            }
        }
        emit(YB_T14, sizeof(YB_T14) - 1);
        {
            if (V_hr_null[r] == 0) {
                emit(YB_NULL, 4);
            } else {
                if (cur_pat >= V_hr_pat.size()) YB_FAIL("hours pattern underrun");
                uint32_t pt = V_hr_pat[cur_pat++];
                if (pt >= D.npat) YB_FAIL("hours pattern");
                StrRef pr = D.pat[pt];
                emit("{", 1);
                uint32_t prev_val = UINT32_MAX;
                for (uint32_t j = 0; j < pr.len; j++) {
                    if (j) emit(",", 1);
                    uint8_t day = pr.s[j];
                    emit("\"", 1);
                    emit(DAY_NAMES[day], DAY_LEN[day]);
                    emit("\":\"", 3);
                    uint32_t v;
                    if (j > 0) {
                        if (cur_hr_same >= V_hr_same.size()) YB_FAIL("hours same underrun");
                        uint32_t same = V_hr_same[cur_hr_same++];
                        if (same == 0) {
                            v = prev_val;
                        } else {
                            if (cur_hrv[j] >= V_hr_val[j].size()) YB_FAIL("hours value underrun");
                            v = V_hr_val[j][cur_hrv[j]++];
                        }
                    } else {
                        if (cur_hrv[0] >= V_hr_val[0].size()) YB_FAIL("hours value underrun");
                        v = V_hr_val[0][cur_hrv[0]++];
                    }
                    if (v >= D.nhour) YB_FAIL("hours value index");
                    emit(D.hour[v].s, D.hour[v].len);
                    emit("\"", 1);
                    prev_val = v;
                }
                emit("}", 1);
            }
        }
        emit(YB_T15, sizeof(YB_T15) - 1);
        if (r + 1 < nrec || trailing) emit("\n", 1);
    }

    // cursor consistency checks
    if (cur_cat_tok != V_cat_tok.size()) YB_FAIL("cat tokens leftover");
    if (cur_hr_same != V_hr_same.size()) YB_FAIL("hours flags leftover");
    if (cur_state != V_state_ex.size()) YB_FAIL("state exceptions leftover");
    if (cur_esc != escapes.size()) YB_FAIL("escapes leftover");
    for (int p = 0; p < YB_NPOS; p++) {
        if (cur_latd[p] != V_lat_d[p].size() || cur_lond[p] != V_lon_d[p].size())
            YB_FAIL("digit stream leftover");
    }
    for (int p = 0; p < 7; p++)
        if (cur_hrv[p] != V_hr_val[p].size()) YB_FAIL("hours values leftover");
    if (cur_pat != V_hr_pat.size()) YB_FAIL("hours patterns leftover");
    for (uint32_t k = 0; k < nkeys; k++)
        if (cur_val[k] != V_val[k].size()) YB_FAIL("attr values leftover");
    if (pos != orig_len) YB_FAIL("output size mismatch");
}

// ---------------- file IO ----------------
static std::vector<uint8_t> read_file(const char* path, bool from_stdin = false) {
    std::vector<uint8_t> data;
    const size_t CH = 1 << 20;
    uint8_t buf[CH];
    int fd = from_stdin ? 0 : -1;
    if (!from_stdin) {
        fd = open(path, O_RDONLY);
        if (fd < 0) YB_FAIL("cannot open input");
    }
    for (;;) {
        ssize_t n = read(fd, buf, CH);
        if (n < 0) YB_FAIL("read error");
        if (n == 0) break;
        data.insert(data.end(), buf, buf + n);
    }
    if (!from_stdin) close(fd);
    return data;
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

static void write_stdout(const uint8_t* p, size_t n) {
    size_t off = 0;
    while (off < n) {
        ssize_t w = write(1, p + off, n - off);
        if (w <= 0) YB_FAIL("stdout write error");
        off += (size_t)w;
    }
}

int main(int argc, char** argv) {
    if (argc < 2) YB_FAIL("usage: decoder decode-dir IN OUT | decode-stream");
    if (!std::strcmp(argv[1], "decode-dir")) {
        if (argc != 4) YB_FAIL("usage: decoder decode-dir IN OUT");
        std::vector<uint8_t> names = read_file((std::string(argv[2]) + "/names.bin").c_str());
        if (names.size() < 9) YB_FAIL("names.bin too short");
        std::vector<uint8_t> names_out;
        // transcode validates the container (count, alias charset, framing)
        if (!yb_container_transcode(names.data(), names.size(), HBI_MAGIC, names_out))
            YB_FAIL("bad names.bin");
        uint32_t count = yb_get32be(names.data() + 5);
        // decode everything BEFORE writing anything: a failing archive must not
        // leave partial output in the output directory
        std::vector<uint8_t*> outps(count, nullptr);
        std::vector<uint64_t> outns(count, 0);
        for (uint32_t i = 0; i < count; i++) {
            char pbuf[512];
            std::snprintf(pbuf, sizeof pbuf, "%s/%08u.bin", argv[2], i);
            std::vector<uint8_t> payload = read_file(pbuf);
            decode_payload(payload.data(), payload.size(), &outps[i], &outns[i]);
        }
        write_file((std::string(argv[3]) + "/names.bin").c_str(), names_out.data(), names_out.size());
        for (uint32_t i = 0; i < count; i++) {
            char obuf[512];
            std::snprintf(obuf, sizeof obuf, "%s/%08u.bin", argv[3], i);
            write_file(obuf, outps[i], outns[i]);
            std::free(outps[i]);
        }
        return 0;
    }
    if (!std::strcmp(argv[1], "decode-stream")) {
        std::vector<uint8_t> in = read_file(nullptr, true);
        if (in.size() < 9) YB_FAIL("stream too short");
        if (std::memcmp(in.data(), HBA_MAGIC, 4) != 0) YB_FAIL("bad stream magic");
        if (in[4] != 1) YB_FAIL("bad stream version");
        uint32_t count = yb_get32be(in.data() + 5);
        if (count == 0 || count > 1000000u) YB_FAIL("bad stream count");
        std::vector<uint8_t> out;
        out.reserve(in.size());
        out.insert(out.end(), HBI_MAGIC, HBI_MAGIC + 4);
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
            uint8_t* outp = nullptr;
            uint64_t outn = 0;
            decode_payload(in.data() + pos, (size_t)plen, &outp, &outn);
            pos += (size_t)plen;
            uint8_t b[4];
            yb_put32be(b, alen);
            out.insert(out.end(), b, b + 4);
            out.insert(out.end(), alias, alias + alen);
            uint8_t b8[8];
            yb_put64be(b8, outn);
            out.insert(out.end(), b8, b8 + 8);
            out.insert(out.end(), outp, outp + outn);
            std::free(outp);
        }
        if (pos != in.size()) YB_FAIL("trailing stream bytes");
        write_stdout(out.data(), out.size());
        return 0;
    }
    YB_FAIL("unknown command");
}
