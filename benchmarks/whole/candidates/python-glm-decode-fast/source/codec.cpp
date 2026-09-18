// pypack-fast encoder: byte-aligned LZ77 tuned for decode speed on Python source.
//
// Archive payload per object: "PZ" + fmt(1) + flags(0) + u64 LE original length
// followed by an LZ4-class token stream:
//   token byte: high nibble L = literal length (15 = escape chain), low nibble M
//   literals (raw copy)
//   M <= 13 : match length = M+4 (4..17), distance = 2 bytes LE (1..65535)
//   M == 15 : match length = 18 + escape chain (bytes 0..254, 255 continues),
//             distance = 2 bytes LE
//   M == 14 : distance = 3 bytes LE (65536..16777215), match length = 18 + escape
//   final token carries literals only (no distance), stream ends when the
//   decoder has produced the declared original length and the input is consumed.
//
// Match search: 4-byte rolling hash into a 2^22 head table over a 16 MiB window,
// depth-limited chain walk, one-step lazy parse, distance-tiered minimum lengths.
#include "format.h"
#include <chrono>
#include <cstdlib>

using namespace pylz;

namespace {

// Shipping configuration (validated on the full corpus): 16 MiB window,
// 2^20-entry hash table, depth-4 chains with one-step lazy matching.
#ifndef PYPACK_WINDOW_LOG
#define PYPACK_WINDOW_LOG 24
#endif
#ifndef PYPACK_HBITS
#define PYPACK_HBITS 20
#endif
#ifndef PYPACK_DEPTH
#define PYPACK_DEPTH 4
#endif
#ifndef PYPACK_GOOD_LEN
#define PYPACK_GOOD_LEN 48
#endif
#ifndef PYPACK_LAZY
#define PYPACK_LAZY 1
#endif
constexpr size_t WINDOW = size_t(1) << PYPACK_WINDOW_LOG;
constexpr size_t WMASK = WINDOW - 1;
constexpr uint32_t HBITS = PYPACK_HBITS;
constexpr size_t HSIZE = size_t(1) << HBITS;
constexpr size_t GOOD_LEN = PYPACK_GOOD_LEN;   // stop chain walk at this length
constexpr int DEPTH = PYPACK_DEPTH;            // chain candidates per search

inline uint32_t hash4(const uint8_t* p) {
  uint32_t v;
  memcpy(&v, p, 4);
  return (v * 2654435761u) >> (32 - HBITS);
}

// Number of leading matching bytes between a and b, bounded by limit.
inline size_t common_len(const uint8_t* a, const uint8_t* b, size_t limit) {
  size_t l = 0;
  while (l + 8 <= limit) {
    uint64_t x, y;
    memcpy(&x, a + l, 8);
    memcpy(&y, b + l, 8);
    uint64_t d = x ^ y;
    if (d) return l + (unsigned)__builtin_ctzll(d) / 8;
    l += 8;
  }
  while (l < limit && a[l] == b[l]) l++;
  return l;
}

struct Writer {
  std::vector<uint8_t>& out;
  uint8_t* w;                // write cursor into out's storage
  const uint8_t* lit_base;
  size_t lit_start = 0;      // pending literal run [lit_start, lit_pos)
  size_t lit_pos = 0;

  Writer(std::vector<uint8_t>& o, const uint8_t* base)
      : out(o), lit_base(base) {
    w = out.data() + HEADER_SIZE;  // header already placed
  }
  size_t size() const { return (size_t)(w - out.data()); }

  void extend_literals(size_t to) { lit_pos = to; }

  void flush_last_literals() {
    size_t n = lit_pos - lit_start;
    if (n == 0) return;
    if (n < 15) {
      *w++ = uint8_t(n << 4);
    } else {
      *w++ = 0xF0;
      size_t rem = n - 15;
      while (rem >= 255) { *w++ = 255; rem -= 255; }
      *w++ = uint8_t(rem);
    }
    memcpy(w, lit_base + lit_start, n);
    w += n;
    lit_start = lit_pos;
  }

