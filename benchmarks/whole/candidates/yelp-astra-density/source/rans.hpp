#pragma once

#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <memory>
#include <stdexcept>
#include <vector>

// From-scratch static, four-lane byte rANS. Algorithm: Jarek Duda,
// "Asymmetric numeral systems", https://arxiv.org/abs/1311.2540.
// This header uses no third-party codec implementation.
namespace entropy_detail {
constexpr uint32_t scale_bits = 12;
constexpr uint32_t scale = 1u << scale_bits;
constexpr uint32_t lower = 1u << 23;

inline void put16(std::vector<uint8_t> &v, uint32_t x) {
  v.push_back(uint8_t(x));
  v.push_back(uint8_t(x >> 8));
}
inline void put32(std::vector<uint8_t> &v, uint32_t x) {
  put16(v, x);
  put16(v, x >> 16);
}
inline uint32_t get16(const uint8_t *p) {
  return uint32_t(p[0]) | (uint32_t(p[1]) << 8);
}
inline uint32_t get32(const uint8_t *p) {
  return get16(p) | (get16(p + 2) << 16);
}
[[noreturn]] inline void invalid() {
  throw std::runtime_error("invalid entropy stream");
}
struct EncoderSymbol {
  uint64_t reciprocal;
  uint32_t frequency;
  uint32_t start;
};
struct DecoderSymbol {
  uint16_t frequency;
  uint16_t start;
  uint8_t symbol;
};
} // namespace entropy_detail

inline std::vector<uint8_t> entropy_encode(const uint8_t *input, size_t input_size) {
  using namespace entropy_detail;
  if (input_size > std::numeric_limits<uint32_t>::max())
    throw std::runtime_error("entropy input exceeds 32-bit length");
  const uint32_t n = uint32_t(input_size);
  auto raw = [&]() {
    std::vector<uint8_t> result;
    result.reserve(size_t(n) + 5);
    result.push_back(0);
    put32(result, n);
    if (n) result.insert(result.end(), input, input + n);
    return result;
  };
  if (n < 2)
    return raw();
  std::array<std::array<uint32_t, 256>, 4> partial{};
  size_t i = 0;
  for (; i + 4 <= input_size; i += 4) {
    ++partial[0][input[i]];
    ++partial[1][input[i + 1]];
    ++partial[2][input[i + 2]];
    ++partial[3][input[i + 3]];
  }
  for (; i < input_size; ++i)
    ++partial[0][input[i]];
  std::array<uint32_t, 256> counts;
  for (size_t c = 0; c < 256; ++c)
    counts[c] = partial[0][c] + partial[1][c] + partial[2][c] + partial[3][c];
  uint32_t symbols = 0, most = 0;
  for (uint32_t c = 0; c < 256; ++c) {
    symbols += counts[c] != 0;
    if (counts[c] > counts[most])
      most = c;
  }
  if (symbols == 1) {
    std::vector<uint8_t> result{2};
    put32(result, n);
    result.push_back(uint8_t(most));
    return result;
  }
  // Floor the measured probabilities, keeping every observed symbol.
  // Distribute rounding residue by signed largest remainder; if rare
  // symbols forced the sum upward, subtract from overallocated symbols.
  std::array<EncoderSymbol, 256> table{};
  std::array<int64_t, 256> error{};
  uint32_t sum = 0;
  for (uint32_t c = 0; c < 256; ++c) {
    if (!counts[c])
      continue;
    const uint64_t scaled = uint64_t(counts[c]) * scale;
    table[c].frequency = std::max(1u, uint32_t(scaled / n));
    error[c] = int64_t(scaled) - int64_t(table[c].frequency) * n;
    sum += table[c].frequency;
  }
  while (sum < scale) {
    uint32_t best = most;
    for (uint32_t c = 0; c < 256; ++c)
      if (counts[c] && error[c] > error[best])
        best = c;
    ++table[best].frequency;
    error[best] -= n;
    ++sum;
  }
  while (sum > scale) {
    uint32_t best = most;
    for (uint32_t c = 0; c < 256; ++c)
      if (table[c].frequency > 1 && error[c] < error[best])
        best = c;
    --table[best].frequency;
    error[best] += n;
    --sum;
  }
  // Skip the coding pass when even the normalized ideal cost cannot repay
  // table overhead. The final exact byte-size check handles close cases.
  double estimated_bytes = 7 + 3 * symbols + 16;
  for (uint32_t c = 0; c < 256; ++c)
    if (counts[c])
      estimated_bytes +=
          counts[c] * (scale_bits - std::log2(double(table[c].frequency))) / 8;
  if (estimated_bytes >= double(n) + 5)
    return raw();
  std::vector<uint8_t> result;
  result.reserve(size_t(n) + 5);
  result.push_back(1);
  put32(result, n);
  put16(result, symbols);
  uint32_t start = 0;
  for (uint32_t c = 0; c < 256; ++c) {
    auto &t = table[c];
    if (!t.frequency)
      continue;
    t.start = start;
    t.reciprocal = ((uint64_t(1) << 32) + t.frequency - 1) / t.frequency;
    start += t.frequency;
    result.push_back(uint8_t(c));
    put16(result, t.frequency);
  }
  // Every source byte can emit at most two bytes: minimum f=1,
  // 12-bit probability scale, and byte renormalization.
  if (size_t(n) > (std::numeric_limits<size_t>::max() - 16) / 2)
    throw std::runtime_error("entropy working allocation overflow");
  std::unique_ptr<uint8_t[]> reversed(new uint8_t[size_t(n) * 2]);
  uint8_t *end = reversed.get() + size_t(n) * 2;
  uint8_t *next = end;
  uint32_t states[4] = {lower, lower, lower, lower};
  for (size_t i = n; i-- != 0;) {
    const auto &t = table[input[i]];
    uint32_t x = states[i & 3];
    const uint32_t limit = ((lower >> scale_bits) << 8) * t.frequency;
    while (x >= limit) {
      *--next = uint8_t(x);
      x >>= 8;
    }
    // ceil(2^32/f) gives the exact quotient or one too many because
    // x<2^31. Correct it without a variable integer division.
    uint32_t q = uint32_t((uint64_t(x) * t.reciprocal) >> 32);
    q -= q * t.frequency > x;
    states[i & 3] = (q << scale_bits) + x - q * t.frequency + t.start;
  }
  if (result.size() + 16 + size_t(end - next) >= size_t(n) + 5)
    return raw();
  for (uint32_t x : states)
    put32(result, x);
  result.insert(result.end(), next, end);
  return result;
}

