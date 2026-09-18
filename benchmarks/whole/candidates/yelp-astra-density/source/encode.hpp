#pragma once
#include "core.hpp"

#ifndef HASH_BITS
#define HASH_BITS 18
#endif
#ifndef HASH_BYTES
#define HASH_BYTES 4
#endif
#ifndef DEPTH
#define DEPTH 1
#endif
#ifndef LAZY
#define LAZY 0
#endif
#ifndef RING_BUCKETS
#define RING_BUCKETS 0
#endif
#ifndef CACHE_LAZY
#define CACHE_LAZY 1
#endif
#ifndef INSERT_STEP
#define INSERT_STEP 3
#endif

namespace sw {
static_assert(HASH_BITS > 0 && HASH_BITS < 32, "hash width out of range");
static_assert(HASH_BYTES >= 4 && HASH_BYTES <= 6,
              "hash byte width out of range");
static_assert(INSERT_STEP > 0 && LAZY >= 0, "invalid parser setting");
inline unsigned hash_position(const uint8_t *p) {
  if constexpr (HASH_BYTES == 4)
    return (load32(p) * 2654435761U) >> (32 - HASH_BITS);
  else {
    constexpr uint64_t mask = (uint64_t(1) << (8 * HASH_BYTES)) - 1;
    return unsigned(((load64(p) & mask) * 0x9e3779b185ebca87ULL) >>
                    (64 - HASH_BITS));
  }
}
inline Bytes lzencode(const uint8_t *p, size_t n) {
  std::vector<uint32_t> table((size_t(1) << HASH_BITS) * DEPTH, 0xffffffffU);
  static_assert(DEPTH >= 1 && DEPTH <= 128 && (DEPTH & (DEPTH - 1)) == 0,
                "power-of-two history required");
  std::vector<uint8_t> heads;
  if constexpr (RING_BUCKETS && DEPTH > 1)
    heads.resize(1u << HASH_BITS);
  Bytes tok, literals, offsets, lengths;
  tok.reserve(n / 8);
  literals.reserve(n / 4);
  offsets.reserve(n / 8);
  lengths.reserve(n / 16);
  auto insert = [&](size_t at) {
    const auto h = hash_position(p + at);
    auto *bucket = table.data() + size_t(h) * DEPTH;
    if constexpr (RING_BUCKETS && DEPTH > 1) {
      const unsigned head = (heads[h] + 1) & (DEPTH - 1);
      heads[h] = uint8_t(head);
      bucket[head] = uint32_t(at);
    } else {
      for (size_t k = DEPTH - 1; k > 0; --k)
        bucket[k] = bucket[k - 1];
      bucket[0] = uint32_t(at);
    }
  };
  struct Match {
    size_t length = 0;
    uint32_t reference = 0;
  };
  auto find = [&](size_t at) {
    Match best;
    const auto h = hash_position(p + at);
    auto *bucket = table.data() + size_t(h) * DEPTH;
    unsigned head = 0;
    if constexpr (RING_BUCKETS && DEPTH > 1)
      head = heads[h];
    for (size_t k = 0; k < DEPTH; ++k) {
      uint32_t ref;
      if constexpr (RING_BUCKETS && DEPTH > 1)
        ref = bucket[(head - unsigned(k)) & (DEPTH - 1)];
      else
        ref = bucket[k];
      if (ref == 0xffffffffU || ref >= at || load32(p + ref) != load32(p + at))
        continue;
      if (best.length && at + best.length < n &&
          p[ref + best.length] != p[at + best.length])
        continue;
      size_t len = 4;
      while (at + len + 8 <= n && load64(p + ref + len) == load64(p + at + len))
        len += 8;
      while (at + len < n && p[ref + len] == p[at + len])
        ++len;
      if (len > best.length)
        best = {len, ref};
    }
    return best;
  };
  size_t at = 0, anchor = 0;
  Match pending;
  bool has_pending = false;
  constexpr size_t minimum = HASH_BYTES == 4 ? 4 : 8;
  while (at + minimum <= n) {
    Match best;
    if constexpr (CACHE_LAZY) {
      best = has_pending ? pending : find(at);
      has_pending = false;
    } else
      best = find(at);
    insert(at);
    if (best.length < 4) {
      ++at;
      continue;
    }
    if constexpr (LAZY != 0) {
      if (at + minimum + 1 <= n) {
        auto second = find(at + 1);
        if (second.length > best.length + LAZY) {
          if constexpr (CACHE_LAZY) {
            pending = second;
            has_pending = true;
          }
          ++at;
          continue;
        }
      }
    }
    size_t lit = at - anchor, len = best.length;
    tok.push_back(uint8_t((std::min<size_t>(lit, 15) << 4) |
                          std::min<size_t>(len - 4, 15)));
    if (lit >= 15)
      extput(lengths, lit - 15);
    literals.insert(literals.end(), p + anchor, p + at);
    varput(offsets, uint32_t(at - best.reference));
    if (len >= 19)
      extput(lengths, len - 19);
    size_t end = at + len;
    for (size_t j = at + 1; j + minimum <= end; j += INSERT_STEP)
      insert(j);
    at = end;
    anchor = at;
  }
  if (anchor < n) {
    size_t lit = n - anchor;
    tok.push_back(uint8_t(std::min<size_t>(lit, 15) << 4));
    if (lit >= 15)
      extput(lengths, lit - 15);
    literals.insert(literals.end(), p + anchor, p + n);
  }
  Bytes out;
  for (auto *stream : {&tok, &literals, &offsets, &lengths}) {
    auto z = entropy_encode(*stream);
    put32(out, uint32_t(z.size()));
    out.insert(out.end(), z.begin(), z.end());
  }
  return out;
}
struct StripeView {
  const uint8_t *p;
  size_t n;
};
// The transform is encoder-owned. An unsupported or inconsistent layout simply
// becomes one stripe, so this optional split cannot change its literal bytes.
inline std::vector<StripeView> column_stripes(const Bytes &prep) {
  auto single = [&]() { return std::vector<StripeView>{{prep.data(), prep.size()}}; };
  if (prep.size() < 24 || load32(prep.data()) != 0x314c434a || prep[4] != 2)
    return single();
  try {
    Reader r(prep.data(), prep.size());
    r.take(5);
    require(r.byte() <= 1 && r.byte() == 0 && r.byte() == 0, "bad stripe transform flags");
    uint64_t raw = r.u64();
    uint32_t rows = r.u32(), count = r.u32();
    require(raw <= (uint64_t(1) << 30) && rows && uint64_t(rows) * 2 <= raw && count <= 1024,
            "bad stripe transform dimensions");
    std::vector<size_t> lengths;
    lengths.reserve(count);
    size_t total = 0;
    for (uint32_t i = 0; i < count; ++i) {
      uint32_t schema = r.u32();
      r.take(schema);
      uint64_t data = r.u64(), packed = r.u64();
      uint8_t mode = r.byte();
      require(schema <= raw && data >= rows && data <= raw && mode <= 3 &&
                  (mode || packed == data) && packed && packed <= prep.size() - total,
              "bad stripe transform lengths");
      lengths.push_back(size_t(packed));
      total += size_t(packed);
    }
    require(total == prep.size() - r.pos, "inconsistent stripe transform payloads");
    std::vector<StripeView> stripes;
    stripes.reserve(size_t(count) + 1);
    stripes.push_back({prep.data(), r.pos});
    for (size_t length : lengths) stripes.push_back({r.take(length), length});
    r.end();
    return stripes;
  } catch (const std::runtime_error &) {
    return single();
  }
}
inline Bytes stripesencode(const Bytes &prep) {
  const auto stripes = column_stripes(prep);
  std::vector<Bytes> packed(stripes.size());
  std::vector<uint8_t> modes(stripes.size());
  parallel(stripes.size(), [&](size_t i) {
    const auto &s = stripes[i];
    Bytes lz = lzencode(s.p, s.n);
    Bytes direct = entropy_encode(s.p, s.n);
    if (lz.size() < s.n && lz.size() <= direct.size()) {
      packed[i] = std::move(lz);
      modes[i] = 1;
    } else if (direct.size() < s.n) {
      packed[i] = std::move(direct);
      modes[i] = 2;
    }
  });
  size_t size = 4 + stripes.size() * 9;
  for (size_t i = 0; i < stripes.size(); ++i)
    size += modes[i] ? packed[i].size() : stripes[i].n;
  Bytes out;
  out.reserve(size);
  put32(out, uint32_t(stripes.size()));
  for (size_t i = 0; i < stripes.size(); ++i) {
    const uint8_t *payload = modes[i] ? packed[i].data() : stripes[i].p;
    size_t n = modes[i] ? packed[i].size() : stripes[i].n;
    put32(out, uint32_t(stripes[i].n));
    put32(out, uint32_t(n));
    out.push_back(modes[i]);
    out.insert(out.end(), payload, payload + n);
  }
  return out;
}
inline Bytes encode(const Bytes &in) {
  require(in.size() <= BLOCK, "input object too large");
  Bytes out;
  if (in.empty()) {
    put32(out, 0x335a4c43);
    put64(out, 0);
    put32(out, 0);
    return out;
  }
  Bytes prep = columnar::forward_block(in.data(), in.size());
  Bytes z = stripesencode(prep);
  bool compressed = z.size() < prep.size();
  const Bytes &payload = compressed ? z : prep;
  out.reserve(37 + payload.size());
  put32(out, 0x335a4c43);
  put64(out, in.size());
  put32(out, 1);
  out.push_back(compressed ? 1 : 0);
  put32(out, uint32_t(in.size()));
  put32(out, uint32_t(prep.size()));
  put32(out, uint32_t(payload.size()));
  put64(out, checksum(in.data(), in.size()));
  out.insert(out.end(), payload.begin(), payload.end());
  return out;
}
} // namespace sw