  void emit_match(size_t len, size_t dist, size_t match_end) {
    size_t litn = lit_pos - lit_start;
    uint8_t* tok = w;
    if (litn < 15) {
      *w++ = uint8_t(litn << 4);
    } else {
      *w++ = 0xF0;
      size_t rem = litn - 15;
      while (rem >= 255) { *w++ = 255; rem -= 255; }
      *w++ = uint8_t(rem);
    }
    if (litn) {
      memcpy(w, lit_base + lit_start, litn);
      w += litn;
    }
    lit_start = lit_pos = match_end;
    // stream order after literals: distance, then length extras
    size_t m = len - 4;
    if (dist < 65536) {
      w[0] = uint8_t(dist);
      w[1] = uint8_t(dist >> 8);
      w += 2;
      if (m <= 13) {
        *tok |= uint8_t(m);
      } else {
        *tok |= 0x0F;  // long match, 2-byte distance
        size_t x = len - 18;
        while (x >= 255) { *w++ = 255; x -= 255; }
        *w++ = uint8_t(x);
      }
    } else {
      *tok |= 0x0E;  // 3-byte distance, long match
      w[0] = uint8_t(dist);
      w[1] = uint8_t(dist >> 8);
      w[2] = uint8_t(dist >> 16);
      w += 3;
      size_t x = len - 18;
      while (x >= 255) { *w++ = 255; x -= 255; }
      *w++ = uint8_t(x);
    }
  }
};

std::vector<uint8_t> compress_fast(const uint8_t* p, size_t n) {
  std::vector<uint8_t> out;
  if (n == 0) {
    out.resize(HEADER_SIZE);
    put_header(out.data(), FMT_FAST, 0, 0, XXH3_64bits(p, 0));
    return out;
  }
  out.resize(HEADER_SIZE + n + n / 8 + 1024);  // worst case: all literals + tokens
  put_header(out.data(), FMT_FAST, 0, n, XXH3_64bits(p, n));

  std::vector<int32_t> head(HSIZE, -1);
  std::vector<int32_t> prev(WINDOW);
  Writer em(out, p);

  size_t i = 0;
  size_t last_insert = 0;  // positions < last_insert are in the chain
  auto insert_range = [&](size_t from, size_t to) {
    if (to > n - 3) to = n - 3;  // need 4 bytes to hash
    for (size_t k = from; k < to; k++) {
      uint32_t hh = hash4(p + k);
      prev[k & WMASK] = head[hh];
      head[hh] = (int32_t)k;
    }
    if (to > last_insert) last_insert = to;
  };
  // shallow modes (DEPTH <= 2) index only match starts and literal positions
  auto insert_pos = [&](size_t k, uint32_t hh, int32_t old_head) {
    if (k + 4 > n) return;
    if (DEPTH > 1) prev[k & WMASK] = old_head;
    head[hh] = (int32_t)k;
    if (k + 1 > last_insert) last_insert = k + 1;
  };
  auto search_h = [&](size_t pos, uint32_t hh, size_t& blen, size_t& bdist,
                      int32_t cand0) {
    blen = 0; bdist = 0;
    size_t ml = n - pos;
    if (ml < 4) return;
    int32_t cand = cand0;
    int depth = DEPTH;
    while (cand >= 0 && depth-- > 0) {
      size_t dist = pos - (size_t)cand;
      if (dist > WINDOW) break;
      // tiered minimums; far matches must reach the 3-byte-offset base (18)
      size_t req = dist < 512 ? 4 : dist < 16384 ? 5 : dist < 65536 ? 6 : 18;
      if (blen < ml && p[(size_t)cand + blen] == p[pos + blen]) {
        size_t l = common_len(p + cand, p + pos, ml);
        if ((int)l >= (int)req && l > blen) {
          blen = l; bdist = dist;
          if (l >= GOOD_LEN) break;
        }
      }
      if (DEPTH == 1) break;
      cand = prev[cand & WMASK];
    }
  };

  while (i < n) {
    if (i + 4 > n) break;  // tail literals
    uint32_t h = hash4(p + i);
    size_t len1, dist1;
    search_h(i, h, len1, dist1, head[h]);
    if (len1 == 0) {
      em.extend_literals(i + 1);
      insert_pos(i, h, head[h]);
      i++;
      continue;
    }
    // one-step lazy: prefer a longer match starting at i+1
    if (PYPACK_LAZY && i + 1 + 4 <= n && len1 < GOOD_LEN) {
      insert_pos(i, h, head[h]);
      uint32_t h2 = hash4(p + i + 1);
      size_t len2, dist2;
      search_h(i + 1, h2, len2, dist2, head[h2]);
      bool adopt = len2 > len1;
      size_t mlen = adopt ? len2 : len1;
      size_t mdist = adopt ? dist2 : dist1;
      size_t mend = (adopt ? i + 1 : i) + mlen;
      if (adopt) em.extend_literals(i + 1);
      if (DEPTH > 2) insert_range(last_insert, mend);
      else {
        insert_pos(i + 1, h2, head[h2]);
        if (mend > last_insert) last_insert = mend;
      }
      em.emit_match(mlen, mdist, mend);
      i = mend;
      continue;
    }
    size_t mend = i + len1;
    if (DEPTH > 2) insert_range(last_insert, mend);
    else if (mend > last_insert) last_insert = mend;
    em.emit_match(len1, dist1, mend);
    i = mend;
  }
  if (i < n) em.extend_literals(n);
  em.flush_last_literals();
  out.resize(em.size());
  return out;
}

// ---- directory / stream plumbing -------------------------------------------

int encode_dir(const std::string& in_dir, const std::string& out_dir) {
  std::vector<uint8_t> names;
  if (!read_file(in_dir + "/names.bin", names)) {
    fprintf(stderr, "pypack: cannot read %s/names.bin\n", in_dir.c_str());
    return 2;
  }
  std::vector<Record> recs;
  if (!parse_packed(names.data(), names.size(), "HBI1", recs)) {
    fprintf(stderr, "pypack: invalid HBI1 names.bin\n");
    return 2;
  }
  for (const Record& r : recs)
    if (!r.payload.empty()) {
      fprintf(stderr, "pypack: nonempty index payload\n");
      return 2;
    }
  std::vector<Record> out_recs;
  out_recs.reserve(recs.size());
  for (size_t idx = 0; idx < recs.size(); idx++) {
    std::vector<uint8_t> data;
    if (!read_file(in_dir + "/" + ordinal_name(idx), data)) {
      fprintf(stderr, "pypack: cannot read object %zu\n", idx);
      return 2;
    }
    std::vector<uint8_t> arc = compress_fast(data.data(), data.size());
    data.clear();
    data.shrink_to_fit();  // release before the archive copy is materialized
    Record r;
    r.alias = recs[idx].alias;
    r.payload = std::move(arc);
    out_recs.push_back(std::move(r));
  }
  // archive names.bin: aliases with empty payloads; payloads live in ordinal files
  std::vector<Record> name_recs;
  name_recs.reserve(out_recs.size());
  for (const Record& r : out_recs) {
    Record nr;
    nr.alias = r.alias;
    name_recs.push_back(std::move(nr));
  }
  std::vector<uint8_t> names_out;
  serialize_packed("HBA1", name_recs, names_out);
  if (!write_file(out_dir + "/names.bin", names_out.data(), names_out.size())) {
    fprintf(stderr, "pypack: cannot write archive names.bin\n");
    return 2;
  }
  for (size_t idx = 0; idx < out_recs.size(); idx++) {
    const Record& r = out_recs[idx];
    if (!write_file(out_dir + "/" + ordinal_name(idx), r.payload.data(), r.payload.size())) {
      fprintf(stderr, "pypack: cannot write archive object %zu\n", idx);
      return 2;
    }
  }
  return 0;
}

int encode_stream() {
  std::vector<uint8_t> in;
  {
    constexpr size_t CH = 1 << 20;
    size_t sz = 0;
    for (;;) {
      in.resize(sz + CH);
      size_t got = fread(in.data() + sz, 1, CH, stdin);
      sz += got;
      if (got < CH) break;
    }
    in.resize(sz + 32, 0);
  }
  std::vector<Record> recs;
  if (!parse_packed(in.data(), in.size() - 32, "HBI1", recs)) {
    fprintf(stderr, "pypack: invalid HBI1 stream\n");
    return 2;
  }
  std::vector<Record> out_recs;
  out_recs.reserve(recs.size());
  for (size_t idx = 0; idx < recs.size(); idx++) {
    std::vector<uint8_t> arc = compress_fast(recs[idx].payload.data(), recs[idx].payload.size());
    Record r;
    r.alias = recs[idx].alias;
    r.payload = std::move(arc);
    out_recs.push_back(std::move(r));
  }
  std::vector<uint8_t> buf;
  serialize_packed("HBA1", out_recs, buf);
  if (fwrite(buf.data(), 1, buf.size(), stdout) != buf.size()) return 2;
  return 0;
}

}  // namespace

int main(int argc, char** argv) {
  if (argc == 4 && strcmp(argv[1], "encode-dir") == 0)
    return encode_dir(argv[2], argv[3]);
  if (argc == 2 && strcmp(argv[1], "encode-stream") == 0)
    return encode_stream();
  fprintf(stderr, "usage: codec encode-dir IN OUT | codec encode-stream\n");
  return 64;
}
