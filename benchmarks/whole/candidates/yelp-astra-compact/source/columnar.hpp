#pragma once
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <string_view>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#ifndef COLUMNAR_SCALARS
#define COLUMNAR_SCALARS 1
#endif

// Literal-preserving compact JSONL column transform. All schema bytes are in
// the transformed data; no corpus-specific keys or values live in the decoder.
namespace columnar {
using Bytes = std::vector<uint8_t>;

inline void put32(Bytes &out, uint32_t x) {
  for (unsigned i = 0; i < 4; ++i)
    out.push_back(uint8_t(x >> (i * 8)));
}
inline void put64(Bytes &out, uint64_t x) {
  for (unsigned i = 0; i < 8; ++i)
    out.push_back(uint8_t(x >> (i * 8)));
}
struct Reader {
  const uint8_t *p;
  size_t left;
  const uint8_t *take(size_t n) {
    if (n > left)
      throw std::runtime_error("truncated column transform");
    const uint8_t *result = p;
    if (n)
      p += n;
    left -= n;
    return result;
  }
  uint32_t u32() {
    const uint8_t *q = take(4);
    return uint32_t(q[0]) | (uint32_t(q[1]) << 8) | (uint32_t(q[2]) << 16) |
           (uint32_t(q[3]) << 24);
  }
  uint64_t u64() {
    const uint8_t *q = take(8);
    uint64_t x = 0;
    for (unsigned i = 0; i < 8; ++i)
      x |= uint64_t(q[i]) << (i * 8);
    return x;
  }
};

// The caller points at an opening quote. Escapes are copied literally, never
// decoded or normalized. A dangling escape or quote selects the raw fallback.
inline bool skip_string(const uint8_t *&p, const uint8_t *end) {
  ++p;
  while (p != end) {
    uint8_t c = *p++;
    if (c == '"')
      return true;
    if (c == '\\') {
      if (p == end)
        return false;
      ++p;
    }
  }
  return false;
}

inline bool skip_value(const uint8_t *&p, const uint8_t *end) {
  const uint8_t *start = p;
  uint8_t stack[64];
  unsigned depth = 0;
  while (p != end) {
    uint8_t c = *p;
    if (c == '"') {
      if (!skip_string(p, end))
        return false;
    } else if (c == '{' || c == '[') {
      if (depth == sizeof(stack))
        return false;
      stack[depth++] = c;
      ++p;
    } else if (c == '}' || c == ']') {
      if (depth == 0)
        break;
      if ((c == '}' && stack[depth - 1] != '{') ||
          (c == ']' && stack[depth - 1] != '['))
        return false;
      --depth;
      ++p;
    } else if (c == ',' && depth == 0) {
      break;
    } else {
      ++p;
    }
  }
  return p != start && depth == 0;
}

inline void varput(Bytes &out, uint32_t x) {
  while (x >= 128) {
    out.push_back(uint8_t(x) | 128);
    x >>= 7;
  }
  out.push_back(uint8_t(x));
}
inline uint32_t varget(Reader &r) {
  uint32_t x = 0;
  for (unsigned shift = 0; shift <= 28; shift += 7) {
    uint8_t c = *r.take(1);
    if (shift == 28 && c >= 16)
      throw std::runtime_error("dictionary varint overflow");
    x |= uint32_t(c & 127) << shift;
    if (!(c & 128)) {
      if (shift && c == 0)
        throw std::runtime_error("noncanonical dictionary varint");
      return x;
    }
  }
  throw std::runtime_error("dictionary varint overflow");
}
struct SliceHash {
  size_t operator()(std::string_view s) const {
    uint64_t h = 0x9e3779b185ebca87ULL ^ s.size();
    const char *p = s.data();
    size_t n = s.size();
    while (n >= 8) {
      uint64_t x;
      std::memcpy(&x, p, 8);
      h = (h ^ x) * 0xc2b2ae3d27d4eb4fULL;
      p += 8;
      n -= 8;
    }
    while (n--)
      h = (h ^ uint8_t(*p++)) * 0x100000001b3ULL;
    return size_t(h ^ (h >> 29));
  }
};

// Return an empty vector when this column is not a useful object dictionary.
// Dictionary strings reference the stable input column while they are interned.
inline Bytes pack_pairs(const Bytes &input, uint32_t rows) {
  const uint8_t *p = input.data();
  const uint8_t *end = p + input.size();
  while (end - p >= 5 && std::memcmp(p, "null\n", 5) == 0)
    p += 5;
  if (p == end || *p != '{')
    return {};
  p = input.data();
  std::unordered_map<std::string_view, uint32_t, SliceHash> dictionary;
  dictionary.reserve(4096);
  std::vector<std::string_view> entries;
  Bytes codes;
  codes.reserve(input.size() / 12);
  std::vector<uint32_t> row_ids;
  row_ids.reserve(64);
  size_t dictionary_bytes = 4;
  for (uint32_t row = 0; row < rows; ++row) {
    const uint8_t *stop =
        static_cast<const uint8_t *>(std::memchr(p, '\n', size_t(end - p)));
    if (!stop)
      throw std::runtime_error("internal missing column delimiter");
    const uint8_t *q = p;
    bool object = q != stop && *q++ == '{';
    row_ids.clear();
    if (object) {
      if (q == stop)
        object = false;
      while (object && *q != '}') {
        const uint8_t *member = q;
        if (*q != '"' || !skip_string(q, stop) || q == stop || *q++ != ':' ||
            !skip_value(q, stop) || q == stop) {
          object = false;
          break;
        }
        std::string_view text(reinterpret_cast<const char *>(member),
                              size_t(q - member));
        auto found = dictionary.find(text);
        uint32_t id;
        if (found == dictionary.end()) {
          if (entries.size() >= 1u << 20)
            return {};
          id = uint32_t(entries.size());
          dictionary.emplace(text, id);
          entries.push_back(text);
          dictionary_bytes += 4 + text.size();
        } else
          id = found->second;
        row_ids.push_back(id);
        if (*q == '}')
          break;
        if (*q++ != ',' || q == stop || *q != '"')
          object = false;
      }
      if (object && ++q != stop)
        object = false;
    }
    if (object) {
      varput(codes, uint32_t(row_ids.size()) + 1);
      for (uint32_t id : row_ids)
        varput(codes, id);
    } else {
      varput(codes, 0);
      varput(codes, uint32_t(stop - p));
      codes.insert(codes.end(), p, stop);
    }
    p = stop + 1;
    if (dictionary_bytes + codes.size() >= input.size())
      return {};
  }
  if (p != end)
    throw std::runtime_error("internal extra column rows");
  Bytes out;
  out.reserve(dictionary_bytes + codes.size());
  put32(out, uint32_t(entries.size()));
  for (std::string_view s : entries) {
    put32(out, uint32_t(s.size()));
    out.insert(out.end(), s.begin(), s.end());
  }
  out.insert(out.end(), codes.begin(), codes.end());
  return out;
}

// Sample only to reject likely-unique scalar columns or columns without lists.
// This affects compression selection, never reconstruction or exactness.
template <bool List>
inline bool text_candidate(const Bytes &input, uint32_t rows) {
  const uint8_t *p = input.data();
  const uint8_t *end = p + input.size();
  uint32_t sample = std::min(rows, uint32_t(List ? 64 : 256));
  std::unordered_set<std::string_view, SliceHash> unique;
  if constexpr (!List)
    unique.reserve(sample);
  unsigned lists = 0;
  for (uint32_t i = 0; i < sample; ++i) {
    const uint8_t *stop =
        static_cast<const uint8_t *>(std::memchr(p, '\n', size_t(end - p)));
    if (!stop)
      throw std::runtime_error("internal missing column delimiter");
    std::string_view s(reinterpret_cast<const char *>(p), size_t(stop - p));
    if constexpr (List) {
      if (s.size() >= 2 && s.front() == '"' && s.back() == '"' &&
          s.find(", ") != std::string_view::npos)
        ++lists;
    } else
      unique.insert(s);
    p = stop + 1;
  }
  if constexpr (List)
    return lists * 4 >= sample;
  else
    return unique.size() * 4 <= sample * 3;
}

// List payloads use the pair framing, with quoted strings and ", " separators.
// Scalar payloads instead encode one dictionary ID per row, with no row count.
template <bool List> inline Bytes pack_text(const Bytes &input, uint32_t rows) {
  if (!text_candidate<List>(input, rows))
    return {};
  const uint8_t *p = input.data();
  const uint8_t *end = p + input.size();
  std::unordered_map<std::string_view, uint32_t, SliceHash> dictionary;
  dictionary.reserve(4096);
  std::vector<std::string_view> entries;
  Bytes codes;
  codes.reserve(input.size() / 8);
  std::vector<uint32_t> row_ids;
  row_ids.reserve(32);
  size_t dictionary_bytes = 4;
  auto intern = [&](std::string_view s) {
    auto found = dictionary.find(s);
    if (found != dictionary.end())
      return found->second;
    uint32_t id = uint32_t(entries.size());
    dictionary.emplace(s, id);
    entries.push_back(s);
    dictionary_bytes += 4 + s.size();
    return id;
  };
  for (uint32_t row = 0; row < rows; ++row) {
    const uint8_t *stop =
        static_cast<const uint8_t *>(std::memchr(p, '\n', size_t(end - p)));
    if (!stop)
      throw std::runtime_error("internal missing column delimiter");
    std::string_view s(reinterpret_cast<const char *>(p), size_t(stop - p));
    if constexpr (List) {
      if (s.size() >= 2 && s.front() == '"' && s.back() == '"') {
        s.remove_prefix(1);
        s.remove_suffix(1);
        row_ids.clear();
        size_t start = 0;
        for (;;) {
          size_t next = s.find(", ", start);
          size_t length =
              next == std::string_view::npos ? s.size() - start : next - start;
          row_ids.push_back(intern(s.substr(start, length)));
          if (entries.size() > (1u << 20))
            return {};
          if (next == std::string_view::npos)
            break;
          start = next + 2;
        }
        varput(codes, uint32_t(row_ids.size()) + 1);
        for (uint32_t id : row_ids)
          varput(codes, id);
      } else {
        varput(codes, 0);
        varput(codes, uint32_t(s.size()));
        codes.insert(codes.end(), p, stop);
      }
    } else
      varput(codes, intern(s));
    if (entries.size() > (1u << 20) ||
        dictionary_bytes + codes.size() >= input.size())
      return {};
    p = stop + 1;
  }
  if (p != end)
    throw std::runtime_error("internal extra column rows");
  Bytes out;
  out.reserve(dictionary_bytes + codes.size());
  put32(out, uint32_t(entries.size()));
  for (std::string_view s : entries) {
    put32(out, uint32_t(s.size()));
    out.insert(out.end(), s.begin(), s.end());
  }
  out.insert(out.end(), codes.begin(), codes.end());
  return out;
}

inline Bytes unpack_dictionary(const uint8_t *src, size_t n, size_t raw,
                               uint32_t rows, uint8_t mode) {
  Reader r{src, n};
  uint32_t count = r.u32();
  if (count > (1u << 20) || count > r.left / 4 ||
      (mode == 1 ? count > raw / 4 : count > raw + 1))
    throw std::runtime_error("bad dictionary count");
  std::vector<std::string_view> dictionary;
  dictionary.reserve(count);
  for (uint32_t i = 0; i < count; ++i) {
    uint32_t size = r.u32();
    const uint8_t *text = r.take(size);
    if (size > raw || std::memchr(text, '\n', size))
      throw std::runtime_error("bad dictionary entry");
    dictionary.emplace_back(reinterpret_cast<const char *>(text), size);
  }
  Bytes out;
  out.reserve(raw);
  auto append = [&](const uint8_t *p, size_t size) {
    if (size > raw - out.size())
      throw std::runtime_error("dictionary output overflow");
    out.insert(out.end(), p, p + size);
  };
  auto byte = [&](uint8_t c) {
    if (out.size() == raw)
      throw std::runtime_error("dictionary output overflow");
    out.push_back(c);
  };
  for (uint32_t row = 0; row < rows; ++row) {
    uint32_t code = varget(r);
    if (mode == 3) {
      if (code >= dictionary.size())
        throw std::runtime_error("bad scalar dictionary ID");
      std::string_view s = dictionary[code];
      append(reinterpret_cast<const uint8_t *>(s.data()), s.size());
    } else if (code == 0) {
      uint32_t size = varget(r);
      const uint8_t *text = r.take(size);
      if (std::memchr(text, '\n', size))
        throw std::runtime_error("bad raw dictionary row");
      append(text, size);
    } else {
      uint32_t members = code - 1;
      if (members > r.left || members > raw)
        throw std::runtime_error("bad dictionary row count");
      byte(mode == 1 ? '{' : '"');
      for (uint32_t i = 0; i < members; ++i) {
        uint32_t id = varget(r);
        if (id >= dictionary.size())
          throw std::runtime_error("bad dictionary ID");
        if (i) {
          byte(',');
          if (mode == 2)
            byte(' ');
        }
        std::string_view s = dictionary[id];
        append(reinterpret_cast<const uint8_t *>(s.data()), s.size());
      }
      byte(mode == 1 ? '}' : '"');
    }
    byte('\n');
  }
  if (r.left || out.size() != raw)
    throw std::runtime_error("wrong dictionary output size");
  return out;
}

inline Bytes raw_block(const uint8_t *src, size_t n) {
  Bytes out;
  out.reserve(n + 5);
  out.insert(out.end(), {'J', 'C', 'L', '1', 0});
  if (n)
    out.insert(out.end(), src, src + n);
  return out;
}

// A block may end without newline. Every other row must use LF, share its
// literal schema and have exact {key:value,...} separators. Otherwise all bytes
// in the block are retained verbatim. The enclosing codec chooses line-aligned
// block sizes.
inline Bytes forward_block(const uint8_t *src, size_t n) {
  if (n == 0 || n > (uint64_t(1) << 30))
    return raw_block(src, n);
  std::vector<Bytes> schemas, columns;
  uint32_t rows = 0;
  const uint8_t *p = src;
  const uint8_t *end = src + n;
  while (p != end) {
    const uint8_t *line_end =
        static_cast<const uint8_t *>(std::memchr(p, '\n', size_t(end - p)));
    if (!line_end)
      line_end = end;
    if (p == line_end || *p++ != '{')
      return raw_block(src, n);
    size_t col = 0;
    if (p == line_end)
      return raw_block(src, n);
    while (*p != '}') {
      const uint8_t *key = p;
      if (*p != '"' || !skip_string(p, line_end) || p == line_end ||
          *p++ != ':')
        return raw_block(src, n);
      size_t key_len = size_t(p - key);
      if (rows == 0) {
        if (col == 1024)
          return raw_block(src, n);
        schemas.emplace_back(key, p);
        columns.emplace_back();
      } else if (col >= schemas.size() || schemas[col].size() != key_len ||
                 std::memcmp(schemas[col].data(), key, key_len) != 0) {
        return raw_block(src, n);
      }
      const uint8_t *value = p;
      if (!skip_value(p, line_end) || p == line_end)
        return raw_block(src, n);
      columns[col].insert(columns[col].end(), value, p);
      columns[col].push_back('\n');
      ++col;
      if (*p == '}')
        break;
      if (*p++ != ',' || p == line_end || *p != '"')
        return raw_block(src, n);
    }
    if (++p != line_end || col != schemas.size() ||
        rows == std::numeric_limits<uint32_t>::max())
      return raw_block(src, n);
    ++rows;
    p = line_end == end ? end : line_end + 1;
  }

  std::vector<Bytes> packed;
  packed.reserve(columns.size());
  std::vector<uint8_t> modes;
  modes.reserve(columns.size());
  size_t transformed_size = 24;
  for (const Bytes &column : columns) {
    Bytes payload = pack_pairs(column, rows);
    uint8_t mode = !payload.empty();
    if (payload.empty()) {
      payload = pack_text<true>(column, rows);
      if (!payload.empty())
        mode = 2;
    }
    if constexpr (COLUMNAR_SCALARS) {
      if (payload.empty()) {
        payload = pack_text<false>(column, rows);
        if (!payload.empty())
          mode = 3;
      }
    }
    modes.push_back(mode);
    packed.push_back(std::move(payload));
  }
  for (size_t i = 0; i < columns.size(); ++i)
    transformed_size +=
        21 + schemas[i].size() +
        (packed[i].empty() ? columns[i].size() : packed[i].size());
  if (transformed_size >= n + 5)
    return raw_block(src, n);
  Bytes out;
  out.reserve(transformed_size);
  out.insert(out.end(),
             {'J', 'C', 'L', '1', 2, uint8_t(src[n - 1] == '\n'), 0, 0});
  put64(out, n);
  put32(out, rows);
  put32(out, uint32_t(columns.size()));
  for (size_t i = 0; i < columns.size(); ++i) {
    put32(out, uint32_t(schemas[i].size()));
    out.insert(out.end(), schemas[i].begin(), schemas[i].end());
    put64(out, columns[i].size());
    put64(out, packed[i].empty() ? columns[i].size() : packed[i].size());
    out.push_back(modes[i]);
  }
  for (size_t i = 0; i < columns.size(); ++i) {
    const Bytes &data = packed[i].empty() ? columns[i] : packed[i];
    out.insert(out.end(), data.begin(), data.end());
  }
  return out;
}

inline Bytes
inverse_block(const uint8_t *src, size_t n,
              size_t expected_raw = std::numeric_limits<size_t>::max()) {
  Reader reader{src, n};
  const uint8_t *head = reader.take(5);
  if (std::memcmp(head, "JCL1", 4) != 0)
    throw std::runtime_error("bad column magic");
  if (head[4] == 0) {
    if (expected_raw != std::numeric_limits<size_t>::max() &&
        reader.left != expected_raw)
      throw std::runtime_error("wrong raw column output size");
    return Bytes(reader.p, reader.p + reader.left);
  }
  if (head[4] != 1 && head[4] != 2)
    throw std::runtime_error("bad column mode");
  const uint8_t *flags = reader.take(3);
  if (flags[0] > 1 || flags[1] || flags[2])
    throw std::runtime_error("bad column flags");
  const uint64_t raw_size = reader.u64();
  const uint32_t rows = reader.u32();
  const uint32_t count = reader.u32();
  // Every source row includes at least {}. The external archive format also
  // limits its total output; this transform caps a single block at 1 GiB.
  if ((expected_raw != std::numeric_limits<size_t>::max() &&
       raw_size != expected_raw) ||
      raw_size > (uint64_t(1) << 30) || rows == 0 ||
      uint64_t(rows) * 2 > raw_size || count > 1024)
    throw std::runtime_error("bad column dimensions");
  struct Column {
    const uint8_t *schema;
    size_t schema_size;
    size_t data_size;
    size_t packed_size;
    uint8_t mode;
    const uint8_t *p;
    const uint8_t *end;
  };
  std::vector<Column> columns;
  columns.reserve(count);
  uint64_t expected =
      uint64_t(rows) * (2 + (count ? count - 1 : 0)) + rows - !flags[0];
  for (uint32_t i = 0; i < count; ++i) {
    uint32_t schema_size = reader.u32();
    const uint8_t *schema = reader.take(schema_size);
    const uint64_t data_size = reader.u64();
    const uint64_t packed_size = head[4] == 2 ? reader.u64() : data_size;
    const uint8_t mode = head[4] == 2 ? *reader.take(1) : 0;
    if (schema_size > raw_size || data_size < rows || data_size > raw_size ||
        packed_size > reader.left || mode > 3 ||
        (mode == 0 && packed_size != data_size))
      throw std::runtime_error("bad column lengths");
    expected += uint64_t(rows) * schema_size + data_size - rows;
    if (expected > raw_size)
      throw std::runtime_error("inconsistent column lengths");
    columns.push_back({schema, schema_size, size_t(data_size),
                       size_t(packed_size), mode, nullptr, nullptr});
  }
  if (expected != raw_size)
    throw std::runtime_error("inconsistent column output size");
  std::vector<Bytes> unpacked(count);
  for (size_t i = 0; i < columns.size(); ++i) {
    Column &c = columns[i];
    c.p = reader.take(c.packed_size);
    if (c.mode) {
      unpacked[i] =
          unpack_dictionary(c.p, c.packed_size, c.data_size, rows, c.mode);
      c.p = unpacked[i].data();
    }
    c.end = c.p + c.data_size;
  }
  if (reader.left)
    throw std::runtime_error("trailing column bytes");
  Bytes out(size_t(raw_size), uint8_t(0));
  uint8_t *dest = out.data();
  uint8_t *dest_end = dest + out.size();
  for (uint32_t row = 0; row < rows; ++row) {
    if (dest == dest_end)
      throw std::runtime_error("column output overflow");
    *dest++ = '{';
    for (size_t i = 0; i < columns.size(); ++i) {
      Column &c = columns[i];
      const uint8_t *stop = static_cast<const uint8_t *>(
          std::memchr(c.p, '\n', size_t(c.end - c.p)));
      if (!stop)
        throw std::runtime_error("missing column delimiter");
      size_t len = size_t(stop - c.p);
      // The aggregate size was checked above, but malformed delimiter
      // positions still require a local check before any output write.
      if (len + c.schema_size + (i != 0) > size_t(dest_end - dest))
        throw std::runtime_error("column output overflow");
      if (i)
        *dest++ = ',';
      std::memcpy(dest, c.schema, c.schema_size);
      dest += c.schema_size;
      std::memcpy(dest, c.p, len);
      dest += len;
      c.p = stop + 1;
    }
    size_t suffix = 1 + (row + 1 < rows || flags[0]);
    if (suffix > size_t(dest_end - dest))
      throw std::runtime_error("column output overflow");
    *dest++ = '}';
    if (row + 1 < rows || flags[0])
      *dest++ = '\n';
  }
  for (const Column &c : columns)
    if (c.p != c.end)
      throw std::runtime_error("extra column values");
  if (dest != out.data() + out.size())
    throw std::runtime_error("wrong column output size");
  return out;
}

inline Bytes forward(const Bytes &input) {
  return forward_block(input.data(), input.size());
}
inline Bytes inverse(const Bytes &input,
                     size_t expected_raw = std::numeric_limits<size_t>::max()) {
  return inverse_block(input.data(), input.size(), expected_raw);
}
} // namespace columnar
