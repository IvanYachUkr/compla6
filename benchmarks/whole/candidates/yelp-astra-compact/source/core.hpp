#pragma once
#include "columnar.hpp"
#include "rans.hpp"
#include <algorithm>
#include <atomic>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <vector>
namespace sw {
using Bytes = std::vector<uint8_t>;
inline void require(bool p, const char *s) {
  if (!p)
    throw std::runtime_error(s);
}
inline uint32_t load32(const uint8_t *p) {
  uint32_t v;
  std::memcpy(&v, p, 4);
  return v;
}
inline uint64_t load64(const uint8_t *p) {
  uint64_t v;
  std::memcpy(&v, p, 8);
  return v;
}
inline void put32(Bytes &b, uint32_t v) {
  for (int i = 0; i < 4; ++i)
    b.push_back(uint8_t(v >> (8 * i)));
}
inline void put64(Bytes &b, uint64_t v) {
  for (int i = 0; i < 8; ++i)
    b.push_back(uint8_t(v >> (8 * i)));
}
struct Reader {
  const uint8_t *p;
  size_t n, pos = 0;
  Reader(const uint8_t *p_, size_t n_) : p(p_), n(n_) {}
  uint8_t byte() {
    require(pos < n, "truncated archive");
    return p[pos++];
  }
  uint32_t u32() {
    require(n - pos >= 4, "truncated integer");
    auto v = load32(p + pos);
    pos += 4;
    return v;
  }
  uint64_t u64() {
    require(n - pos >= 8, "truncated integer");
    auto v = load64(p + pos);
    pos += 8;
    return v;
  }
  const uint8_t *take(size_t z) {
    require(z <= n - pos, "truncated payload");
    auto q = pos ? p + pos : p;
    pos += z;
    return q;
  }
  void end() { require(pos == n, "trailing archive bytes"); }
};
inline uint64_t checksum(const uint8_t *p, size_t n) {
  uint64_t a = 0x243f6a8885a308d3ULL ^ n, b = 0x13198a2e03707344ULL;
  size_t i = 0;
  for (; i + 16 <= n; i += 16) {
    a = (a ^ load64(p + i)) * 0x9e3779b185ebca87ULL;
    b = (b ^ load64(p + i + 8)) * 0xc2b2ae3d27d4eb4fULL;
  }
  for (; i < n; ++i)
    a = (a ^ p[i]) * 0x100000001b3ULL;
  a ^= b + (b << 17);
  a ^= a >> 29;
  a *= 0x165667b19e3779f9ULL;
  return a ^ (a >> 32);
}
inline unsigned workers() {
  const char *e = std::getenv("COMPRESSION_LAB_THREADS");
  if (!e)
    return 4;
  char *end;
  long n = std::strtol(e, &end, 10);
  require(*end == 0 && n > 0, "invalid COMPRESSION_LAB_THREADS");
  return unsigned(std::min(4L, n));
}
template <class F> void parallel(size_t n, F fn) {
  unsigned t = std::min<size_t>(workers(), n);
  if (t <= 1) {
    for (size_t i = 0; i < n; ++i)
      fn(i);
    return;
  }
  std::atomic<size_t> next{0};
  std::exception_ptr err;
  std::mutex lock;
  auto work = [&]() {
    try {
      for (;;) {
        size_t i = next.fetch_add(1, std::memory_order_relaxed);
        if (i >= n)
          break;
        fn(i);
      }
    } catch (...) {
      std::lock_guard<std::mutex> g(lock);
      if (!err)
        err = std::current_exception();
      next.store(n);
    }
  };
  std::vector<std::thread> pool;
  pool.reserve(t - 1);
  try {
    for (unsigned i = 1; i < t; ++i)
      pool.emplace_back(work);
  } catch (...) {
    next.store(n);
    for (auto &th : pool)
      th.join();
    throw;
  }
  work();
  for (auto &th : pool)
    th.join();
  if (err)
    std::rethrow_exception(err);
}
#ifndef BLOCK_BITS
#define BLOCK_BITS 20
#endif
constexpr size_t BLOCK = 1u << BLOCK_BITS;
inline void varput(Bytes &out, uint32_t v) {
  while (v >= 128) {
    out.push_back(uint8_t(v) | 128);
    v >>= 7;
  }
  out.push_back(uint8_t(v));
}
inline uint32_t varget(Reader &r) {
  uint32_t v = 0;
  for (unsigned s = 0; s < 35; s += 7) {
    uint8_t c = r.byte();
    require(s < 28 || c < 16, "varint overflow");
    v |= uint32_t(c & 127) << s;
    if (!(c & 128)) {
      require(s == 0 || c != 0, "noncanonical varint");
      return v;
    }
  }
  throw std::runtime_error("varint overflow");
}
inline void extput(Bytes &o, size_t n) {
  while (n >= 255) {
    o.push_back(255);
    n -= 255;
  }
  o.push_back(uint8_t(n));
}
inline size_t extget(Reader &r, size_t v, size_t bound) {
  for (;;) {
    uint8_t x = r.byte();
    require(x <= bound - v, "length overflow");
    v += x;
    if (x != 255)
      return v;
  }
}
inline void lzdecode(const uint8_t *p, size_t n, uint8_t *out, size_t raw) {
  Reader container(p, n);
  Bytes streams[4];
  for (auto &s : streams) {
    auto size = container.u32();
    auto ptr = container.take(size);
    s = entropy_decode(ptr, size, raw + 32);
  }
  container.end();
  Reader tok(streams[0].data(), streams[0].size()),
      literals(streams[1].data(), streams[1].size()),
      offsets(streams[2].data(), streams[2].size()),
      lengths(streams[3].data(), streams[3].size());
  size_t at = 0;
  while (at < raw) {
    uint8_t t = tok.byte();
    size_t lit = t >> 4;
    if (lit == 15)
      lit = extget(lengths, lit, raw);
    require(lit <= raw - at, "literal overflow");
    auto q = literals.take(lit);
    if (lit)
      std::memcpy(out + at, q, lit);
    at += lit;
    if (at == raw) {
      require((t & 15) == 0, "terminal match");
      break;
    }
    size_t off = varget(offsets);
    require(off > 0 && off <= at, "invalid match distance");
    size_t len = (t & 15) + 4;
    if ((t & 15) == 15)
      len = extget(lengths, len, raw);
    require(len <= raw - at, "match overflow");
    if (off >= len)
      std::memcpy(out + at, out + at - off, len);
    else
      for (size_t j = 0; j < len; ++j)
        out[at + j] = out[at + j - off];
    at += len;
  }
  tok.end();
  literals.end();
  offsets.end();
  lengths.end();
}
inline Bytes stripesdecode(const uint8_t *p, size_t n, size_t raw) {
  Reader r(p, n);
  uint32_t count = r.u32();
  require(count > 0 && count <= 1025, "bad stripe count");
  struct Stripe {
    const uint8_t *p;
    uint32_t raw, n;
    uint8_t mode;
  };
  std::vector<Stripe> stripes;
  stripes.reserve(count);
  size_t total = 0;
  for (uint32_t i = 0; i < count; ++i) {
    uint32_t size = r.u32(), payload = r.u32();
    uint8_t mode = r.byte();
    require(size > 0 && size <= raw - total && mode <= 2,
            "bad stripe dimensions");
    require(mode == 0 ? payload == size : payload < size,
            "bad stripe payload length");
    stripes.push_back({r.take(payload), size, payload, mode});
    total += size;
  }
  require(total == raw, "inconsistent stripe output size");
  r.end();
  // Every stripe and the complete output size were validated before allocation.
  Bytes out(raw);
  size_t at = 0;
  for (const auto &s : stripes) {
    if (s.mode == 0)
      std::memcpy(out.data() + at, s.p, s.raw);
    else if (s.mode == 1)
      lzdecode(s.p, s.n, out.data() + at, s.raw);
    else {
      Bytes decoded = entropy_decode(s.p, s.n, s.raw);
      require(decoded.size() == s.raw, "wrong entropy stripe output size");
      std::memcpy(out.data() + at, decoded.data(), s.raw);
    }
    at += s.raw;
  }
  return out;
}
inline Bytes decode(const Bytes &in) {
  Reader r(in.data(), in.size());
  require(r.u32() == 0x335a4c43, "bad CLZ3 magic");
  uint64_t total = r.u64();
  require(total <= 512u * 1024u * 1024u, "output too large");
  uint32_t count = r.u32();
  require(count <= (total + BLOCK - 1) / BLOCK, "block count mismatch");
  struct Part {
    const uint8_t *p;
    uint32_t n, raw, prep;
    uint64_t hash;
    size_t at;
    uint8_t mode;
  };
  std::vector<Part> parts;
  parts.reserve(count);
  size_t at = 0;
  for (uint32_t i = 0; i < count; ++i) {
    uint8_t mode = r.byte();
    uint32_t raw = r.u32(), prep = r.u32(), n = r.u32();
    uint64_t hash = r.u64();
    require(mode <= 1, "unknown block mode");
    require(raw > 0 && raw <= BLOCK + 4096 && raw <= total - at,
            "block length mismatch");
    require(prep <= raw + 5 && n <= prep + 32, "block payload too large");
    if (mode == 0)
      require(n == prep, "raw length mismatch");
    parts.push_back({r.take(n), n, raw, prep, hash, at, mode});
    at += raw;
  }
  require(at == total, "incomplete blocks");
  r.end();
  Bytes out(total);
  parallel(count, [&](size_t i) {
    const auto &p = parts[i];
    Bytes prep;
    if (p.mode)
      prep = stripesdecode(p.p, p.n, p.prep);
    else if (p.prep)
      prep.assign(p.p, p.p + p.prep);
    Bytes raw = columnar::inverse(prep, p.raw);
    require(checksum(raw.data(), raw.size()) == p.hash, "checksum mismatch");
    std::memcpy(out.data() + p.at, raw.data(), raw.size());
  });
  return out;
}
} // namespace sw
