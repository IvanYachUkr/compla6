// SPDX-License-Identifier: MIT
// Yelp business JSONL columnizer: dataset-specialized whole-corpus codec.
// Mode 0 stores the byte-constant record template implicitly and encodes the
// 14 field columns with input-derived dictionaries plus zstd-12 streams.
// Modes 1/2 (zstd / literal) accept arbitrary payload bytes. The decoder
// rebuilds exact original bytes from the archive alone; no artifact leaves it.
#include <algorithm>
#include <array>
#include <cerrno>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <string_view>
#include <system_error>
#include <unordered_map>
#include <vector>
#include <fcntl.h>
#include <unistd.h>
#include <zstd.h>
#include "lab_crc.hpp"
#include "parallel_workers.hpp"

#ifndef YELP_DECODE_ONLY
#define YELP_DECODE_ONLY 0
#endif
#ifndef YELP_LEVEL
#define YELP_LEVEL 12
#endif
#ifndef YELP_GENERIC_LEVEL
#define YELP_GENERIC_LEVEL 9
#endif
#ifndef YELP_THREADS
#define YELP_THREADS 4
#endif
#ifndef YELP_MAX_BYTES
#define YELP_MAX_BYTES (uint64_t(8) << 30)
#endif
#ifndef YELP_MAX_LINES
#define YELP_MAX_LINES 20000000
#endif

static_assert(YELP_DECODE_ONLY == 0 || YELP_DECODE_ONLY == 1, "decode-only flag");
static_assert(YELP_THREADS >= 1 && YELP_THREADS <= 64, "thread bound");
static_assert(sizeof(std::size_t) >= 8, "64-bit target required");

namespace yc {
namespace fs = std::filesystem;
using Bytes = std::vector<uint8_t>;
using StrPair = std::pair<std::string, std::string>;
constexpr uint64_t kLimit = YELP_MAX_BYTES;
constexpr uint32_t kMaxAlias = 1048576, kMaxRecords = 1000000;
constexpr unsigned kStreamLevel = YELP_LEVEL, kGenericLevel = YELP_GENERIC_LEVEL;

enum StreamId : unsigned {
    S_BID = 0, S_NAME_P, S_NAME_L, S_ADDR_P, S_ADDR_L,
    S_CITY_T, S_CITY_I, S_STATE_T, S_STATE_I, S_ZIP_T, S_ZIP_I,
    S_LAT_IP, S_LAT_FL, S_LAT_FV, S_LON_IP, S_LON_FL, S_LON_FV, S_STARS_T, S_STARS_I,
    S_RC, S_OPEN, S_ATTR_N, S_ATTR_C, S_ATTR_I, S_ATTR_T,
    S_CAT_N, S_CAT_C, S_CAT_I, S_CAT_T,
    S_HOURS_N, S_HOURS_C, S_HOURS_I, S_HOURS_T,
    S_COUNT
};
enum Mode : uint8_t { M_YELP = 0, M_ZSTD = 1, M_STORED = 2 };
enum CodecId : uint8_t { C_STORE = 0, C_ZSTD = 1 };

void require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}
uint64_t add(uint64_t a, uint64_t b, uint64_t limit = kLimit) {
    require(a <= limit && b <= limit - a, "size limit/overflow");
    return a + b;
}
uint32_t get32(const uint8_t* p) {
    return (uint32_t(p[0]) << 24) | (uint32_t(p[1]) << 16) | (uint32_t(p[2]) << 8) | p[3];
}
uint64_t get64(const uint8_t* p) { return (uint64_t(get32(p)) << 32) | get32(p + 4); }
void set32(uint8_t* p, uint32_t n) {
    for (unsigned i = 0; i < 4; ++i) p[i] = uint8_t(n >> (24 - 8 * i));
}
void set64(uint8_t* p, uint64_t n) { set32(p, uint32_t(n >> 32)); set32(p + 4, uint32_t(n)); }

// ---------- varint (LEB128) ----------
void put_var(Bytes& out, uint64_t v) {
    while (v >= 0x80) {
        out.push_back(uint8_t(v) | 0x80u);
        v >>= 7;
    }
    out.push_back(uint8_t(v));
}
void put_u32(Bytes& out, uint32_t v) {
    for (unsigned i = 0; i < 4; ++i) out.push_back(uint8_t(v >> (24 - 8 * i)));
}
struct VarCursor {
    const uint8_t* p = nullptr;
    std::size_t size = 0, pos = 0;
    uint64_t var() {
        require(pos < size, "truncated varint");
        uint64_t v = 0;
        unsigned shift = 0;
        for (;;) {
            require(pos < size, "truncated varint");
            const uint8_t b = p[pos++];
            if (shift == 63 && b > 1) require(false, "varint overflow");
            v |= uint64_t(b & 0x7f) << shift;
            if (b < 0x80) return v;
            shift += 7;
            require(shift < 64, "varint overflow");
        }
    }
};

struct Slice { const uint8_t* data = nullptr; std::size_t size = 0; };
struct Reader {
    Slice input;
    std::size_t position = 0;
    const uint8_t* take(uint64_t size) {
        require(position <= input.size && size <= input.size - position, "truncated framing");
        const auto* result = input.data + position;
        position += std::size_t(size);
        return result;
    }
    uint32_t u32() { return get32(take(4)); }
    uint64_t u64() { return get64(take(8)); }
};
Slice view(const Bytes& b) { return {b.data(), b.size()}; }

struct StrViewHash {
    std::size_t operator()(std::string_view s) const {
        uint64_t h = 1469598103934665603ull;
        for (unsigned char c : s) { h ^= c; h *= 1099511628211ull; }
        return std::size_t(h);
    }
};
struct PairView {
    std::string_view key, value;
    bool operator==(const PairView& o) const { return key == o.key && value == o.value; }
};
struct PairViewHash {
    std::size_t operator()(const PairView& p) const {
        uint64_t h = 1469598103934665603ull;
        for (unsigned char c : p.key) { h ^= c; h *= 1099511628211ull; }
        h ^= 0x9e3779b97f4a7c15ull;
        for (unsigned char c : p.value) { h ^= c; h *= 1099511628211ull; }
        return std::size_t(h);
    }
};

