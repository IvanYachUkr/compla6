// pypack-fast: shared container definitions.
//
// Transports (defined by the lab's strict parsers, mirrored here for the
// stream interface):
//   HBI1 = original layout : magic "HBI1", u8 version=1, u32 BE record count,
//          then per record: u32 BE alias length, alias bytes, u64 BE payload
//          length, payload bytes.
//   HBA1 = archive layout  : same framing with magic "HBA1"; index records
//          (names.bin) carry empty payloads and the payloads live in ordinal
//          files 00000000.bin, 00000001.bin, ...
// Directory encode: input  = HBI1 names.bin + ordinal payload files
// Directory decode: output = HBI1 names.bin + ordinal payload files
// Directory archive: names.bin (HBA1, empty payloads) + ordinal archive files.
//
// PYLZ object payload (fast mode), little-endian:
//   bytes 0..1  magic 'P''Z'
//   byte  2     format id (1 = PYLZ fast LZ)
//   byte  3     flags (0)
//   bytes 4..11 u64 original length
//   bytes 12..  LZ token stream
#pragma once
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

namespace pylz {

static constexpr uint8_t FMT_FAST = 1;

struct Record {
  std::string alias;
  std::vector<uint8_t> payload;  // index records: empty; stream/dir data: content
};

// ---- strict packed-format parser -------------------------------------------
inline bool valid_alias_char(char c) {
  return (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-';
}

inline bool valid_alias(const std::string& a) {
  if (a.empty() || a.size() > 1048576) return false;
  size_t i = 0;
  while (i < a.size() && ((a[i] >= 'a' && a[i] <= 'z') || (a[i] >= '0' && a[i] <= '9'))) i++;
  while (i < a.size()) {
    if (a[i] != '-') return false;
    i++;
    size_t start = i;
    while (i < a.size() && ((a[i] >= 'a' && a[i] <= 'z') || (a[i] >= '0' && a[i] <= '9'))) i++;
    if (i == start) return false;
  }
  return true;
}

// Parse a packed stream. When expect_magic is non-null it must match; the
// trailing-bytes rule is enforced. Empty payloads are allowed (index records).
inline bool parse_packed(const uint8_t* p, size_t n, const char* magic,
                         std::vector<Record>& out) {
  if (n < 9 || memcmp(p, magic, 4) != 0) return false;
  if (p[4] != 1) return false;
  uint32_t count = (uint32_t(p[5]) << 24) | (uint32_t(p[6]) << 16) |
                   (uint32_t(p[7]) << 8) | uint32_t(p[8]);
  if (count == 0 || count > 1000000) return false;
  size_t off = 9;
  std::string prev;
  bool first = true;
  out.clear();
  out.reserve(count);
  for (uint32_t r = 0; r < count; r++) {
    if (off + 4 > n) return false;
    uint32_t alen = (uint32_t(p[off]) << 24) | (uint32_t(p[off+1]) << 16) |
                    (uint32_t(p[off+2]) << 8) | uint32_t(p[off+3]);
    off += 4;
    if (alen < 1 || alen > 1048576 || off + alen > n) return false;
    Record rec;
    rec.alias.assign((const char*)p + off, alen);
    off += alen;
    if (!valid_alias(rec.alias)) return false;
    if (!first && !(prev < rec.alias)) return false;  // strictly increasing
    first = false;
    prev = rec.alias;
    if (off + 8 > n) return false;
    uint64_t plen = 0;
    for (int i = 0; i < 8; i++) plen = (plen << 8) | p[off + i];
    off += 8;
    if (off + plen > n) return false;
    rec.payload.assign(p + off, p + off + plen);
    off += plen;
    out.push_back(std::move(rec));
  }
  return off == n;  // no trailing bytes
}

inline void append_u32be(std::vector<uint8_t>& v, uint32_t x) {
  v.push_back(uint8_t(x >> 24)); v.push_back(uint8_t(x >> 16));
  v.push_back(uint8_t(x >> 8)); v.push_back(uint8_t(x));
}
inline void append_u64be(std::vector<uint8_t>& v, uint64_t x) {
  for (int i = 7; i >= 0; i--) v.push_back(uint8_t(x >> (8 * i)));
}

inline void serialize_packed(const char* magic, const std::vector<Record>& recs,
                             std::vector<uint8_t>& out) {
  out.clear();
  out.reserve(9 + recs.size() * 16);
  out.insert(out.end(), magic, magic + 4);
  out.push_back(1);
  append_u32be(out, (uint32_t)recs.size());
  for (const Record& r : recs) {
    append_u32be(out, (uint32_t)r.alias.size());
    out.insert(out.end(), r.alias.begin(), r.alias.end());
    append_u64be(out, r.payload.size());
    out.insert(out.end(), r.payload.begin(), r.payload.end());
  }
}

// ---- content checksum -------------------------------------------------------
// libxxhash.so.0 is a pinned standard codec library (deducted from decoder
// deployment size); the build environment ships no <xxhash.h> header, so the
// C symbol is declared directly.
typedef unsigned long long XXH64_hash_t;
extern "C" XXH64_hash_t XXH3_64bits(const void* input, size_t length);

// ---- object payload header --------------------------------------------------
// 20 bytes: magic 'PZ', format id, flags, u64 LE original length, u64 LE
// XXH3-64 checksum of the original bytes (corruption detection).
constexpr size_t HEADER_SIZE = 20;

inline void put_header(uint8_t* out, uint8_t fmt, uint8_t flags,
                       uint64_t orig_len, uint64_t checksum) {
  out[0] = 'P'; out[1] = 'Z';
  out[2] = fmt; out[3] = flags;
  for (int i = 0; i < 8; i++) out[4 + i] = uint8_t(orig_len >> (8 * i));
  for (int i = 0; i < 8; i++) out[12 + i] = uint8_t(checksum >> (8 * i));
}

inline bool get_header(const uint8_t* p, size_t n, uint8_t fmt,
                       uint64_t& orig_len, size_t& body_off, uint8_t& flags,
                       uint64_t& checksum) {
  if (n < HEADER_SIZE || p[0] != 'P' || p[1] != 'Z' || p[2] != fmt)
    return false;
  flags = p[3];
  orig_len = 0;
  for (int i = 0; i < 8; i++) orig_len |= uint64_t(p[4 + i]) << (8 * i);
  checksum = 0;
  for (int i = 0; i < 8; i++) checksum |= uint64_t(p[12 + i]) << (8 * i);
  body_off = HEADER_SIZE;
  return true;
}

// ---- file helpers -----------------------------------------------------------
inline bool read_file(const std::string& path, std::vector<uint8_t>& data) {
  FILE* f = fopen(path.c_str(), "rb");
  if (!f) return false;
  if (fseek(f, 0, SEEK_END) != 0) { fclose(f); return false; }
  long sz = ftell(f);
  if (sz < 0) { fclose(f); return false; }
  if (fseek(f, 0, SEEK_SET) != 0) { fclose(f); return false; }
  data.resize((size_t)sz);
  bool ok = sz == 0 || fread(data.data(), 1, (size_t)sz, f) == (size_t)sz;
  fclose(f);
  return ok;
}

inline bool write_file(const std::string& path, const uint8_t* data, size_t n) {
  FILE* f = fopen(path.c_str(), "wb");
  if (!f) return false;
  bool ok = n == 0 || fwrite(data, 1, n, f) == n;
  if (fclose(f) != 0) ok = false;
  return ok;
}

inline std::string ordinal_name(size_t i) {
  char buf[32];
  snprintf(buf, sizeof buf, "%08zu.bin", i);
  return std::string(buf);
}

}  // namespace pylz
