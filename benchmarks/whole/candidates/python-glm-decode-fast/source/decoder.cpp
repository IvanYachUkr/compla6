// pypack-fast decoder: LZ4-class memcpy-style reconstruction, optimized for
// throughput. See format.h and codec.cpp for the container and stream format.
#include "format.h"
#include <cstring>

using namespace pylz;

namespace {

// Wild-copy helpers write 8/16 bytes at a time; dst must have >=16 slack bytes
// beyond the logical end, and copies may read up to 8 bytes before src.
inline void wild8(uint8_t* dst, const uint8_t* src, size_t n) {
  do {
    memcpy(dst, src, 8);
    dst += 8; src += 8;
  } while (n -= 8);  // n is a positive multiple of 8
}

inline void wild16(uint8_t* dst, const uint8_t* src, size_t n) {
  do {
    memcpy(dst, src, 16);
    dst += 16; src += 16;
  } while (n -= 16);  // n is a positive multiple of 16
}

// Decode the LZ stream [ip, iend) into out (size orig_len, capacity + 32).
// Returns false on any violation (bounds, distance, trailing bytes).
//
// Shape: while the cursor is far from both ends (>= 35 readable input bytes,
// >= 48 writable output bytes) a fast token path runs with almost no per-token
// bounds work; the 255-escape chains read at most the margin plus the 32-byte
// zero padding, and one predictable check on the decoded match length plus the
// copy grains keeps every wide write inside the output slack. A fully checked
// slow path handles the last tokens of any stream.
bool decode_stream(const uint8_t* ip, const uint8_t* iend, uint8_t* op,
                   size_t orig_len) {
  uint8_t* const obeg = op;
  uint8_t* const oend = op + orig_len;
  uint8_t* const ofast = oend - 48;
  const uint8_t* const ifast = iend - 35;

  while (op < oend) {
    bool fast = ip <= ifast && op <= ofast;
    // ---- token + literal run ----
    if (fast) {
      uint8_t tok = *ip++;
      size_t lit = tok >> 4;
      if (lit == 15) {
        uint8_t b;
        do { b = *ip++; lit += b; } while (b == 255 && ip < iend + 24);
      }
      size_t avail = ip < iend ? (size_t)(iend - ip) : 0;
      if (avail < lit) return false;  // chain reads may pass iend into padding
      if (lit) {
        if (lit <= 8) {
          memcpy(op, ip, 8);
        } else if (lit <= 16) {
          memcpy(op, ip, 8);
          memcpy(op + 8, ip + 8, 8);
        } else {
          size_t w = (lit + 7) & ~(size_t)7;
          uint8_t* d = op;
          const uint8_t* s = ip;
          do { memcpy(d, s, 8); d += 8; s += 8; } while (w -= 8);
        }
      }
      ip += lit;
      op += lit;
      if (op == oend) break;  // final literals-only token
      // ---- match (wide margins) ----
      size_t dist = ip[0] | (uint32_t(ip[1]) << 8);
      ip += 2;
      size_t mlen = tok & 0x0F;
      if (mlen >= 14) {
        if (mlen == 14) { dist |= uint32_t(*ip++) << 16; }
        mlen = 18;
        uint8_t b;
        do { b = *ip++; mlen += b; } while (b == 255 && ip < iend + 24);
      } else {
        mlen += 4;
      }
      if (dist == 0 || dist > (size_t)(op - obeg) || mlen > (size_t)(oend - op))
        return false;
      uint8_t* d = op;
      const uint8_t* s = op - dist;
      if (dist == 1) {
        memset(d, *s, mlen);
      } else if (dist >= 8 && mlen <= 8) {
        memcpy(d, s, 8);
      } else if (dist >= 8 && mlen <= 16) {
        memcpy(d, s, 8);
        memcpy(d + 8, s + 8, 8);
      } else if (dist >= 8 && mlen <= 24) {
        memcpy(d, s, 8);
        memcpy(d + 8, s + 8, 8);
        memcpy(d + 16, s + 16, 8);
      } else if (dist >= 8) {
        size_t w = (mlen + 7) & ~(size_t)7;
        do { memcpy(d, s, 8); d += 8; s += 8; } while (w -= 8);
      } else if (dist >= 4) {
        size_t w = (mlen + 3) & ~(size_t)3;
        do { memcpy(d, s, 4); d += 4; s += 4; } while (w -= 4);
      } else {
        // 2-byte grain: pairs copy correctly for all owned bytes; an odd
        // length's final pair writes one harmless byte past the match end
        size_t w = (mlen + 1) & ~(size_t)1;
        do { memcpy(d, s, 2); d += 2; s += 2; } while (w -= 2);
      }
      op += mlen;
    } else {
      // ---- slow, fully checked path ----
      if (ip >= iend) return false;
      uint8_t tok = *ip++;
      size_t lit = tok >> 4;
      if (lit == 15) {
        uint8_t b;
        do {
          if (ip >= iend) return false;
          b = *ip++;
          lit += b;
        } while (b == 255);
      }
      if ((size_t)(iend - ip) < lit || (size_t)(oend - op) < lit) return false;
      memcpy(op, ip, lit);
      ip += lit;
      op += lit;
      if (op == oend) break;
      if (iend - ip < 2) return false;  // match needs at least a 2-byte distance
      size_t dist = ip[0] | (uint32_t(ip[1]) << 8);
      ip += 2;
      size_t mlen = tok & 0x0F;
      if (mlen >= 14) {
        if (mlen == 14) {
          if (ip >= iend) return false;
          dist |= uint32_t(*ip++) << 16;
        }
        mlen = 18;
        uint8_t b;
        do {
          if (ip >= iend) return false;
          b = *ip++;
          mlen += b;
        } while (b == 255);
      } else {
        mlen += 4;
      }
      if (dist == 0 || dist > (size_t)(op - obeg) || mlen > (size_t)(oend - op))
        return false;
      const uint8_t* s = op - dist;
      if (dist == 1) {
        memset(op, *s, mlen);
      } else if (dist >= 8) {
        size_t w = (mlen + 7) & ~(size_t)7;
        uint8_t* d = op;
        do { memcpy(d, s, 8); d += 8; s += 8; } while (w -= 8);
      } else if (dist >= 4) {
        size_t w = (mlen + 3) & ~(size_t)3;
        uint8_t* d = op;
        do { memcpy(d, s, 4); d += 4; s += 4; } while (w -= 4);
      } else {
        for (size_t k = 0; k < mlen; k++) op[k] = s[k];
      }
      op += mlen;
    }
  }
  return op == oend && ip == iend;  // no trailing bytes
}

int decode_dir(const std::string& in_dir, const std::string& out_dir) {
  std::vector<uint8_t> names;
  if (!read_file(in_dir + "/names.bin", names)) {
    fprintf(stderr, "pypack: cannot read %s/names.bin\n", in_dir.c_str());
    return 2;
  }
  std::vector<Record> recs;
  if (!parse_packed(names.data(), names.size(), "HBA1", recs)) {
    fprintf(stderr, "pypack: invalid HBA1 names.bin\n");
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
    std::vector<uint8_t> arc;
    if (!read_file(in_dir + "/" + ordinal_name(idx), arc)) {
      fprintf(stderr, "pypack: cannot read archive object %zu\n", idx);
      return 2;
    }
    uint64_t orig_len = 0, want = 0;
    size_t body = 0;
    uint8_t flags = 0;
    if (!get_header(arc.data(), arc.size(), FMT_FAST, orig_len, body, flags, want) ||
        orig_len > (uint64_t)1 << 33 || flags != 0) {
      fprintf(stderr, "pypack: bad archive header, object %zu\n", idx);
      return 2;
    }
    Record r;
    r.alias = recs[idx].alias;
    size_t arcsz = arc.size();
    arc.resize(arcsz + 32, 0);  // slack for wide literal reads at the end
    r.payload.resize((size_t)orig_len + 32);
    if (!decode_stream(arc.data() + body, arc.data() + arcsz,
                       r.payload.data(), (size_t)orig_len) ||
        XXH3_64bits(r.payload.data(), (size_t)orig_len) != want) {
      fprintf(stderr, "pypack: corrupt archive, object %zu\n", idx);
      return 2;
    }
    r.payload.resize((size_t)orig_len);
    out_recs.push_back(std::move(r));
  }
  // directory names.bin: aliases with empty payloads; payloads live in ordinal files
  std::vector<Record> name_recs;
  name_recs.reserve(out_recs.size());
  for (const Record& r : out_recs) {
    Record nr;
    nr.alias = r.alias;
    name_recs.push_back(std::move(nr));
  }
  std::vector<uint8_t> names_out;
  serialize_packed("HBI1", name_recs, names_out);
  if (!write_file(out_dir + "/names.bin", names_out.data(), names_out.size())) {
    fprintf(stderr, "pypack: cannot write names.bin\n");
    return 2;
  }
  for (size_t idx = 0; idx < out_recs.size(); idx++) {
    const Record& r = out_recs[idx];
    if (!write_file(out_dir + "/" + ordinal_name(idx), r.payload.data(), r.payload.size())) {
      fprintf(stderr, "pypack: cannot write object %zu\n", idx);
      return 2;
    }
  }
  return 0;
}

int decode_stream_main() {
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
  if (!parse_packed(in.data(), in.size() - 32, "HBA1", recs)) {
    fprintf(stderr, "pypack: invalid HBA1 stream\n");
    return 2;
  }
  std::vector<Record> out_recs;
  out_recs.reserve(recs.size());
  for (size_t idx = 0; idx < recs.size(); idx++) {
    uint64_t orig_len = 0, want = 0;
    size_t body = 0;
    uint8_t flags = 0;
    size_t psz = recs[idx].payload.size();
    if (!get_header(recs[idx].payload.data(), psz, FMT_FAST,
                    orig_len, body, flags, want) || flags != 0) {
      fprintf(stderr, "pypack: bad archive header, object %zu\n", idx);
      return 2;
    }
    Record r;
    r.alias = recs[idx].alias;
    recs[idx].payload.resize(psz + 32, 0);  // slack for wide reads at the end
    r.payload.resize((size_t)orig_len + 32);
    if (!decode_stream(recs[idx].payload.data() + body,
                       recs[idx].payload.data() + psz,
                       r.payload.data(), (size_t)orig_len) ||
        XXH3_64bits(r.payload.data(), (size_t)orig_len) != want) {
      fprintf(stderr, "pypack: corrupt archive, object %zu\n", idx);
      return 2;
    }
    r.payload.resize((size_t)orig_len);
    out_recs.push_back(std::move(r));
  }
  std::vector<uint8_t> buf;
  serialize_packed("HBI1", out_recs, buf);
  if (fwrite(buf.data(), 1, buf.size(), stdout) != buf.size()) return 2;
  return 0;
}

}  // namespace

int main(int argc, char** argv) {
  if (argc == 4 && strcmp(argv[1], "decode-dir") == 0)
    return decode_dir(argv[2], argv[3]);
  if (argc == 2 && strcmp(argv[1], "decode-stream") == 0)
    return decode_stream_main();
  fprintf(stderr, "usage: decoder decode-dir IN OUT | decoder decode-stream\n");
  return 64;
}