inline std::vector<uint8_t> entropy_encode(const std::vector<uint8_t> &input) {
  return entropy_encode(input.data(), input.size());
}

inline std::vector<uint8_t> entropy_decode(const uint8_t *data, size_t size,
                                           size_t expected_max) {
  using namespace entropy_detail;
  if (size < 5)
    invalid();
  const uint32_t n = get32(data + 1);
  if (size_t(n) > expected_max)
    invalid();
  if (data[0] == 0) {
    if (size - 5 != n)
      invalid();
    return std::vector<uint8_t>(data + 5, data + size);
  }
  if (data[0] == 2) {
    if (n < 2 || size != 6)
      invalid();
    return std::vector<uint8_t>(n, data[5]);
  }
  if (data[0] != 1 || n < 2 || size < 7)
    invalid();
  const uint32_t symbols = get16(data + 5);
  if (symbols < 2 || symbols > 256 || size < 7 + size_t(symbols) * 3 + 16)
    invalid();
  std::array<DecoderSymbol, scale> table;
  const uint8_t *p = data + 7;
  uint32_t start = 0;
  int previous = -1;
  for (uint32_t i = 0; i < symbols; ++i, p += 3) {
    const uint32_t c = p[0], f = get16(p + 1);
    if (int(c) <= previous || !f || f > scale - start)
      invalid();
    previous = int(c);
    const DecoderSymbol t{uint16_t(f), uint16_t(start), uint8_t(c)};
    for (uint32_t j = 0; j < f; ++j)
      table[start + j] = t;
    start += f;
  }
  if (start != scale)
    invalid();
  uint32_t states[4];
  for (uint32_t &x : states) {
    x = get32(p);
    p += 4;
    if (x < lower || x >= (lower << 8))
      invalid();
  }
  const uint8_t *end = data + size;
  std::vector<uint8_t> output(n);
  for (size_t i = 0; i < n; ++i) {
    uint32_t x = states[i & 3];
    const auto &t = table[x & (scale - 1)];
    output[i] = t.symbol;
    x = t.frequency * (x >> scale_bits) + (x & (scale - 1)) - t.start;
    while (x < lower) {
      if (p == end)
        invalid();
      x = (x << 8) | *p++;
    }
    states[i & 3] = x;
  }
  if (p != end)
    invalid();
  for (uint32_t x : states)
    if (x != lower)
      invalid();
  return output;
}