bool valid_alias(const std::string& name) {
    if (name.empty() || name.size() > kMaxAlias) return false;
    bool need_letter = true;
    for (unsigned char c : name) {
        if ((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')) need_letter = false;
        else if (c == '-' && !need_letter) need_letter = true;
        else return false;
    }
    return !need_letter;
}
struct InputRecord { std::string name; Slice payload; };
struct OutputRecord { std::string name; Bytes payload; };

std::vector<InputRecord> unpack(Slice bytes, bool archive) {
    require(bytes.size <= kLimit && bytes.size >= 9, "invalid packed length");
    Reader reader{bytes};
    const auto* header = reader.take(5);
    require(std::memcmp(header, archive ? "HBA1" : "HBI1", 4) == 0 && header[4] == 1, "packed magic/version");
    const auto count = reader.u32();
    require(count > 0 && count <= kMaxRecords && uint64_t(count) * 13 <= bytes.size - 9, "invalid record count");
    std::vector<InputRecord> records;
    records.reserve(count);
    for (uint32_t i = 0; i < count; ++i) {
        const auto length = reader.u32();
        require(length > 0 && length <= kMaxAlias, "invalid alias length");
        const auto* name_bytes = reader.take(length);
        std::string name(reinterpret_cast<const char*>(name_bytes), length);
        require(valid_alias(name) && (records.empty() || records.back().name < name), "alias syntax/order");
        const auto length64 = reader.u64();
        const auto* payload = reader.take(length64);
        records.push_back({std::move(name), {payload, std::size_t(length64)}});
    }
    require(reader.position == bytes.size, "trailing packed bytes");
    return records;
}

Bytes read_stdin() {
    Bytes result;
    std::array<char, 65536> block{};
    for (;;) {
        std::cin.read(block.data(), std::streamsize(block.size()));
        const auto count = std::cin.gcount();
        require(count >= 0, "stdin read failure");
        (void)add(result.size(), uint64_t(count));
        result.insert(result.end(), block.data(), block.data() + count);
        if (std::cin.eof()) break;
        require(bool(std::cin), "stdin read failure");
    }
    return result;
}
Bytes read_file(const fs::path& path, uint64_t limit = kLimit) {
    require(fs::is_regular_file(fs::symlink_status(path)), "nonregular/symlink input");
    require(fs::hard_link_count(path) == 1, "hard-linked input");
    const auto size = fs::file_size(path);
    require(size <= limit, "file size limit");
    std::ifstream file(path, std::ios::binary);
    require(bool(file), "cannot open input");
    Bytes result(static_cast<std::size_t>(size));
    if (size) file.read(reinterpret_cast<char*>(result.data()), std::streamsize(size));
    require(bool(file) && file.peek() == std::char_traits<char>::eof() && !file.bad(), "changed/truncated input or read failure");
    return result;
}
std::string ordinal(std::size_t index) {
    auto number = std::to_string(index);
    require(number.size() <= 8, "ordinal overflow");
    return std::string(8 - number.size(), '0') + number + ".bin";
}
struct InputBundle {
    Bytes transport;
    std::vector<Bytes> files;
    std::vector<InputRecord> records;
    InputBundle() = default;
    InputBundle(const InputBundle&) = delete;
    InputBundle& operator=(const InputBundle&) = delete;
    InputBundle(InputBundle&&) = default;
    InputBundle& operator=(InputBundle&&) = default;
};
InputBundle input_bundle(const fs::path* directory, bool archive) {
    InputBundle bundle;
    if (!directory) {
        bundle.transport = read_stdin();
        bundle.records = unpack(view(bundle.transport), archive);
        return bundle;
    }
    require(fs::is_directory(fs::symlink_status(*directory)), "input is not a real directory");
    bundle.transport = read_file(*directory / "names.bin");
    bundle.records = unpack(view(bundle.transport), archive);
    for (const auto& record : bundle.records) require(record.payload.size == 0, "index payload must be empty");
    std::size_t entries = 0;
    for (const auto& entry : fs::directory_iterator(*directory)) {
        require(fs::is_regular_file(entry.symlink_status()) && fs::hard_link_count(entry.path()) == 1, "nonregular/hard-linked directory entry");
        ++entries;
        require(entries <= bundle.records.size() + 1, "extra directory entry");
    }
    require(entries == bundle.records.size() + 1, "directory inventory mismatch");
    for (std::size_t i = 0; i < bundle.records.size(); ++i) {
        const auto path = *directory / ordinal(i);
        require(fs::is_regular_file(fs::symlink_status(path)), "missing/nonregular ordinal");
    }
    bundle.files.reserve(bundle.records.size());
    for (std::size_t i = 0; i < bundle.records.size(); ++i) {
        bundle.files.push_back(read_file(*directory / ordinal(i)));
        bundle.records[i].payload = view(bundle.files.back());
    }
    return bundle;
}

std::size_t worker_budget() {
    const char* text = std::getenv("COMPRESSION_LAB_THREADS");
    if (!text) return YELP_THREADS;
    require(*text != '\0', "empty worker setting");
    unsigned value = 0;
    for (; *text; ++text) {
        require(*text >= '0' && *text <= '9', "invalid worker setting");
        value = std::min(unsigned(YELP_THREADS), value * 10 + unsigned(*text - '0'));
    }
    require(value > 0, "worker budget must be positive");
    return value;
}

Bytes zstd_compress(const uint8_t* data, std::size_t size, unsigned level) {
    if (size == 0) return {};
    const auto bound = ZSTD_compressBound(size);
    require(!ZSTD_isError(bound), "zstd bound");
    Bytes out(bound);
    const auto n = ZSTD_compress(out.data(), out.size(), data, size, int(level));
    require(!ZSTD_isError(n), "zstd compression failed");
    out.resize(std::size_t(n));
    return out;
}
void zstd_decompress(const uint8_t* data, std::size_t size, uint8_t* out, std::size_t raw) {
    require(raw > 0, "empty zstd frame");
    require(ZSTD_getFrameContentSize(data, size) == raw, "zstd frame content size");
    const auto n = ZSTD_decompress(out, raw, data, size);
    require(!ZSTD_isError(n) && std::size_t(n) == raw, "zstd decompression failed");
}

// ---------- column structures ----------
struct BitVec {
    Bytes bytes;
    std::size_t count = 0;
    void push(bool bit) {
        if (count % 8 == 0) bytes.push_back(0);
        if (bit) bytes[count / 8] |= uint8_t(1u << (count % 8));
        ++count;
    }
    bool get(std::size_t i) const {
        require(i < count, "bit index");
        return (bytes[i / 8] >> (i % 8)) & 1;
    }
};
struct Dict {
    std::unordered_map<std::string_view, uint32_t, StrViewHash> index;
    std::vector<std::string_view> vals;
    uint32_t get(std::string_view v) {
        auto it = index.find(v);
        if (it != index.end()) return it->second;
        const auto id = uint32_t(vals.size());
        require(id < 0xffffffffu, "dictionary overflow");
        index.emplace(v, id);
        vals.push_back(v);
        return id;
    }
};
struct PairDict {
    std::unordered_map<PairView, uint32_t, PairViewHash> index;
    std::vector<PairView> vals;
    uint32_t get(const PairView& p) {
        auto it = index.find(p);
        if (it != index.end()) return it->second;
        const auto id = uint32_t(vals.size());
        require(id < 0xffffffffu, "dictionary overflow");
        index.emplace(p, id);
        vals.push_back(p);
        return id;
    }
};

// ---------- record template ----------
// kSep[0] opens the record; kSep[i] (i>=1) is the separator BEFORE field i's
// value (its leading quote closes the previous string); kSep[14] closes.
constexpr std::string_view kSep[15] = {
    "{\"business_id\":\"",
    "\",\"name\":\"",
    ",\"address\":\"",
    ",\"city\":\"",
    ",\"state\":\"",
    ",\"postal_code\":\"",
    ",\"latitude\":",
    ",\"longitude\":",
    ",\"stars\":",
    ",\"review_count\":",
    ",\"is_open\":",
    ",\"attributes\":",
    ",\"categories\":",
    ",\"hours\":",
    "}"
};
constexpr char kIdChars[65] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
signed char id_value(unsigned char c) {
    if (c >= 'A' && c <= 'Z') return static_cast<signed char>(c - 'A');
    if (c >= 'a' && c <= 'z') return static_cast<signed char>(c - 'a' + 26);
    if (c >= '0' && c <= '9') return static_cast<signed char>(c - '0' + 52);
    if (c == '-') return 62;
    if (c == '_') return 63;
    return -1;
}

std::size_t string_end(std::string_view s, std::size_t begin, std::size_t limit) {
    std::size_t p = begin;
    while (p < limit) {
        if (s[p] == '\\') { p += 2; continue; }
        if (s[p] == '"') return p;
        ++p;
    }
    return std::string_view::npos;
}
// s[begin] must be '{'; returns one-past the matching '}' (strings respected).
std::size_t object_end(std::string_view s, std::size_t begin) {
    int depth = 0;
    bool instr = false;
    std::size_t i = begin;
    while (i < s.size()) {
        const char c = s[i];
        if (instr) {
            if (c == '\\') { i += 2; continue; }
            if (c == '"') instr = false;
        } else {
            if (c == '"') instr = true;
            else if (c == '{') ++depth;
            else if (c == '}') {
                --depth;
                if (depth == 0) return i + 1;
            }
        }
        ++i;
    }
    return std::string_view::npos;
}
// Strict coordinate grammar: [-]("0"|["1"-"9"]["0"-"9"]{0,2})"."digits{1,12}.
// Exact reconstruction: sign + ip + '.' + frac zero-padded to its original width.
constexpr unsigned kCoordPlanes = 6;  // frac value < 2^48
bool coord_token(std::string_view s, unsigned& flags, unsigned& ip, uint64_t& fval) {
    flags = 0;
    std::size_t i = 0;
    if (!s.empty() && s[0] == '-') {
        flags = 1;
        i = 1;
    }
    if (i >= s.size()) return false;
    if (s[i] == '0') {
        ip = 0;
        ++i;
        if (i < s.size() && s[i] >= '0' && s[i] <= '9') return false;  // leading zero
    } else {
        unsigned digits = 0;
        uint64_t v = 0;
        while (i < s.size() && s[i] >= '0' && s[i] <= '9') {
            v = v * 10 + unsigned(s[i] - '0');
            ++digits;
            ++i;
            if (digits > 3) return false;
        }
        if (digits == 0) return false;
        ip = unsigned(v);
    }
    if (i >= s.size() || s[i] != '.') return false;
    ++i;
    uint64_t fv = 0;
    unsigned flen = 0;
    while (i < s.size() && s[i] >= '0' && s[i] <= '9') {
        fv = fv * 10 + unsigned(s[i] - '0');
        ++flen;
        ++i;
        if (flen > 12) return false;
    }
    if (flen == 0 || i != s.size() || fv >= (uint64_t(1) << (8 * kCoordPlanes))) return false;
    flags |= flen << 1;
    fval = fv;
    return true;
}
bool number_token(std::string_view s) {
    if (s.empty() || s.size() > 32) return false;
    std::size_t i = 0;
    if (s[0] == '-') i = 1;
    bool digits = false, point = false, fraction = false;
    for (; i < s.size(); ++i) {
        const char c = s[i];
        if (c >= '0' && c <= '9') { if (point) fraction = true; else digits = true; continue; }
        if (c == '.' && !point) { point = true; continue; }
        return false;
    }
    return digits && fraction;
}
bool uint_token(std::string_view s, uint64_t& value) {
    if (s.empty() || s.size() > 20) return false;
    if (s[0] == '0' && s.size() > 1) return false;
    uint64_t v = 0;
    for (char c : s) {
        if (c < '0' || c > '9') return false;
        const auto d = unsigned(c - '0');
        if (v > (std::numeric_limits<uint64_t>::max() - d) / 10) return false;
        v = v * 10 + d;
    }
    value = v;
    return true;
}
// Parse {"k":"v",...} with string values only; exactly canonical shape.
struct PairSink {
    virtual void pair(std::string_view key, std::string_view value) = 0;
protected:
    ~PairSink() = default;
};
bool parse_object(std::string_view s, PairSink& sink) {
    if (s.size() < 2 || s.front() != '{' || s.back() != '}') return false;
    std::size_t i = 1;
    const std::size_t body_end = s.size() - 1;
    if (i == body_end) return true;  // {}
    for (;;) {
        if (i >= body_end || s[i] != '"') return false;
        ++i;
        std::size_t key_end = i;
        for (;;) {
            if (key_end >= body_end) return false;
            if (s[key_end] == '\\') { key_end += 2; continue; }
            if (s[key_end] == '"') break;
            ++key_end;
        }
        if (key_end >= body_end || s[key_end] != '"') return false;
        const std::string_view key = s.substr(i, key_end - i);
        i = key_end + 1;
        if (i >= body_end || s[i] != ':' || s[i + 1] != '"') return false;
        i += 2;
        const auto p = string_end(s, i, body_end);
        if (p == std::string_view::npos) return false;
        sink.pair(key, s.substr(i, p - i));
        i = p + 1;
        if (i == body_end) return true;
        if (i > body_end || s[i] != ',') return false;
        ++i;
    }
}

// ---------- writers shared by size counting and emission ----------
struct CountingWriter {
    std::size_t pos = 0;
    void raw(const void*, std::size_t n) { pos += n; }
    void sv(std::string_view s) { pos += s.size(); }
    void ch(char) { ++pos; }
};
struct Writer {
    uint8_t* out = nullptr;
    std::size_t cap = 0, pos = 0;
    void raw(const void* p, std::size_t n) {
        require(pos <= cap && n <= cap - pos, "output overflow");
        std::memcpy(out + pos, p, n);
        pos += n;
    }
    void sv(std::string_view s) { raw(s.data(), s.size()); }
    void ch(char c) { raw(&c, 1); }
};

// ---------- encode ----------
#if !YELP_DECODE_ONLY
struct Columns {
    Bytes bid;
    unsigned bid_bits = 0;
    uint64_t bid_acc = 0;
    BitVec open_bits, attr_null, cat_null, hours_null;
    Bytes name_p, addr_p;
    std::vector<uint32_t> name_l, addr_l;
    Bytes lat_ip, lat_fl, lat_fv, lon_ip, lon_fl, lon_fv;  // fv: 6 bytes per record
    Dict city, state, zip, stars, cat;
    PairDict attr, hours;
    std::vector<uint32_t> city_i, state_i, zip_i, stars_i, attr_c, attr_i, cat_c, cat_i, hours_c, hours_i;
    std::vector<uint64_t> rc;
    std::size_t record_count = 0;
    struct PairCollect : PairSink {
        PairDict& dict;
        std::vector<uint32_t>& ids;
        std::size_t begin;
        PairCollect(PairDict& d, std::vector<uint32_t>& i) : dict(d), ids(i), begin(i.size()) {}
        void pair(std::string_view key, std::string_view value) override {
            ids.push_back(dict.get(PairView{key, value}));
        }
    };
};

// Strictly match one record (no trailing newline); append column data.
bool parse_record(std::string_view line, Columns& cols) {
    std::size_t pos = 0;
    const auto match = [&](std::string_view sep) {
        if (line.size() - pos < sep.size() || line.compare(pos, sep.size(), sep) != 0) return false;
        pos += sep.size();
        return true;
    };
    // Separators kSep[0..5] already consume each value's opening quote; the
    // value body starts directly at pos and ends at the closing quote.
    const auto scan_quoted = [&](std::string_view& value) {
        const auto end = string_end(line, pos, line.size());
        if (end == std::string_view::npos) return false;
        value = line.substr(pos, end - pos);
        pos = end + 1;
        return true;
    };
    const auto scan_until_comma = [&](std::string_view& token) {
        std::size_t end = pos;
        while (end < line.size() && line[end] != ',') ++end;
        if (end == pos || end >= line.size()) return false;
        token = line.substr(pos, end - pos);
        pos = end;
        return true;
    };
    if (!match(kSep[0])) return false;
    if (line.size() - pos < 22) return false;
    for (unsigned i = 0; i < 22; ++i) {
        const auto v = id_value(static_cast<unsigned char>(line[pos + i]));
        if (v < 0) return false;
        cols.bid_acc = (cols.bid_acc << 6) | uint64_t(v);
        cols.bid_bits += 6;
        while (cols.bid_bits >= 8) {
            cols.bid_bits -= 8;
            cols.bid.push_back(uint8_t(cols.bid_acc >> cols.bid_bits));
        }
    }
    pos += 22;
    std::string_view value;
    if (!match(kSep[1]) || !scan_quoted(value)) return false;
    cols.name_l.push_back(uint32_t(value.size()));
    cols.name_p.insert(cols.name_p.end(), value.begin(), value.end());
    if (!match(kSep[2]) || !scan_quoted(value)) return false;
    cols.addr_l.push_back(uint32_t(value.size()));
    cols.addr_p.insert(cols.addr_p.end(), value.begin(), value.end());
    if (!match(kSep[3]) || !scan_quoted(value)) return false;
    cols.city_i.push_back(cols.city.get(value));
    if (!match(kSep[4]) || !scan_quoted(value)) return false;
    cols.state_i.push_back(cols.state.get(value));
    if (!match(kSep[5]) || !scan_quoted(value)) return false;
    cols.zip_i.push_back(cols.zip.get(value));
    if (!match(kSep[6]) || !scan_until_comma(value)) return false;
    {
        unsigned flags, ip;
        uint64_t fval;
        if (!coord_token(value, flags, ip, fval)) return false;
        cols.lat_ip.push_back(uint8_t(ip));
        cols.lat_fl.push_back(uint8_t(flags));
        cols.lat_fv.insert(cols.lat_fv.end(), reinterpret_cast<const uint8_t*>(&fval),
                           reinterpret_cast<const uint8_t*>(&fval) + kCoordPlanes);
        // little-endian bytes of the low 6 planes
    }
    if (!match(kSep[7]) || !scan_until_comma(value)) return false;
    {
        unsigned flags, ip;
        uint64_t fval;
        if (!coord_token(value, flags, ip, fval)) return false;
        cols.lon_ip.push_back(uint8_t(ip));
        cols.lon_fl.push_back(uint8_t(flags));
        cols.lon_fv.insert(cols.lon_fv.end(), reinterpret_cast<const uint8_t*>(&fval),
                           reinterpret_cast<const uint8_t*>(&fval) + kCoordPlanes);
    }
    if (!match(kSep[8]) || !scan_until_comma(value) || !number_token(value)) return false;
    cols.stars_i.push_back(cols.stars.get(value));
    if (!match(kSep[9]) || !scan_until_comma(value)) return false;
    uint64_t rc_value;
    if (!uint_token(value, rc_value)) return false;
    cols.rc.push_back(rc_value);
    if (!match(kSep[10])) return false;
    if (pos >= line.size() || (line[pos] != '0' && line[pos] != '1')) return false;
    cols.open_bits.push(line[pos] == '1');
    ++pos;
    if (!match(kSep[11])) return false;
    if (line.size() - pos >= 4 && line.compare(pos, 4, "null") == 0) {
        pos += 4;
        cols.attr_null.push(true);
        cols.attr_c.push_back(0);
    } else {
        if (pos >= line.size() || line[pos] != '{') return false;
        const auto end = object_end(line, pos);
        if (end == std::string_view::npos) return false;
        Columns::PairCollect collect{cols.attr, cols.attr_i};
        if (!parse_object(line.substr(pos, end - pos), collect)) return false;
        cols.attr_null.push(false);
        cols.attr_c.push_back(uint32_t(cols.attr_i.size() - collect.begin));
        pos = end;
    }
    if (!match(kSep[12])) return false;
    if (line.size() - pos >= 4 && line.compare(pos, 4, "null") == 0) {
        pos += 4;
        cols.cat_null.push(true);
        cols.cat_c.push_back(0);
    } else {
        const std::size_t begin = cols.cat_i.size();
        if (pos >= line.size() || line[pos] != '"') return false;
        ++pos;
        if (!scan_quoted(value)) return false;
        std::size_t start = 0;
        for (;;) {
            const auto comma = value.find(", ", start);
            if (comma == std::string_view::npos) {
                cols.cat_i.push_back(cols.cat.get(value.substr(start)));
                break;
            }
            cols.cat_i.push_back(cols.cat.get(value.substr(start, comma - start)));
            start = comma + 2;
        }
        cols.cat_null.push(false);
        cols.cat_c.push_back(uint32_t(cols.cat_i.size() - begin));
    }
    if (!match(kSep[13])) return false;
    if (line.size() - pos >= 4 && line.compare(pos, 4, "null") == 0) {
        pos += 4;
        cols.hours_null.push(true);
        cols.hours_c.push_back(0);
    } else {
        if (pos >= line.size() || line[pos] != '{') return false;
        const auto end = object_end(line, pos);
        if (end == std::string_view::npos) return false;
        Columns::PairCollect collect{cols.hours, cols.hours_i};
        if (!parse_object(line.substr(pos, end - pos), collect)) return false;
        cols.hours_null.push(false);
        cols.hours_c.push_back(uint32_t(cols.hours_i.size() - collect.begin));
        pos = end;
    }
    if (!match(kSep[14]) || pos != line.size()) return false;
    ++cols.record_count;
    return true;
}

void append_varints(Bytes& out, const std::vector<uint32_t>& vals) {
    for (const auto v : vals) put_var(out, v);
}
void append_varints64(Bytes& out, const std::vector<uint64_t>& vals) {
    for (const auto v : vals) put_var(out, v);
}
void append_table(Bytes& out, const std::vector<std::string_view>& vals) {
    for (const auto v : vals) {
        put_var(out, v.size());
        out.insert(out.end(), v.begin(), v.end());
    }
}
void append_pair_table(Bytes& out, const std::vector<PairView>& vals) {
    for (const auto& p : vals) {
        put_var(out, p.key.size());
        out.insert(out.end(), p.key.begin(), p.key.end());
        put_var(out, p.value.size());
        out.insert(out.end(), p.value.begin(), p.value.end());
    }
}

// n x W row-major bytes -> W planes of n bytes each.
void transpose_planes(const Bytes& rowmajor, Bytes& out) {
    require(rowmajor.size() % kCoordPlanes == 0, "plane rows");
    const std::size_t n = rowmajor.size() / kCoordPlanes;
    out.resize(rowmajor.size());
    for (unsigned p = 0; p < kCoordPlanes; ++p)
        for (std::size_t i = 0; i < n; ++i)
            out[p * n + i] = rowmajor[i * kCoordPlanes + p];
}

// Build the mode-0 archive from parsed columns. Fails (returns false) only on
// resource-bound violations; byte-level deviations never reach this point.
bool build_yelp(Columns& cols, Bytes& out) {
    std::array<Bytes, S_COUNT> st;
    st[S_BID] = std::move(cols.bid);
    if (cols.bid_bits > 0) st[S_BID].push_back(uint8_t(cols.bid_acc << (8 - cols.bid_bits)));
    st[S_NAME_P] = std::move(cols.name_p);
    append_varints(st[S_NAME_L], cols.name_l);
    st[S_ADDR_P] = std::move(cols.addr_p);
    append_varints(st[S_ADDR_L], cols.addr_l);
    append_table(st[S_CITY_T], cols.city.vals);
    append_varints(st[S_CITY_I], cols.city_i);
    append_table(st[S_STATE_T], cols.state.vals);
    append_varints(st[S_STATE_I], cols.state_i);
    append_table(st[S_ZIP_T], cols.zip.vals);
    append_varints(st[S_ZIP_I], cols.zip_i);
    st[S_LAT_IP] = std::move(cols.lat_ip);
    st[S_LAT_FL] = std::move(cols.lat_fl);
    transpose_planes(cols.lat_fv, st[S_LAT_FV]);
    st[S_LON_IP] = std::move(cols.lon_ip);
    st[S_LON_FL] = std::move(cols.lon_fl);
    transpose_planes(cols.lon_fv, st[S_LON_FV]);
    append_table(st[S_STARS_T], cols.stars.vals);
    append_varints(st[S_STARS_I], cols.stars_i);
    append_varints64(st[S_RC], cols.rc);
    st[S_OPEN] = cols.open_bits.bytes;
    st[S_ATTR_N] = cols.attr_null.bytes;
    append_varints(st[S_ATTR_C], cols.attr_c);
    append_varints(st[S_ATTR_I], cols.attr_i);
    append_pair_table(st[S_ATTR_T], cols.attr.vals);
    st[S_CAT_N] = cols.cat_null.bytes;
    append_varints(st[S_CAT_C], cols.cat_c);
    append_varints(st[S_CAT_I], cols.cat_i);
    append_table(st[S_CAT_T], cols.cat.vals);
    st[S_HOURS_N] = cols.hours_null.bytes;
    append_varints(st[S_HOURS_C], cols.hours_c);
    append_varints(st[S_HOURS_I], cols.hours_i);
    append_pair_table(st[S_HOURS_T], cols.hours.vals);

    std::array<Bytes, S_COUNT> enc;
    std::array<uint8_t, S_COUNT> codec{};
    std::array<uint32_t, S_COUNT> crc_raw{}, crc_blob{};
    {
        const auto budget = std::min(worker_budget(), std::size_t(S_COUNT));
        cl_parallel::Workers pool(budget);
        std::vector<std::unique_ptr<ZSTD_CCtx, decltype(&ZSTD_freeCCtx)>> ctxs;
        std::vector<Bytes> scratch(budget);
        for (std::size_t i = 0; i < budget; ++i) {
            ctxs.emplace_back(ZSTD_createCCtx(), ZSTD_freeCCtx);
            require(bool(ctxs.back()), "zstd context");
            require(!ZSTD_isError(ZSTD_CCtx_setParameter(ctxs[i].get(), ZSTD_c_compressionLevel, int(kStreamLevel))), "zstd level");
            require(!ZSTD_isError(ZSTD_CCtx_setParameter(ctxs[i].get(), ZSTD_c_nbWorkers, 0)), "zstd workers");
        }
        pool.run(S_COUNT, [&](std::size_t i, std::size_t worker) {
            const auto& data = st[i];
            crc_raw[i] = lab::crc32(data.data(), data.size());
            if (data.empty()) {
                codec[i] = C_STORE;
                crc_blob[i] = crc_raw[i];
                return;
            }
            const auto bound = ZSTD_compressBound(data.size());
            require(!ZSTD_isError(bound), "zstd bound");
            scratch[worker].resize(bound);
            const auto n = ZSTD_compress2(ctxs[worker].get(), scratch[worker].data(),
                                          scratch[worker].size(), data.data(), data.size());
            require(!ZSTD_isError(n), "zstd compression failed");
            if (std::size_t(n) < data.size()) {
                codec[i] = C_ZSTD;
                enc[i].assign(scratch[worker].data(), scratch[worker].data() + n);
            } else {
                codec[i] = C_STORE;
                enc[i] = data;
            }
            crc_blob[i] = lab::crc32(enc[i].data(), enc[i].size());
        });
    }

    out.clear();
    out.reserve(S_COUNT * 24 + 32);
    out.insert(out.end(), {'Y', 'C', 'L', '1', 1, M_YELP});
    put_var(out, cols.record_count);
    uint64_t raw_total = 0, enc_total = 0;
    for (unsigned i = 0; i < S_COUNT; ++i) {
        out.push_back(codec[i]);
        put_var(out, st[i].size());
        put_var(out, enc[i].size());
        put_u32(out, crc_raw[i]);
        put_u32(out, crc_blob[i]);
        raw_total = add(raw_total, st[i].size());
        enc_total = add(enc_total, enc[i].size());
    }
    put_u32(out, lab::crc32(out.data(), out.size()));
    const auto header_size = out.size();
    out.resize(std::size_t(add(header_size, enc_total)));
    std::size_t pos = header_size;
    for (unsigned i = 0; i < S_COUNT; ++i) {
        std::memcpy(out.data() + pos, enc[i].data(), enc[i].size());
        pos += enc[i].size();
    }
    return true;
}

// Specialized attempt: strict whole-payload template scan.
bool encode_yelp(Slice payload, Bytes& out) {
    if (payload.size < 18 || payload.data[payload.size - 1] != '\n') return false;
    Columns cols;
    const auto* base = reinterpret_cast<const char*>(payload.data);
    std::size_t pos = 0;
    while (pos < payload.size) {
        const void* nl = std::memchr(base + pos, '\n', payload.size - pos);
        if (!nl) return false;
        const auto end = std::size_t(static_cast<const char*>(nl) - base);
        if (!parse_record(std::string_view(base + pos, end - pos), cols)) return false;
        if (cols.record_count > YELP_MAX_LINES) return false;
        pos = end + 1;
    }
    return build_yelp(cols, out);
}
#endif  // !YELP_DECODE_ONLY

// ---------- decode: rebuilder shared by size-count and emission passes ----------
std::string_view as_sv(const Bytes& b) {
    return {reinterpret_cast<const char*>(b.data()), b.size()};
}
struct BitReader {
    const uint8_t* p = nullptr;
    std::size_t size = 0, pos = 0;
    unsigned nb = 0;
    uint64_t acc = 0;
    unsigned get(unsigned n) {
        while (nb < n) {
            require(pos < size, "bit stream exhausted");
            acc = (acc << 8) | p[pos++];
            nb += 8;
        }
        nb -= n;
        return unsigned((acc >> nb) & ((1u << n) - 1));
    }
};
struct Rebuilder {
    const std::vector<StrPair>& attr;
    const std::vector<StrPair>& hours;
    const std::vector<std::string>& cat;
    const std::vector<std::string>& city_t;
    const std::vector<std::string>& state_t;
    const std::vector<std::string>& zip_t;
    const std::vector<std::string>& stars_t;
    std::string_view name_c, addr_c;
    const uint8_t *lat_ip = nullptr, *lat_fl = nullptr, *lat_fv = nullptr;
    const uint8_t *lon_ip = nullptr, *lon_fl = nullptr, *lon_fv = nullptr;
    std::size_t geo_n = 0;
    BitReader bid;
    std::string_view open_bits, attr_null, cat_null, hours_null;
    VarCursor name_l, addr_l, city_i, state_i, zip_i, stars_i;
    VarCursor rc, attr_c, attr_i, cat_c, cat_i, hours_c, hours_i;
    char id_buf[22];

    static bool bit_at(std::string_view bits, std::size_t i) {
        require(i < bits.size() * 8, "bit index");
        return (bits[i / 8] >> (i % 8)) & 1;
    }
    std::string_view take_payload(std::string_view& col, VarCursor& lens) {
        const auto len = lens.var();
        require(len <= col.size(), "column overrun");
        std::string_view result(col.data(), std::size_t(len));
        col.remove_prefix(std::size_t(len));
        return result;
    }
    std::string_view take_dict(const std::vector<std::string>& table, VarCursor& ids) {
        const auto id = ids.var();
        require(id < table.size(), "dictionary id out of range");
        return table[id];
    }
    // Exact decimal text from (sign, int part, frac width, transposed frac value).
    std::string_view geo_text(const uint8_t* ipb, const uint8_t* flb, const uint8_t* fvb, std::size_t index) {
        require(index < geo_n, "geo index");
        char* buf = geo_buf[geo_slot];
        geo_slot ^= 1u;
        unsigned len = 0;
        const unsigned flags = flb[index];
        if (flags & 1) buf[len++] = '-';
        const unsigned ip = ipb[index];
        if (ip >= 100) buf[len++] = char('0' + ip / 100);
        if (ip >= 10) buf[len++] = char('0' + (ip / 10) % 10);
        buf[len++] = char('0' + ip % 10);
        buf[len++] = '.';
        uint64_t fv = 0;
        for (unsigned p = 0; p < kCoordPlanes; ++p)
            fv |= uint64_t(fvb[p * geo_n + index]) << (8 * p);
        const unsigned flen = flags >> 1;
        require(flen >= 1 && flen <= 12, "frac width");
        for (unsigned j = 0; j < flen; ++j) {
            buf[len + flen - 1 - j] = char('0' + fv % 10);
            fv /= 10;
        }
        require(fv == 0, "frac digits exceed width");
        len += flen;
        return std::string_view(buf, len);
    }
    char geo_buf[2][24];
    unsigned geo_slot = 0;
    template <class W>
    void record(W& w, std::size_t index) {
        for (unsigned i = 0; i < 22; ++i) id_buf[i] = kIdChars[bid.get(6)];
        const auto name = take_payload(name_c, name_l);
        const auto addr = take_payload(addr_c, addr_l);
        const auto city = take_dict(city_t, city_i);
        const auto state = take_dict(state_t, state_i);
        const auto zip = take_dict(zip_t, zip_i);
        const auto lat = geo_text(lat_ip, lat_fl, lat_fv, index);
        const auto lon = geo_text(lon_ip, lon_fl, lon_fv, index);
        const auto stars = take_dict(stars_t, stars_i);
        const auto rc_value = rc.var();
        char rc_buf[20];
        unsigned rc_n = 0;
        {
            uint64_t v = rc_value;
            do {
                rc_buf[19 - rc_n++] = char('0' + v % 10);
                v /= 10;
            } while (v != 0);
        }
        w.sv(kSep[0]);
        w.sv(std::string_view(id_buf, 22));
        w.sv(kSep[1]);  // opens with bid's closing quote
        w.sv(name);
        w.ch('"');
        w.sv(kSep[2]);
        w.sv(addr);
        w.ch('"');
        w.sv(kSep[3]);
        w.sv(city);
        w.ch('"');
        w.sv(kSep[4]);
        w.sv(state);
        w.ch('"');
        w.sv(kSep[5]);
        w.sv(zip);
        w.ch('"');
        w.sv(kSep[6]);
        w.sv(lat);
        w.sv(kSep[7]);
        w.sv(lon);
        w.sv(kSep[8]);
        w.sv(stars);
        w.sv(kSep[9]);
        w.sv(std::string_view(rc_buf + 20 - rc_n, rc_n));
        w.sv(kSep[10]);
        w.ch(bit_at(open_bits, index) ? '1' : '0');
        w.sv(kSep[11]);
        const auto attr_count = attr_c.var();
        if (bit_at(attr_null, index)) {
            require(attr_count == 0, "null attributes with pairs");
            w.sv("null");
        } else {
            w.ch('{');
            for (uint64_t i = 0; i < attr_count; ++i) {
                if (i) w.ch(',');
                const auto id = attr_i.var();
                require(id < attr.size(), "pair id out of range");
                w.ch('"');
                w.sv(attr[id].first);
                w.sv("\":\"");
                w.sv(attr[id].second);
                w.ch('"');
            }
            w.ch('}');
        }
        w.sv(kSep[12]);
        const auto cat_count = cat_c.var();
        if (bit_at(cat_null, index)) {
            require(cat_count == 0, "null categories with tokens");
            w.sv("null");
        } else {
            w.ch('"');
            for (uint64_t i = 0; i < cat_count; ++i) {
                if (i) w.sv(", ");
                const auto id = cat_i.var();
                require(id < cat.size(), "token id out of range");
                w.sv(cat[id]);
            }
            w.ch('"');
        }
        w.sv(kSep[13]);
        const auto hours_count = hours_c.var();
        if (bit_at(hours_null, index)) {
            require(hours_count == 0, "null hours with pairs");
            w.sv("null");
        } else {
            w.ch('{');
            for (uint64_t i = 0; i < hours_count; ++i) {
                if (i) w.ch(',');
                const auto id = hours_i.var();
                require(id < hours.size(), "pair id out of range");
                w.ch('"');
                w.sv(hours[id].first);
                w.sv("\":\"");
                w.sv(hours[id].second);
                w.ch('"');
            }
            w.ch('}');
        }
        w.sv(kSep[14]);
        w.ch('\n');
    }
};

std::vector<std::string> load_table(const uint8_t* p, std::size_t n) {
    std::vector<std::string> table;
    VarCursor c{p, n, 0};
    while (c.pos < n) {
        const auto len = c.var();
        require(len <= n - c.pos, "table overrun");
        table.emplace_back(reinterpret_cast<const char*>(p + c.pos), std::size_t(len));
        c.pos += std::size_t(len);
    }
    return table;
}
std::vector<StrPair> load_pair_table(const uint8_t* p, std::size_t n) {
    std::vector<StrPair> table;
    VarCursor c{p, n, 0};
    while (c.pos < n) {
        const auto klen = c.var();
        require(klen <= n - c.pos, "pair table overrun");
        const auto kpos = c.pos;
        c.pos += std::size_t(klen);
        const auto vlen = c.var();
        require(vlen <= n - c.pos, "pair table overrun");
        const auto vpos = c.pos;
        c.pos += std::size_t(vlen);
        table.emplace_back(
            std::string(reinterpret_cast<const char*>(p + kpos), std::size_t(klen)),
            std::string(reinterpret_cast<const char*>(p + vpos), std::size_t(vlen)));
    }
    return table;
}

Bytes decode_yelp(Slice archive, std::size_t body_begin) {
    VarCursor head{archive.data, archive.size, body_begin};
    const auto rec_count = head.var();
    require(rec_count >= 1 && rec_count <= YELP_MAX_LINES, "record count");
    struct Entry {
        uint8_t codec;
        std::size_t rawlen, clen;
        uint32_t crc_raw, crc_blob;
    };
    std::array<Entry, S_COUNT> entries{};
    uint64_t raw_total = 0, enc_total = 0;
    for (unsigned i = 0; i < S_COUNT; ++i) {
        auto& e = entries[i];
        e.codec = archive.data[head.pos++];
        require(e.codec == C_STORE || e.codec == C_ZSTD, "stream codec");
        const auto raw = head.var();
        const auto clen = head.var();
        require(raw <= kLimit && clen <= raw, "stream lengths");
        e.rawlen = std::size_t(raw);
        e.clen = std::size_t(clen);
        if (e.codec == C_STORE) require(e.clen == e.rawlen, "stored length");
        else require(e.rawlen == 0 || e.clen < e.rawlen, "compressed length");
        e.crc_raw = get32(archive.data + head.pos);
        head.pos += 4;
        e.crc_blob = get32(archive.data + head.pos);
        head.pos += 4;
        raw_total = add(raw_total, raw);
        enc_total = add(enc_total, clen);
    }
    require(lab::crc32(archive.data, head.pos) == get32(archive.data + head.pos), "header checksum");
    head.pos += 4;
    require(add(head.pos, enc_total) == archive.size, "blob total");
    // Guard against corrupted length claims before any large allocation.
    require(raw_total <= add(uint64_t(archive.size) * 64, uint64_t(1) << 20), "implausible raw total");
    require(raw_total <= kLimit, "raw total limit");
    std::array<std::size_t, S_COUNT> offsets{};
    std::size_t walk = head.pos;
    for (unsigned i = 0; i < S_COUNT; ++i) {
        offsets[i] = walk;
        walk += entries[i].clen;
    }
    std::array<Bytes, S_COUNT> st;
    for (unsigned i = 0; i < S_COUNT; ++i) st[i].resize(entries[i].rawlen);
    {
        const auto budget = std::min(worker_budget(), std::size_t(S_COUNT));
        cl_parallel::Workers pool(budget);
        pool.run(S_COUNT, [&](std::size_t i, std::size_t) {
            const auto& e = entries[i];
            const auto* blob = archive.data + offsets[i];
            require(lab::crc32(blob, e.clen) == e.crc_blob, "stream blob checksum");
            if (e.rawlen == 0) return;
            if (e.codec == C_STORE) {
                std::memcpy(st[i].data(), blob, e.rawlen);
            } else {
                zstd_decompress(blob, e.clen, st[i].data(), e.rawlen);
            }
            require(lab::crc32(st[i].data(), e.rawlen) == e.crc_raw, "stream raw checksum");
        });
    }
    const std::size_t bitmap_bytes = (std::size_t(rec_count) + 7) / 8;
    require(st[S_OPEN].size() == bitmap_bytes && st[S_ATTR_N].size() == bitmap_bytes &&
                st[S_CAT_N].size() == bitmap_bytes && st[S_HOURS_N].size() == bitmap_bytes,
            "bitmap sizes");
    require(st[S_BID].size() == (std::size_t(rec_count) * 132 + 7) / 8, "id stream size");
    require(st[S_LAT_IP].size() == rec_count && st[S_LAT_FL].size() == rec_count &&
                st[S_LON_IP].size() == rec_count && st[S_LON_FL].size() == rec_count &&
                st[S_LAT_FV].size() == std::size_t(rec_count) * kCoordPlanes &&
                st[S_LON_FV].size() == std::size_t(rec_count) * kCoordPlanes,
            "geo stream sizes");
    const auto tables_cat = load_table(st[S_CAT_T].data(), st[S_CAT_T].size());
    const auto tables_attr = load_pair_table(st[S_ATTR_T].data(), st[S_ATTR_T].size());
    const auto tables_hours = load_pair_table(st[S_HOURS_T].data(), st[S_HOURS_T].size());
    const auto tables_city = load_table(st[S_CITY_T].data(), st[S_CITY_T].size());
    const auto tables_state = load_table(st[S_STATE_T].data(), st[S_STATE_T].size());
    const auto tables_zip = load_table(st[S_ZIP_T].data(), st[S_ZIP_T].size());
    const auto tables_stars = load_table(st[S_STARS_T].data(), st[S_STARS_T].size());
    const auto build = [&] {
        return Rebuilder{
            tables_attr, tables_hours, tables_cat, tables_city, tables_state, tables_zip,
            tables_stars,
            as_sv(st[S_NAME_P]), as_sv(st[S_ADDR_P]),
            st[S_LAT_IP].data(), st[S_LAT_FL].data(), st[S_LAT_FV].data(),
            st[S_LON_IP].data(), st[S_LON_FL].data(), st[S_LON_FV].data(),
            std::size_t(rec_count),
            {st[S_BID].data(), st[S_BID].size(), 0, 0, uint64_t(0)},
            as_sv(st[S_OPEN]), as_sv(st[S_ATTR_N]), as_sv(st[S_CAT_N]), as_sv(st[S_HOURS_N]),
            {st[S_NAME_L].data(), st[S_NAME_L].size(), 0},
            {st[S_ADDR_L].data(), st[S_ADDR_L].size(), 0},
            {st[S_CITY_I].data(), st[S_CITY_I].size(), 0},
            {st[S_STATE_I].data(), st[S_STATE_I].size(), 0},
            {st[S_ZIP_I].data(), st[S_ZIP_I].size(), 0},
            {st[S_STARS_I].data(), st[S_STARS_I].size(), 0},
            {st[S_RC].data(), st[S_RC].size(), 0},
            {st[S_ATTR_C].data(), st[S_ATTR_C].size(), 0},
            {st[S_ATTR_I].data(), st[S_ATTR_I].size(), 0},
            {st[S_CAT_C].data(), st[S_CAT_C].size(), 0},
            {st[S_CAT_I].data(), st[S_CAT_I].size(), 0},
            {st[S_HOURS_C].data(), st[S_HOURS_C].size(), 0},
            {st[S_HOURS_I].data(), st[S_HOURS_I].size(), 0},
            {}};
    };
    // Pass 1 measures through the shared emission function; pass 2 writes.
    std::size_t total = 0;
    {
        auto rb = build();
        CountingWriter cw;
        for (std::size_t i = 0; i < std::size_t(rec_count); ++i) rb.record(cw, i);
        total = cw.pos;
    }
    require(total <= kLimit, "output limit");
    Bytes out(total);
    {
        auto rb = build();
        Writer w{out.data(), out.size(), 0};
        for (std::size_t i = 0; i < std::size_t(rec_count); ++i) rb.record(w, i);
        require(w.pos == out.size(), "output size mismatch");
        // Every stream must be consumed exactly: catches desynchronized framing.
        require(rb.bid.pos == rb.bid.size, "id stream tail");
        require(rb.name_c.empty() && rb.addr_c.empty(), "payload column tail");
        require(rb.name_l.pos == st[S_NAME_L].size(), "tail name_l");
        require(rb.addr_l.pos == st[S_ADDR_L].size(), "tail addr_l");
        require(rb.city_i.pos == st[S_CITY_I].size(), "tail city_i");
        require(rb.state_i.pos == st[S_STATE_I].size(), "tail state_i");
        require(rb.zip_i.pos == st[S_ZIP_I].size(), "tail zip_i");
        require(rb.stars_i.pos == st[S_STARS_I].size(), "tail stars_i");
        require(rb.rc.pos == st[S_RC].size(), "tail rc");
        require(rb.attr_c.pos == st[S_ATTR_C].size(), "tail attr_c");
        require(rb.attr_i.pos == st[S_ATTR_I].size(), "tail attr_i");
        require(rb.cat_c.pos == st[S_CAT_C].size(), "tail cat_c");
        require(rb.cat_i.pos == st[S_CAT_I].size(), "tail cat_i");
        require(rb.hours_c.pos == st[S_HOURS_C].size(), "tail hours_c");
        require(rb.hours_i.pos == st[S_HOURS_I].size(), "tail hours_i");
    }
    return out;
}

Bytes decode_payload(Slice archive) {
    require(archive.size >= 8, "archive too small");
    require(std::memcmp(archive.data, "YCL1", 4) == 0 && archive.data[4] == 1, "archive magic/version");
    const auto mode = archive.data[5];
    if (mode == M_YELP) return decode_yelp(archive, 6);
    require(mode == M_ZSTD || mode == M_STORED, "archive mode");
    VarCursor head{archive.data, archive.size, 6};
    const auto rawlen = head.var();
    require(rawlen <= kLimit, "raw length");
    const auto crc_raw = get32(archive.data + head.pos);
    head.pos += 4;
    if (mode == M_ZSTD) {
        const auto codec = archive.data[head.pos++];
        require(codec == C_ZSTD, "generic codec");
        const auto clen = head.var();
        require(clen > 0 && clen < rawlen, "generic lengths");
        const auto crc_blob = get32(archive.data + head.pos);
        head.pos += 4;
        require(lab::crc32(archive.data, head.pos) == get32(archive.data + head.pos), "header checksum");
        head.pos += 4;
        require(add(head.pos, clen) == archive.size, "generic blob total");
        const auto* blob = archive.data + head.pos;
        require(lab::crc32(blob, std::size_t(clen)) == crc_blob, "generic blob checksum");
        const auto out_len = std::size_t(rawlen);
    Bytes out(out_len);
        zstd_decompress(blob, std::size_t(clen), out.data(), std::size_t(rawlen));
        require(lab::crc32(out.data(), out.size()) == crc_raw, "generic raw checksum");
        return out;
    }
    require(lab::crc32(archive.data, head.pos) == get32(archive.data + head.pos), "header checksum");
    head.pos += 4;
    require(add(head.pos, rawlen) == archive.size, "stored blob total");
    const auto out_len = std::size_t(rawlen);
    Bytes out(out_len);
    if (rawlen) std::memcpy(out.data(), archive.data + head.pos, std::size_t(rawlen));
    require(lab::crc32(out.data(), out.size()) == crc_raw, "stored raw checksum");
    return out;
}

#if !YELP_DECODE_ONLY
Bytes encode_generic(Slice payload) {
    const auto crc_raw = lab::crc32(payload.data, payload.size);
    auto z = zstd_compress(payload.data, payload.size, kGenericLevel);
    Bytes out;
    if (!z.empty() && z.size() < payload.size) {
        out.insert(out.end(), {'Y', 'C', 'L', '1', 1, M_ZSTD});
        put_var(out, payload.size);
        put_u32(out, crc_raw);
        out.push_back(C_ZSTD);
        put_var(out, z.size());
        put_u32(out, lab::crc32(z.data(), z.size()));
        put_u32(out, lab::crc32(out.data(), out.size()));
        out.insert(out.end(), z.begin(), z.end());
    } else {
        out.insert(out.end(), {'Y', 'C', 'L', '1', 1, M_STORED});
        put_var(out, payload.size);
        put_u32(out, crc_raw);
        put_u32(out, lab::crc32(out.data(), out.size()));
        if (payload.size) out.insert(out.end(), payload.data, payload.data + payload.size);
    }
    return out;
}
Bytes encode_payload(Slice payload) {
    Bytes out;
    if (encode_yelp(payload, out)) return out;
    return encode_generic(payload);
}
#endif

void check_output_directory(const fs::path& directory) {
    require(fs::is_directory(fs::symlink_status(directory)) && fs::is_empty(directory), "output directory must exist, be real and empty");
}
Bytes index_bytes(const std::vector<OutputRecord>& records, bool archive) {
    uint64_t size = 9;
    for (const auto& record : records) size = add(size, 12 + record.name.size());
    Bytes index(static_cast<std::size_t>(size));
    std::memcpy(index.data(), archive ? "HBA1" : "HBI1", 4);
    index[4] = 1;
    set32(index.data() + 5, uint32_t(records.size()));
    std::size_t pos = 9;
    for (const auto& record : records) {
        set32(index.data() + pos, uint32_t(record.name.size()));
        pos += 4;
        std::memcpy(index.data() + pos, record.name.data(), record.name.size());
        pos += record.name.size();
        set64(index.data() + pos, 0);
        pos += 8;
    }
    return index;
}
void write_directory(const fs::path& directory, const std::vector<OutputRecord>& records, bool archive) {
    check_output_directory(directory);
    const auto index = index_bytes(records, archive);
    std::vector<fs::path> paths;
    paths.reserve(records.size() + 1);
    for (std::size_t i = 0; i < records.size(); ++i) paths.push_back(directory / ordinal(i));
    paths.push_back(directory / "names.bin");
    std::size_t created = 0;
    int fd = -1;
    try {
        for (std::size_t i = 0; i < paths.size(); ++i) {
            fd = ::open(paths[i].c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
            if (fd < 0) throw std::system_error(errno, std::generic_category(), "output open");
            ++created;
            const bool is_index = i == records.size();
            Slice bytes = is_index ? view(index) : view(records[i].payload);
            std::size_t pos = 0;
            while (pos < bytes.size) {
                const auto n = ::write(fd, bytes.data + pos, std::min(bytes.size - pos, std::size_t(1) << 30));
                if (n < 0 && errno == EINTR) continue;
                if (n <= 0) throw std::system_error(n < 0 ? errno : EIO, std::generic_category(), "output write");
                pos += std::size_t(n);
            }
            const int close_status = ::close(fd);
            fd = -1;
            if (close_status != 0) throw std::system_error(errno, std::generic_category(), "output close");
        }
    } catch (...) {
        if (fd >= 0) ::close(fd);
        for (std::size_t i = 0; i < created; ++i) {
            std::error_code error;
            fs::remove(paths[i], error);
            if (error) std::cerr << "codec_error: rollback failed: " << error.message() << '\n';
        }
        throw;
    }
}
void write_stdout(const std::vector<OutputRecord>& records, bool archive) {
    uint64_t total = 9;
    for (const auto& record : records) total = add(add(total, 12 + record.name.size()), record.payload.size());
    std::array<uint8_t, 9> header{};
    std::memcpy(header.data(), archive ? "HBA1" : "HBI1", 4);
    header[4] = 1;
    set32(header.data() + 5, uint32_t(records.size()));
    std::cout.write(reinterpret_cast<const char*>(header.data()), std::streamsize(header.size()));
    for (const auto& record : records) {
        std::array<uint8_t, 8> number{};
        set32(number.data(), uint32_t(record.name.size()));
        std::cout.write(reinterpret_cast<const char*>(number.data()), 4);
        std::cout.write(record.name.data(), std::streamsize(record.name.size()));
        set64(number.data(), record.payload.size());
        std::cout.write(reinterpret_cast<const char*>(number.data()), 8);
        if (!record.payload.empty()) std::cout.write(reinterpret_cast<const char*>(record.payload.data()), std::streamsize(record.payload.size()));
    }
    std::cout.flush();
    require(bool(std::cout), "stdout write failure");
}

int run(int argc, char** argv) {
    require(argc >= 2, "missing operation");
    const std::string op = argv[1];
    const bool directory = op == "encode-dir" || op == "decode-dir";
    const bool encode = op == "encode-dir" || op == "encode-stream";
    require(directory || op == "encode-stream" || op == "decode-stream", "unknown operation");
#if YELP_DECODE_ONLY
    require(!encode, "decoder-only executable rejects encoding");
#endif
    const int expected = directory ? 4 : 2;
    require(argc == expected, "argument count");
    std::ios::sync_with_stdio(false);
    std::cin.tie(nullptr);
    fs::path input_dir, output_dir;
    if (directory) {
        input_dir = argv[2];
        output_dir = argv[3];
        check_output_directory(output_dir);
    }
    auto input = input_bundle(directory ? &input_dir : nullptr, !encode);
    std::vector<OutputRecord> output;
    output.reserve(input.records.size());
    for (const auto& record : input.records) {
        Bytes bytes;
#if !YELP_DECODE_ONLY
        if (encode) {
            bytes = encode_payload(record.payload);
        } else
#endif
        {
            bytes = decode_payload(record.payload);
        }
        output.push_back({record.name, std::move(bytes)});
    }
    if (directory) write_directory(output_dir, output, encode);
    else write_stdout(output, encode);
    return 0;
}

}  // namespace yc

int main(int argc, char** argv) {
    try {
        return yc::run(argc, argv);
    } catch (const std::exception& error) {
        std::cerr << "codec_error: " << error.what() << '\n';
        return 2;
    } catch (...) {
        std::cerr << "codec_error: unknown failure\n";
        return 2;
    }
}
