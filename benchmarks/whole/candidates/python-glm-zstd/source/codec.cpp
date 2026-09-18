// RSPZ: region-streaming parallel zstd candidate for the whole-corpus Python task.
// One source file builds both roles: encoder (codec) and decoder (decoder).
//   codec:   HBI1 stdin -> HBA1 stdout;  encode-dir {input_dir} {output_dir}
//   decoder: HBA1 stdin -> HBI1 stdout;  decode-dir {input_dir} {output_dir}
// Record container: 32B envelope (magic "CLB1") holding a "PZS1" container with
// 0..4 concatenated single-threaded zstd frames (one per region, XXH64 checksums).
// Incompressible or tiny payloads fall back to stored bytes (flag bit 0).
#ifndef CL_DECODER
#define ZSTD_STATIC_LINKING_ONLY
#endif
#include <zstd.h>
#include <dirent.h>
#include <sys/stat.h>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>
#include <thread>
#include <atomic>
#include <algorithm>

namespace {

[[noreturn]] void fail(const std::string& msg) {
    std::fprintf(stderr, "rspz: %s\n", msg.c_str());
    std::exit(2);
}

// ---------- slice-by-8 CRC-32 (IEEE) ----------
uint32_t crc_table[8][256];
struct CrcInit {
    CrcInit() {
        for (uint32_t i = 0; i < 256; i++) {
            uint32_t c = i;
            for (int k = 0; k < 8; k++) c = (c & 1) ? 0xEDB88320u ^ (c >> 1) : c >> 1;
            crc_table[0][i] = c;
        }
        for (uint32_t i = 0; i < 256; i++)
            for (int t = 1; t < 8; t++)
                crc_table[t][i] = (crc_table[t - 1][i] >> 8) ^ crc_table[0][crc_table[t - 1][i] & 0xFF];
    }
} crc_init;

uint32_t crc32_buf(const uint8_t* buf, size_t len, uint32_t crc = 0) {
    crc = ~crc;
    while (len >= 8) {
        crc ^= (uint32_t)buf[0] | ((uint32_t)buf[1] << 8) | ((uint32_t)buf[2] << 16) |
               ((uint32_t)buf[3] << 24);
        uint32_t hi = (uint32_t)buf[4] | ((uint32_t)buf[5] << 8) | ((uint32_t)buf[6] << 16) |
                      ((uint32_t)buf[7] << 24);
        crc = crc_table[7][crc & 0xFF] ^ crc_table[6][(crc >> 8) & 0xFF] ^
              crc_table[5][(crc >> 16) & 0xFF] ^ crc_table[4][crc >> 24] ^
              crc_table[3][hi & 0xFF] ^ crc_table[2][(hi >> 8) & 0xFF] ^
              crc_table[1][(hi >> 16) & 0xFF] ^ crc_table[0][hi >> 24];
        buf += 8;
        len -= 8;
    }
    while (len--) crc = crc_table[0][(crc ^ *buf++) & 0xFF] ^ (crc >> 8);
    return ~crc;
}

// ---------- GF(2) CRC combine (zlib crc32_combine algorithm) ----------
uint32_t gf2_matrix_times(const uint32_t* mat, uint32_t vec) {
    uint32_t sum = 0;
    while (vec) {
        if (vec & 1) sum ^= *mat;
        vec >>= 1;
        mat++;
    }
    return sum;
}

void gf2_matrix_square(uint32_t* square, const uint32_t* mat) {
    for (int n = 0; n < 32; n++) square[n] = gf2_matrix_times(mat, mat[n]);
}

uint32_t crc32_combine(uint32_t crc1, uint32_t crc2, uint64_t len2) {
    uint32_t even[32], odd[32];
    if (len2 == 0) return crc1 ^ crc2;
    odd[0] = 0xEDB88320u;
    uint32_t row = 1;
    for (int n = 1; n < 32; n++) {
        odd[n] = row;
        row <<= 1;
    }
    gf2_matrix_square(even, odd);
    gf2_matrix_square(odd, even);
    do {
        gf2_matrix_square(even, odd);
        if (len2 & 1) crc1 = gf2_matrix_times(even, crc1);
        len2 >>= 1;
        if (len2 == 0) break;
        gf2_matrix_square(odd, even);
        if (len2 & 1) crc1 = gf2_matrix_times(odd, crc1);
        len2 >>= 1;
    } while (len2 != 0);
    return crc1 ^ crc2;
}

// ---------- strict packed transport ----------
constexpr uint32_t MAGIC_ORIG = 0x48424931u;  // "HBI1"
constexpr uint32_t MAGIC_ARCH = 0x48424131u;  // "HBA1"
constexpr size_t MAX_ALIAS = 1048576;
constexpr uint32_t MAX_RECORDS = 1000000;
constexpr uint64_t MAX_CANONICAL = 2147483648ull;  // card output cap

bool alias_ok(const uint8_t* a, size_t len) {
    if (len < 1 || len > MAX_ALIAS) return false;
    bool word = false;
    for (size_t i = 0; i < len; i++) {
        uint8_t c = a[i];
        if ((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')) {
            word = true;
            continue;
        }
        if (c == '-') {
            if (!word) return false;
            word = false;
            continue;
        }
        return false;
    }
    return word;
}

struct StreamParser {
    const uint8_t* p;
    size_t n, off = 0;
    explicit StreamParser(const std::vector<uint8_t>& buf) : p(buf.data()), n(buf.size()) {}
    void take(size_t k, const uint8_t*& out) {
        if (n - off < k) fail("truncated packed stream");
        out = p + off;
        off += k;
    }
    uint32_t u32be() {
        if (n - off < 4) fail("truncated packed stream (u32)");
        uint32_t v = ((uint32_t)p[off] << 24) | ((uint32_t)p[off + 1] << 16) |
                     ((uint32_t)p[off + 2] << 8) | p[off + 3];
        off += 4;
        return v;
    }
    uint64_t u64be() {
        if (n - off < 8) fail("truncated packed stream (u64)");
        uint64_t v = 0;
        for (int i = 0; i < 8; i++) v = (v << 8) | p[off + i];
        off += 8;
        return v;
    }
};

struct Record {
    const uint8_t* alias;
    size_t alias_len;
    const uint8_t* payload;
    size_t payload_len;
};

void parse_stream(const std::vector<uint8_t>& buf, uint32_t expect_magic,
                  std::vector<Record>& out) {
    StreamParser r(buf);
    if (buf.size() < 9) fail("truncated stream header");
    uint32_t magic = ((uint32_t)buf[0] << 24) | ((uint32_t)buf[1] << 16) |
                     ((uint32_t)buf[2] << 8) | buf[3];
    if (magic != expect_magic) fail("stream magic mismatch");
    if (buf[4] != 1) fail("unsupported stream version");
    uint32_t count = ((uint32_t)buf[5] << 24) | ((uint32_t)buf[6] << 16) |
                     ((uint32_t)buf[7] << 8) | buf[8];
    if (count < 1 || count > MAX_RECORDS) fail("invalid record count");
    r.off = 9;
    const uint8_t* prev = nullptr;
    size_t prev_len = 0;
    bool have_prev = false;
    out.reserve(count < 4096 ? count : 4096);
    for (uint32_t i = 0; i < count; i++) {
        uint32_t alen = r.u32be();
        if (alen < 1 || alen > MAX_ALIAS) fail("invalid alias length");
        const uint8_t* alias;
        r.take(alen, alias);
        if (!alias_ok(alias, alen)) fail("invalid alias bytes");
        if (have_prev) {
            size_t cmp = std::min(prev_len, (size_t)alen);
            int c = std::memcmp(prev, alias, cmp);
            if (c > 0 || (c == 0 && prev_len >= alen)) fail("aliases not strictly increasing");
        }
        prev = alias;
        prev_len = alen;
        have_prev = true;
        uint64_t plen = r.u64be();
        if (plen > (uint64_t)(buf.size() - r.off)) fail("payload exceeds stream");
        const uint8_t* payload;
        r.take((size_t)plen, payload);
        out.push_back(Record{alias, alen, payload, (size_t)plen});
    }
    if (r.off != r.n) fail("trailing bytes after stream");
}

// ---------- PZS1 record codec ----------
constexpr uint32_t ENV_MAGIC = 0x434C4231u;  // "CLB1"
constexpr uint32_t PZS_MAGIC = 0x505A5331u;  // "PZS1"
constexpr uint8_t FLAG_STORED = 0x01;
constexpr size_t ENV_SIZE = 32;
constexpr size_t PZS_HEAD = 16;  // magic u32, ver u8, flags u8, regionCount u16, origSize u64
constexpr int MAX_REGIONS = 4;

int region_count_for(uint64_t n) {
    if (n == 0) return 0;
    uint64_t r = (n + (1u << 20) - 1) / (1u << 20);
    return (int)(r < (uint64_t)MAX_REGIONS ? r : (uint64_t)MAX_REGIONS);
}

void compress_region(const uint8_t* src, size_t len, std::vector<uint8_t>& dst,
                     uint32_t& region_crc) {
    region_crc = crc32_buf(src, len);
    ZSTD_CCtx* c = ZSTD_createCCtx();
    if (!c) fail("cctx allocation failed");
    ZSTD_CCtx_reset(c, ZSTD_reset_session_and_parameters);
    ZSTD_CCtx_setParameter(c, ZSTD_c_compressionLevel, CL_LEVEL);
    ZSTD_CCtx_setParameter(c, ZSTD_c_windowLog, CL_WLOG);
    ZSTD_CCtx_setParameter(c, ZSTD_c_nbWorkers, 0);
#if CL_SLOG > 0
    ZSTD_CCtx_setParameter(c, ZSTD_c_searchLog, CL_SLOG);
#endif
#if CL_HLOG > 0
    ZSTD_CCtx_setParameter(c, ZSTD_c_hashLog, CL_HLOG);
#endif
    ZSTD_CCtx_setParameter(c, ZSTD_c_checksumFlag, 1);
    ZSTD_CCtx_setPledgedSrcSize(c, (unsigned long long)len);
    dst.resize(ZSTD_compressBound(len) + 64);
    ZSTD_outBuffer out{dst.data(), dst.size(), 0};
    size_t done = 0;
    while (done < len) {
        size_t inChunk = len - done < (size_t)(1 << 20) ? len - done : (size_t)(1 << 20);
        ZSTD_inBuffer in{src + done, inChunk, 0};
        size_t ret = ZSTD_compressStream2(c, &out, &in, ZSTD_e_continue);
        if (ZSTD_isError(ret)) fail(std::string("zstd encode: ") + ZSTD_getErrorName(ret));
        done += in.pos;
    }
    ZSTD_inBuffer in{src, 0, 0};
    size_t need = 1;
    while (need != 0) {
        need = ZSTD_compressStream2(c, &out, &in, ZSTD_e_end);
        if (ZSTD_isError(need)) fail(std::string("zstd end: ") + ZSTD_getErrorName(need));
        if (need != 0 && out.pos == out.size) fail("zstd output overflow");
    }
    dst.resize(out.pos);
    ZSTD_freeCCtx(c);
}

void store_u32be(std::vector<uint8_t>& v, size_t pos, uint32_t x) {
    v[pos] = (uint8_t)(x >> 24);
    v[pos + 1] = (uint8_t)(x >> 16);
    v[pos + 2] = (uint8_t)(x >> 8);
    v[pos + 3] = (uint8_t)x;
}

void encode_record(const uint8_t* payload, size_t n, std::vector<uint8_t>& out_rec) {
    int R = region_count_for(n);
    uint32_t canonical_crc;
    std::vector<uint8_t> body;

    if (n == 0) {
        body.assign(PZS_HEAD, 0);
        store_u32be(body, 0, PZS_MAGIC);
        body[4] = 1;
        canonical_crc = crc32_buf(nullptr, 0);
    } else {
        std::vector<std::vector<uint8_t>> frames((size_t)R);
        std::vector<uint32_t> crcs((size_t)R);
        if (R == 1) {
            compress_region(payload, n, frames[0], crcs[0]);
        } else {
            uint64_t base = n / (uint64_t)R;
            uint64_t rem = n % (uint64_t)R;
            std::atomic<int> next{0};
            int nthreads = CL_THREADS < R ? CL_THREADS : R;
            if (nthreads > 4) nthreads = 4;
            auto work = [&]() {
                int k;
                while ((k = next.fetch_add(1)) < R) {
                    size_t pos = (size_t)(base * (uint64_t)k +
                                          (uint64_t)(k < (int)rem ? (uint64_t)k : (uint64_t)rem));
                    size_t len = (size_t)(base + (uint64_t)(k < (int)rem ? 1 : 0));
                    compress_region(payload + pos, len, frames[(size_t)k], crcs[(size_t)k]);
                }
            };
            int extra = nthreads - 1;
            std::vector<std::thread> th;
            th.reserve((size_t)extra);
            for (int t = 0; t < extra; t++) th.emplace_back(work);
            work();
            for (auto& t : th) t.join();
        }
        canonical_crc = crcs[0];
        {
            uint64_t base = n / (uint64_t)R;
            uint64_t rem = n % (uint64_t)R;
            for (int k = 1; k < R; k++) {
                uint64_t len = base + (uint64_t)(k < (int)rem ? 1 : 0);
                canonical_crc = crc32_combine(canonical_crc, crcs[(size_t)k], len);
            }
        }
        // body = PZS1 header + region table + frames
        size_t table = PZS_HEAD + 4ull * (uint64_t)R;
        size_t frames_len = 0;
        for (int k = 0; k < R; k++) frames_len += frames[(size_t)k].size();
        body.resize(table + frames_len);
        store_u32be(body, 0, PZS_MAGIC);
        body[4] = 1;
        body[5] = 0;
        body[6] = (uint8_t)(R >> 8);
        body[7] = (uint8_t)R;
        for (int i = 8; i < 16; i++) body[i] = (uint8_t)(n >> (8 * (15 - i)));
        size_t q = PZS_HEAD;
        for (int k = 0; k < R; k++) {
            store_u32be(body, q, (uint32_t)frames[(size_t)k].size());
            q += 4;
        }
        for (int k = 0; k < R; k++) {
            std::memcpy(body.data() + q, frames[(size_t)k].data(), frames[(size_t)k].size());
            q += frames[(size_t)k].size();
        }
    }

    bool stored = (n > 0 && body.size() >= n);
    if (stored) canonical_crc = crc32_buf(payload, n);

    out_rec.assign(ENV_SIZE + (stored ? n : body.size()), 0);
    store_u32be(out_rec, 0, ENV_MAGIC);
    out_rec[4] = 1;
    out_rec[5] = stored ? FLAG_STORED : 0;
    out_rec[6] = 0;
    out_rec[7] = 0;
    for (int i = 8; i < 16; i++) out_rec[i] = (uint8_t)(n >> (8 * (15 - i)));
    uint64_t bl = stored ? (uint64_t)n : (uint64_t)body.size();
    for (int i = 16; i < 24; i++) out_rec[i] = (uint8_t)(bl >> (8 * (23 - i)));
    if (stored)
        std::memcpy(out_rec.data() + ENV_SIZE, payload, n);
    else
        std::memcpy(out_rec.data() + ENV_SIZE, body.data(), body.size());
    // envelope CRC covers first 24 header bytes + payload
    std::vector<uint8_t> hp(out_rec.begin(), out_rec.begin() + 24);
    hp.insert(hp.end(), out_rec.begin() + ENV_SIZE, out_rec.end());
    store_u32be(out_rec, 24, crc32_buf(hp.data(), hp.size()));
    store_u32be(out_rec, 28, canonical_crc);
}

void decode_record(const uint8_t* rec, size_t rn, std::vector<uint8_t>& canonical) {
    if (rn < ENV_SIZE) fail("archive record too short");
    uint32_t magic = ((uint32_t)rec[0] << 24) | ((uint32_t)rec[1] << 16) |
                     ((uint32_t)rec[2] << 8) | rec[3];
    if (magic != ENV_MAGIC) fail("archive record magic");
    if (rec[4] != 1) fail("archive record version");
    uint8_t flags = rec[5];
    if (flags & (uint8_t)~FLAG_STORED) fail("unknown archive flags");
    if (((uint16_t)rec[6] << 8 | rec[7]) != 0) fail("reserved flags must be zero");
    uint64_t n = 0;
    for (int i = 8; i < 16; i++) n = (n << 8) | rec[i];
    uint64_t plen = 0;
    for (int i = 16; i < 24; i++) plen = (plen << 8) | rec[i];
    if (plen != rn - ENV_SIZE) fail("archive payload length mismatch");
    if (n > MAX_CANONICAL) fail("canonical size above card output limit");
    {
        uint32_t want = ((uint32_t)rec[24] << 24) | ((uint32_t)rec[25] << 16) |
                        ((uint32_t)rec[26] << 8) | rec[27];
        uint32_t got = crc32_buf(rec, 24);
        got = crc32_buf(rec + ENV_SIZE, rn - ENV_SIZE, got);
        if (got != want) fail("archive envelope CRC mismatch");
    }
    const uint8_t* body = rec + ENV_SIZE;
    size_t body_len = (size_t)plen;
    if (flags & FLAG_STORED) {
        if (body_len != n) fail("stored length mismatch");
        canonical.assign(body, body + body_len);
    } else {
        if (body_len < PZS_HEAD) fail("container too short");
        uint32_t pm = ((uint32_t)body[0] << 24) | ((uint32_t)body[1] << 16) |
                      ((uint32_t)body[2] << 8) | body[3];
        if (pm != PZS_MAGIC) fail("container magic");
        if (body[4] != 1) fail("container version");
        if (body[5] != 0) fail("unknown container flags");
        uint32_t R = ((uint32_t)body[6] << 8) | body[7];
        uint64_t orig = 0;
        for (int i = 8; i < 16; i++) orig = (orig << 8) | body[i];
        if (orig != n) fail("container original size mismatch");
        if (R > MAX_RECORDS) fail("region count too large");
        if (body_len < PZS_HEAD + 4ull * R) fail("region table truncated");
        uint64_t sum = 0;
        std::vector<uint32_t> flen(R);
        for (uint32_t i = 0; i < R; i++) {
            const uint8_t* q = body + PZS_HEAD + 4ull * i;
            flen[i] = ((uint32_t)q[0] << 24) | ((uint32_t)q[1] << 16) |
                      ((uint32_t)q[2] << 8) | q[3];
            sum += flen[i];
        }
        if (sum != body_len - PZS_HEAD - 4ull * R) fail("frame lengths do not tile container");
        if (R == 0 && n != 0) fail("empty container must map to empty canonical");
        if (R > 0 && n == 0) fail("empty canonical must have no frames");
        canonical.resize((size_t)n);
        ZSTD_DCtx* d = ZSTD_createDCtx();
        if (!d) fail("dctx allocation failed");
        size_t off = PZS_HEAD + 4ull * R;
        size_t out_off = 0;
        for (uint32_t i = 0; i < R; i++) {
            ZSTD_DCtx_reset(d, ZSTD_reset_session_and_parameters);
            ZSTD_DCtx_setParameter(d, ZSTD_d_windowLogMax, 27);
            ZSTD_inBuffer in{body + off, flen[i], 0};
            size_t lastRet = 1;
            while (in.pos < in.size || lastRet != 0) {
                ZSTD_outBuffer out{canonical.data() + out_off, n - out_off, 0};
                if (out.size == 0 && out.pos == 0) fail("decoded size mismatch (no room)");
                lastRet = ZSTD_decompressStream(d, &out, &in);
                if (ZSTD_isError(lastRet))
                    fail(std::string("zstd decode: ") + ZSTD_getErrorName(lastRet));
                out_off += out.pos;
                if (lastRet == 0) break;
            }
            if (in.pos != flen[i]) fail("frame did not consume declared bytes");
            off += flen[i];
        }
        if (out_off != n) fail("decoded size mismatch");
        ZSTD_freeDCtx(d);
    }
    uint32_t want = ((uint32_t)rec[28] << 24) | ((uint32_t)rec[29] << 16) |
                    ((uint32_t)rec[30] << 8) | rec[31];
    if (canonical.size() != n) fail("canonical buffer size mismatch");
    if (crc32_buf(canonical.data(), canonical.size()) != want) fail("canonical CRC mismatch");
}

// ---------- I/O helpers ----------
std::vector<uint8_t> read_all(FILE* f) {
    std::vector<uint8_t> buf;
    struct stat s;
    if (fstat(fileno(f), &s) == 0 && S_ISREG(s.st_mode) && s.st_size > 0)
        buf.reserve((size_t)s.st_size + 1);
    uint8_t tmp[1 << 22];
    size_t k;
    while ((k = std::fread(tmp, 1, sizeof tmp, f)) > 0) buf.insert(buf.end(), tmp, tmp + k);
    if (std::ferror(f)) fail("input read error");
    return buf;
}

void write_all(FILE* f, const uint8_t* p, size_t n) {
    if (n && std::fwrite(p, 1, n, f) != n) fail("output write error");
}

void load_file(const std::string& path, std::vector<uint8_t>& out) {
    FILE* f = std::fopen(path.c_str(), "rb");
    if (!f) fail("cannot open " + path);
    out = read_all(f);
    std::fclose(f);
}

// strict directory listing: exact file set {names.bin} U {i:08.bin}, no symlinks
std::vector<std::vector<uint8_t>> payload_store;  // keeps record payloads alive
std::vector<uint8_t> names_store;                 // keeps names.bin bytes (alias pointers) alive

void list_dir_records(const std::string& path, uint32_t expect_magic,
                      std::vector<Record>& records) {
    DIR* d = opendir(path.c_str());
    if (!d) fail("cannot open directory " + path);
    std::vector<std::string> names;
    struct dirent* e;
    while ((e = readdir(d)) != nullptr) {
        std::string nm(e->d_name);
        if (nm == "." || nm == "..") continue;
        struct stat st;
        if (lstat((path + "/" + nm).c_str(), &st) != 0) fail("lstat failed");
        if (!S_ISREG(st.st_mode)) fail("directory contains non-regular entry");
        names.push_back(nm);
    }
    closedir(d);
    names_store.clear();
    load_file(path + "/names.bin", names_store);
    std::vector<Record> name_records;
    parse_stream(names_store, expect_magic, name_records);
    size_t count = name_records.size();
    std::vector<std::string> expect;
    expect.reserve(count + 1);
    expect.push_back("names.bin");
    char idx[32];
    for (size_t i = 0; i < count; i++) {
        std::snprintf(idx, sizeof idx, "%08zu.bin", i);
        expect.push_back(idx);
    }
    std::sort(names.begin(), names.end());
    std::sort(expect.begin(), expect.end());
    if (names != expect) fail("directory file set mismatch");
    for (const Record& r : name_records)
        if (r.payload_len != 0) fail("names.bin payload must be empty");
    for (size_t i = 0; i < count; i++) {
        std::vector<uint8_t> payload;
        std::snprintf(idx, sizeof idx, "%08zu.bin", i);
        load_file(path + "/" + idx, payload);
        records.push_back(Record{name_records[i].alias, name_records[i].alias_len,
                                 payload.data(), payload.size()});
        payload_store.push_back(std::move(payload));
    }
}

void write_names_bin(const std::string& path, uint32_t magic,
                     const std::vector<Record>& records) {
    std::vector<uint8_t> b;
    b.reserve(9 + records.size() * 12);
    b.push_back((uint8_t)(magic >> 24));
    b.push_back((uint8_t)(magic >> 16));
    b.push_back((uint8_t)(magic >> 8));
    b.push_back((uint8_t)magic);
    b.push_back(1);
    b.push_back((uint8_t)(records.size() >> 24));
    b.push_back((uint8_t)(records.size() >> 16));
    b.push_back((uint8_t)(records.size() >> 8));
    b.push_back((uint8_t)records.size());
    for (const Record& r : records) {
        b.push_back((uint8_t)(r.alias_len >> 24));
        b.push_back((uint8_t)(r.alias_len >> 16));
        b.push_back((uint8_t)(r.alias_len >> 8));
        b.push_back((uint8_t)r.alias_len);
        b.insert(b.end(), r.alias, r.alias + r.alias_len);
        uint64_t zero = 0;
        for (int i = 7; i >= 0; i--) b.push_back((uint8_t)(zero >> (8 * i)));
    }
    FILE* f = std::fopen((path + "/names.bin").c_str(), "wb");
    if (!f) fail("cannot create names.bin");
    write_all(f, b.data(), b.size());
    if (std::fclose(f) != 0) fail("names.bin close failed");
}

void dump_file(const std::string& path, const std::vector<uint8_t>& data) {
    FILE* f = std::fopen(path.c_str(), "wb");
    if (!f) fail("cannot create " + path);
    write_all(f, data.data(), data.size());
    if (std::fclose(f) != 0) fail("close failed on " + path);
}

void dir_encode(const std::string& in_dir, const std::string& out_dir) {
    std::vector<Record> records;
    list_dir_records(in_dir, MAGIC_ORIG, records);
    {
        DIR* d = opendir(out_dir.c_str());
        if (!d) fail("cannot open output directory");
        struct dirent* e;
        while ((e = readdir(d)) != nullptr) {
            std::string nm(e->d_name);
            if (nm == "." || nm == "..") continue;
            fail("output directory is not empty");
        }
        closedir(d);
    }
    write_names_bin(out_dir, MAGIC_ARCH, records);
    std::vector<uint8_t> rec;
    char idx[32];
    for (size_t i = 0; i < records.size(); i++) {
        encode_record(records[i].payload, records[i].payload_len, rec);
        std::snprintf(idx, sizeof idx, "%08zu.bin", i);
        dump_file(out_dir + "/" + idx, rec);
        rec.clear();
    }
}

void dir_decode(const std::string& in_dir, const std::string& out_dir) {
    std::vector<Record> records;
    list_dir_records(in_dir, MAGIC_ARCH, records);
    {
        DIR* d = opendir(out_dir.c_str());
        if (!d) fail("cannot open output directory");
        struct dirent* e;
        while ((e = readdir(d)) != nullptr) {
            std::string nm(e->d_name);
            if (nm == "." || nm == "..") continue;
            fail("output directory is not empty");
        }
        closedir(d);
    }
    // Decode and validate every record before creating any output file.
    std::vector<std::vector<uint8_t>> canonicals(records.size());
    for (size_t i = 0; i < records.size(); i++)
        decode_record(records[i].payload, records[i].payload_len, canonicals[i]);
    write_names_bin(out_dir, MAGIC_ORIG, records);
    char idx[32];
    for (size_t i = 0; i < records.size(); i++) {
        std::snprintf(idx, sizeof idx, "%08zu.bin", i);
        dump_file(out_dir + "/" + idx, canonicals[i]);
    }
}

void stream_encode() {
    std::vector<uint8_t> in = read_all(stdin);
    std::vector<Record> records;
    parse_stream(in, MAGIC_ORIG, records);
    uint8_t head[9] = {(uint8_t)(MAGIC_ARCH >> 24), (uint8_t)(MAGIC_ARCH >> 16),
                       (uint8_t)(MAGIC_ARCH >> 8), (uint8_t)MAGIC_ARCH, 1, 0, 0, 0, 0};
    uint32_t cnt = (uint32_t)records.size();
    head[5] = (uint8_t)(cnt >> 24);
    head[6] = (uint8_t)(cnt >> 16);
    head[7] = (uint8_t)(cnt >> 8);
    head[8] = (uint8_t)cnt;
    write_all(stdout, head, 9);
    std::vector<uint8_t> rec;
    for (const Record& r : records) {
        encode_record(r.payload, r.payload_len, rec);
        uint8_t ah[4] = {(uint8_t)(r.alias_len >> 24), (uint8_t)(r.alias_len >> 16),
                         (uint8_t)(r.alias_len >> 8), (uint8_t)r.alias_len};
        write_all(stdout, ah, 4);
        write_all(stdout, r.alias, r.alias_len);
        uint64_t pl = rec.size();
        uint8_t ph[8];
        for (int i = 0; i < 8; i++) ph[i] = (uint8_t)(pl >> (8 * (7 - i)));
        write_all(stdout, ph, 8);
        write_all(stdout, rec.data(), rec.size());
        rec.clear();
        rec.shrink_to_fit();
    }
    if (fflush(stdout) != 0) fail("output flush error");
}

void stream_decode() {
    std::vector<uint8_t> in = read_all(stdin);
    std::vector<Record> records;
    parse_stream(in, MAGIC_ARCH, records);
    // Decode and validate every record before writing any output byte.
    std::vector<std::vector<uint8_t>> canonicals(records.size());
    for (size_t i = 0; i < records.size(); i++)
        decode_record(records[i].payload, records[i].payload_len, canonicals[i]);
    uint8_t head[9] = {(uint8_t)(MAGIC_ORIG >> 24), (uint8_t)(MAGIC_ORIG >> 16),
                       (uint8_t)(MAGIC_ORIG >> 8), (uint8_t)MAGIC_ORIG, 1, 0, 0, 0, 0};
    uint32_t cnt = (uint32_t)records.size();
    head[5] = (uint8_t)(cnt >> 24);
    head[6] = (uint8_t)(cnt >> 16);
    head[7] = (uint8_t)(cnt >> 8);
    head[8] = (uint8_t)cnt;
    write_all(stdout, head, 9);
    for (size_t i = 0; i < records.size(); i++) {
        const Record& r = records[i];
        const std::vector<uint8_t>& out = canonicals[i];
        uint8_t ah[4] = {(uint8_t)(r.alias_len >> 24), (uint8_t)(r.alias_len >> 16),
                         (uint8_t)(r.alias_len >> 8), (uint8_t)r.alias_len};
        write_all(stdout, ah, 4);
        write_all(stdout, r.alias, r.alias_len);
        uint64_t pl = out.size();
        uint8_t ph[8];
        for (int i2 = 0; i2 < 8; i2++) ph[i2] = (uint8_t)(pl >> (8 * (7 - i2)));
        write_all(stdout, ph, 8);
        write_all(stdout, out.data(), out.size());
    }
    if (fflush(stdout) != 0) fail("output flush error");
}

}  // namespace

#ifdef CL_DECODER
int main(int argc, char** argv) {
    if (argc == 2 && std::strcmp(argv[1], "decode-stream") == 0) stream_decode();
    else if (argc == 4 && std::strcmp(argv[1], "decode-dir") == 0) dir_decode(argv[2], argv[3]);
    else fail("unknown decoder invocation");
    return 0;
}
#else
int main(int argc, char** argv) {
    if (argc == 2 && std::strcmp(argv[1], "encode-stream") == 0) stream_encode();
    else if (argc == 4 && std::strcmp(argv[1], "encode-dir") == 0) dir_encode(argv[2], argv[3]);
    else fail("unknown encoder invocation");
    return 0;
}
#endif
