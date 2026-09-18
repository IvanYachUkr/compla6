#include "common.hpp"
#if defined(TERRA_MAXIMUM) || defined(TERRA_LZ_HUFFMAN)
#include "huffman.hpp"
#endif
#ifdef TERRA_BWT
#include "bwt.hpp"
#endif

#include <algorithm>
#include <atomic>
#include <cstdlib>
#include <string_view>
#include <thread>
#include <unordered_map>

namespace {

using terra::Record;

#ifndef TERRA_BLOCK_LOG
#define TERRA_BLOCK_LOG 22
#endif
#ifndef TERRA_HASH_LOG
#define TERRA_HASH_LOG 20
#endif
#ifndef TERRA_LZ_HUFFMAN_MIN_BYTES
#define TERRA_LZ_HUFFMAN_MIN_BYTES 0
#endif
static_assert(TERRA_BLOCK_LOG >= 16 && TERRA_BLOCK_LOG <= 26, "invalid block log");
static_assert(TERRA_HASH_LOG >= 16 && TERRA_HASH_LOG <= 24, "invalid hash log");
static_assert(TERRA_LZ_HUFFMAN_MIN_BYTES >= 0, "invalid Huffman threshold");
constexpr size_t kBlockBytes = size_t{1} << TERRA_BLOCK_LOG;
constexpr unsigned kHashLog = TERRA_HASH_LOG;
constexpr unsigned kMinimumHashLog = 12;
constexpr uint32_t kNoPosition = 0xffffffffU;

struct EncodedBlock {
  uint32_t raw_size = 0;
  uint32_t checksum = 0;
  uint8_t kind = 0;  // 0 = raw, 1 = fixed-token LZ, 2 = Huffman-coded LZ bytes
  std::vector<uint8_t> data;
};

struct LzScratch {
  unsigned hash_log;
  std::vector<uint32_t> table;
  explicit LzScratch(size_t largest_block) : hash_log(kMinimumHashLog) {
    const size_t target = std::max(size_t{1} << kMinimumHashLog, (largest_block + 3) / 4);
    while (hash_log < kHashLog && (size_t{1} << hash_log) < target) ++hash_log;
    table.assign(size_t{1} << hash_log, kNoPosition);
  }
};

inline uint32_t load32(const uint8_t* p) {
  return uint32_t(p[0]) | (uint32_t(p[1]) << 8) | (uint32_t(p[2]) << 16) |
         (uint32_t(p[3]) << 24);
}

inline uint64_t load64(const uint8_t* p) {
  uint64_t value;
  std::memcpy(&value, p, sizeof(value));
  return value;
}

inline size_t hash4(uint32_t value, const LzScratch& scratch) {
  return (value * 2654435761U) >> (32 - scratch.hash_log);
}

void append_lz_length(std::vector<uint8_t>& out, size_t length) {
  while (length >= 255) {
    out.push_back(255);
    length -= 255;
  }
  out.push_back(static_cast<uint8_t>(length));
}

std::vector<uint8_t> compress_lz(const uint8_t* input, size_t size, LzScratch& scratch) {
  std::fill(scratch.table.begin(), scratch.table.end(), kNoPosition);
  std::vector<uint8_t> out;
  out.reserve(size + size / 255 + 32);
  size_t anchor = 0;
  size_t pos = 0;
  while (pos + 4 <= size) {
    const uint32_t sequence = load32(input + pos);
    const size_t slot = hash4(sequence, scratch);
    const uint32_t previous = scratch.table[slot];
    scratch.table[slot] = static_cast<uint32_t>(pos);
    if (previous == kNoPosition || pos - previous > 65535 ||
        load32(input + previous) != sequence) {
      ++pos;
      continue;
    }

    size_t match_length = 4;
    while (pos + match_length + 8 <= size &&
           load64(input + previous + match_length) == load64(input + pos + match_length)) {
      match_length += 8;
    }
    while (pos + match_length < size && input[previous + match_length] == input[pos + match_length]) {
      ++match_length;
    }

    const size_t literal_length = pos - anchor;
    const size_t match_extra = match_length - 4;
    const size_t token_position = out.size();
    out.push_back(0);
    if (literal_length >= 15) append_lz_length(out, literal_length - 15);
    out.insert(out.end(), input + anchor, input + pos);
    const uint32_t distance = static_cast<uint32_t>(pos - previous);
    out.push_back(static_cast<uint8_t>(distance));
    out.push_back(static_cast<uint8_t>(distance >> 8));
    if (match_extra >= 15) append_lz_length(out, match_extra - 15);
    out[token_position] = static_cast<uint8_t>((std::min<size_t>(literal_length, 15) << 4) |
                                               std::min<size_t>(match_extra, 15));

    const size_t end = pos + match_length;
    for (size_t update = pos + 1; update + 4 <= end; ++update) {
      scratch.table[hash4(load32(input + update), scratch)] = static_cast<uint32_t>(update);
    }
    pos = end;
    anchor = pos;
  }
  const size_t literal_length = size - anchor;
  if (literal_length != 0 || out.empty()) {
    out.push_back(static_cast<uint8_t>(std::min<size_t>(literal_length, 15) << 4));
    if (literal_length >= 15) append_lz_length(out, literal_length - 15);
    out.insert(out.end(), input + anchor, input + size);
  }
  return out;
}

#ifdef TERRA_LZ_HUFFMAN
// This E1 comparator entropy-codes the already-produced fixed-token byte
// stream. It deliberately keeps LZ parsing identical to the fixed baseline.
std::vector<uint8_t> compress_lz_huffman(const std::vector<uint8_t>& fixed) {
  terra::require(!fixed.empty() && fixed.size() <= std::numeric_limits<uint32_t>::max(),
                 "invalid fixed LZ stream");
  std::vector<uint64_t> weights(256, 0);
  for (uint8_t byte : fixed) ++weights[byte];
  const std::vector<uint8_t> lengths = terra::huffman_lengths(weights);
  const terra::HuffmanTable table = terra::canonical_huffman(lengths);
  terra::HuffmanWriter writer;
  for (uint8_t byte : fixed) writer.put(table.codes[byte]);
  std::vector<uint8_t> entropy = writer.finish();
  std::vector<uint8_t> packed;
  packed.reserve(4 + lengths.size() + entropy.size());
  terra::append_u32(packed, static_cast<uint32_t>(fixed.size()));
  packed.insert(packed.end(), lengths.begin(), lengths.end());
  packed.insert(packed.end(), entropy.begin(), entropy.end());
  return packed;
}
#endif

EncodedBlock encode_block(const uint8_t* input, size_t size, LzScratch& scratch) {
  terra::require(size <= std::numeric_limits<uint32_t>::max(), "block too large");
  EncodedBlock block;
  block.raw_size = static_cast<uint32_t>(size);
  block.checksum = terra::crc32(input, size);
  auto packed = compress_lz(input, size, scratch);
#ifdef TERRA_LZ_HUFFMAN
  if (packed.size() >= TERRA_LZ_HUFFMAN_MIN_BYTES) {
    std::vector<uint8_t> huffman = compress_lz_huffman(packed);
    if (huffman.size() < packed.size() && huffman.size() < size) {
      block.kind = 2;
      block.data = std::move(huffman);
      return block;
    }
  }
#endif
  if (packed.size() < size) {
    block.kind = 1;
    block.data = std::move(packed);
  } else {
    block.kind = 0;
    block.data.assign(input, input + size);
  }
  return block;
}

unsigned configured_threads() {
  const char* text = std::getenv("COMPRESSION_LAB_THREADS");
  if (text == nullptr || *text == '\0') return 1;
  char* end = nullptr;
  const unsigned long parsed = std::strtoul(text, &end, 10);
  if (*end != '\0' || parsed == 0) return 1;
  return static_cast<unsigned>(std::min<unsigned long>(parsed, 4));
}

std::vector<uint8_t> encode_lz(const std::vector<uint8_t>& input) {
  const size_t block_count_size = (input.size() + kBlockBytes - 1) / kBlockBytes;
  terra::require(block_count_size <= std::numeric_limits<uint32_t>::max(), "too many blocks");
  std::vector<EncodedBlock> blocks(block_count_size);
  std::atomic<size_t> next{0};
  const unsigned worker_count = std::min<unsigned>(configured_threads(),
                                                    static_cast<unsigned>(block_count_size));
  const auto worker = [&] {
    LzScratch scratch(std::min(kBlockBytes, input.size()));
    for (;;) {
      const size_t index = next.fetch_add(1, std::memory_order_relaxed);
      if (index >= block_count_size) break;
      const size_t offset = index * kBlockBytes;
      const size_t length = std::min(kBlockBytes, input.size() - offset);
      blocks[index] = encode_block(input.data() + offset, length, scratch);
    }
  };
  std::vector<std::thread> helpers;
  for (unsigned i = 1; i < worker_count; ++i) helpers.emplace_back(worker);
  if (worker_count != 0) worker();
  for (auto& helper : helpers) helper.join();

  std::vector<uint8_t> archive;
  size_t total = 22;
  for (const auto& block : blocks) {
    terra::require(block.data.size() <= std::numeric_limits<uint32_t>::max(), "compressed block too large");
    terra::require(block.data.size() <= std::numeric_limits<size_t>::max() - total - 13,
                   "archive too large");
    total += 13 + block.data.size();
  }
  archive.reserve(total);
#ifdef TERRA_LZ_HUFFMAN
  archive.insert(archive.end(), {'T', 'L', 'H', '1'});
#else
  archive.insert(archive.end(), {'T', 'L', 'Z', '2'});
#endif
  archive.push_back(1);
  archive.push_back(1);  // generic block LZ mode
  terra::append_u64(archive, input.size());
  terra::append_u32(archive, static_cast<uint32_t>(kBlockBytes));
  terra::append_u32(archive, static_cast<uint32_t>(blocks.size()));
  for (const auto& block : blocks) {
    terra::append_u32(archive, block.raw_size);
    terra::append_u32(archive, static_cast<uint32_t>(block.data.size()));
    terra::append_u32(archive, block.checksum);
    archive.push_back(block.kind);
    archive.insert(archive.end(), block.data.begin(), block.data.end());
  }
  // The block CRCs validate reconstructed bytes. This trailer also authenticates
  // framing and compressed representation, so a changed but coincidentally
  // decodable metadata byte cannot be accepted as a valid archive.
  terra::append_u32(archive, terra::crc32(archive));
  return archive;
}

constexpr size_t kJsonFields = 14;

struct Span {
  const uint8_t* begin;
  const uint8_t* end;
};

bool json_space(uint8_t value) {
  return value == ' ' || value == '\t' || value == '\r' || value == '\n';
}

void skip_json_space(const uint8_t*& p, const uint8_t* end) {
  while (p != end && json_space(*p)) ++p;
}

bool hex_digit(uint8_t value) {
  return (value >= '0' && value <= '9') || (value >= 'a' && value <= 'f') ||
         (value >= 'A' && value <= 'F');
}

bool skip_json_string(const uint8_t*& p, const uint8_t* end) {
  if (p == end || *p++ != '"') return false;
  while (p != end) {
    const uint8_t value = *p++;
    if (value == '"') return true;
    if (value < 0x20) return false;
    if (value != '\\') continue;
    if (p == end) return false;
    const uint8_t escaped = *p++;
    if (escaped == 'u') {
      if (size_t(end - p) < 4 || !hex_digit(p[0]) || !hex_digit(p[1]) ||
          !hex_digit(p[2]) || !hex_digit(p[3])) return false;
      p += 4;
    } else if (escaped != '"' && escaped != '\\' && escaped != '/' && escaped != 'b' &&
               escaped != 'f' && escaped != 'n' && escaped != 'r' && escaped != 't') {
      return false;
    }
  }
  return false;
}

bool skip_json_value(const uint8_t*& p, const uint8_t* end, unsigned depth) {
  if (p == end || depth == 0) return false;
  if (*p == '"') return skip_json_string(p, end);
  if (*p == '{') {
    ++p;
    skip_json_space(p, end);
    if (p != end && *p == '}') {
      ++p;
      return true;
    }
    for (;;) {
      if (!skip_json_string(p, end)) return false;
      skip_json_space(p, end);
      if (p == end || *p++ != ':') return false;
      skip_json_space(p, end);
      if (!skip_json_value(p, end, depth - 1)) return false;
      skip_json_space(p, end);
      if (p == end) return false;
      if (*p == '}') {
        ++p;
        return true;
      }
      if (*p++ != ',') return false;
      skip_json_space(p, end);
    }
  }
  if (*p == '[') {
    ++p;
    skip_json_space(p, end);
    if (p != end && *p == ']') {
      ++p;
      return true;
    }
    for (;;) {
      if (!skip_json_value(p, end, depth - 1)) return false;
      skip_json_space(p, end);
      if (p == end) return false;
      if (*p == ']') {
        ++p;
        return true;
      }
      if (*p++ != ',') return false;
      skip_json_space(p, end);
    }
  }
  const uint8_t* start = p;
  while (p != end && !json_space(*p) && *p != ',' && *p != ']' && *p != '}') {
    if (*p < 0x20 || *p == '{' || *p == '[' || *p == '"' || *p == ':') return false;
    ++p;
  }
  return p != start;
}

bool parse_json_record(const uint8_t* begin, const uint8_t* end,
                       std::array<Span, kJsonFields>& values) {
  const uint8_t* p = begin;
  skip_json_space(p, end);
  if (p == end || *p++ != '{') return false;
  skip_json_space(p, end);
  size_t count = 0;
  while (p != end && *p != '}') {
    if (count == values.size() || !skip_json_string(p, end)) return false;
    skip_json_space(p, end);
    if (p == end || *p++ != ':') return false;
    skip_json_space(p, end);
    values[count].begin = p;
    if (!skip_json_value(p, end, 128)) return false;
    values[count].end = p;
    ++count;
    skip_json_space(p, end);
    if (p == end) return false;
    if (*p == '}') break;
    if (*p++ != ',') return false;
    skip_json_space(p, end);
  }
  if (p == end || *p++ != '}') return false;
  skip_json_space(p, end);
  return p == end && count == values.size();
}

bool same_bytes(const std::vector<uint8_t>& expected, const uint8_t* begin, const uint8_t* end) {
  return expected.size() == size_t(end - begin) &&
         (expected.empty() || std::memcmp(expected.data(), begin, expected.size()) == 0);
}

#ifndef TERRA_LEAN_FAST
bool encode_structural_json(const std::vector<uint8_t>& input, std::vector<uint8_t>& archive) {
  if (input.empty()) return false;
  std::array<std::vector<uint8_t>, kJsonFields + 1> fragments;
#ifdef TERRA_ROW_MAJOR_COLUMNS
  std::vector<uint8_t> row_major;
  row_major.reserve(input.size() / 2);
#else
  std::array<std::vector<uint8_t>, kJsonFields> columns;
  for (auto& column : columns) column.reserve(input.size() / kJsonFields);
#endif
  const uint8_t* const base = input.data();
  const uint8_t* const end = base + input.size();
  const uint8_t* line = base;
  uint32_t records = 0;
  while (line != end) {
    const void* newline = std::memchr(line, '\n', size_t(end - line));
    if (newline == nullptr || records == std::numeric_limits<uint32_t>::max()) return false;
    const uint8_t* line_end = static_cast<const uint8_t*>(newline) + 1;
    std::array<Span, kJsonFields> values{};
    if (!parse_json_record(line, line_end, values)) return false;
    if (records == 0) {
      fragments[0].assign(line, values[0].begin);
      for (size_t field = 1; field < kJsonFields; ++field) {
        fragments[field].assign(values[field - 1].end, values[field].begin);
      }
      fragments[kJsonFields].assign(values.back().end, line_end);
    } else {
      if (!same_bytes(fragments[0], line, values[0].begin)) return false;
      for (size_t field = 1; field < kJsonFields; ++field) {
        if (!same_bytes(fragments[field], values[field - 1].end, values[field].begin)) return false;
      }
      if (!same_bytes(fragments[kJsonFields], values.back().end, line_end)) return false;
    }
    for (size_t field = 0; field < kJsonFields; ++field) {
#ifdef TERRA_ROW_MAJOR_COLUMNS
      row_major.insert(row_major.end(), values[field].begin, values[field].end);
      row_major.push_back(0);
#else
      columns[field].insert(columns[field].end(), values[field].begin, values[field].end);
      columns[field].push_back(0);
#endif
    }
    ++records;
    line = line_end;
  }
  if (records == 0) return false;

#ifdef TERRA_ROW_MAJOR_COLUMNS
  std::vector<uint8_t> packed = encode_lz(row_major);
  terra::require(packed.size() <= std::numeric_limits<uint32_t>::max(),
                 "row-major structural stream too large");
  size_t total = 4 + 1 + 1 + 8 + 4 + 4 * fragments.size() + 4 + packed.size();
  for (const auto& fragment : fragments) total += fragment.size();
  archive.clear();
  archive.reserve(total);
  archive.insert(archive.end(), {'T', 'S', 'J', '0'});
  archive.push_back(1);
  archive.push_back(static_cast<uint8_t>(kJsonFields));
  terra::append_u64(archive, input.size());
  terra::append_u32(archive, records);
  for (const auto& fragment : fragments) {
    terra::require(fragment.size() <= std::numeric_limits<uint32_t>::max(), "fragment too large");
    terra::append_u32(archive, static_cast<uint32_t>(fragment.size()));
  }
  for (const auto& fragment : fragments) archive.insert(archive.end(), fragment.begin(), fragment.end());
  terra::append_u32(archive, static_cast<uint32_t>(packed.size()));
  archive.insert(archive.end(), packed.begin(), packed.end());
  terra::append_u32(archive, terra::crc32(archive));
  return true;
#else
  std::array<std::vector<uint8_t>, kJsonFields> packed_columns;
  size_t total = 4 + 1 + 1 + 8 + 4 + 4 * fragments.size() + 4;
  for (const auto& fragment : fragments) total += fragment.size();
  for (size_t field = 0; field < kJsonFields; ++field) {
    packed_columns[field] = encode_lz(columns[field]);
    terra::require(packed_columns[field].size() <= std::numeric_limits<uint32_t>::max(),
                   "structural column too large");
    terra::require(packed_columns[field].size() <= std::numeric_limits<size_t>::max() - total - 4,
                   "structural archive too large");
    total += 4 + packed_columns[field].size();
  }
  archive.clear();
  archive.reserve(total);
  archive.insert(archive.end(), {'T', 'S', 'J', '1'});
  archive.push_back(1);
  archive.push_back(static_cast<uint8_t>(kJsonFields));
  terra::append_u64(archive, input.size());
  terra::append_u32(archive, records);
  for (const auto& fragment : fragments) {
    terra::require(fragment.size() <= std::numeric_limits<uint32_t>::max(), "fragment too large");
    terra::append_u32(archive, static_cast<uint32_t>(fragment.size()));
  }
  for (const auto& fragment : fragments) archive.insert(archive.end(), fragment.begin(), fragment.end());
  for (const auto& column : packed_columns) {
    terra::append_u32(archive, static_cast<uint32_t>(column.size()));
    archive.insert(archive.end(), column.begin(), column.end());
  }
  terra::append_u32(archive, terra::crc32(archive));
  return true;
#endif
}
#endif

struct OuterRows {
  std::array<std::vector<uint8_t>, kJsonFields + 1> fragments;
  std::vector<std::array<Span, kJsonFields>> rows;
};

bool parse_outer_rows(const std::vector<uint8_t>& input, OuterRows& layout) {
  if (input.empty()) return false;
  for (auto& fragment : layout.fragments) fragment.clear();
  layout.rows.clear();
  layout.rows.reserve(std::min<size_t>(input.size() / 512, 1000000));
  const uint8_t* const base = input.data();
  const uint8_t* const end = base + input.size();
  const uint8_t* line = base;
  while (line != end) {
    const void* newline = std::memchr(line, '\n', size_t(end - line));
    if (newline == nullptr || layout.rows.size() == 1000000) return false;
    const uint8_t* line_end = static_cast<const uint8_t*>(newline) + 1;
    std::array<Span, kJsonFields> values{};
    if (!parse_json_record(line, line_end, values)) return false;
    if (layout.rows.empty()) {
      layout.fragments[0].assign(line, values[0].begin);
      for (size_t field = 1; field < kJsonFields; ++field) {
        layout.fragments[field].assign(values[field - 1].end, values[field].begin);
      }
      layout.fragments[kJsonFields].assign(values.back().end, line_end);
    } else {
      if (!same_bytes(layout.fragments[0], line, values[0].begin)) return false;
      for (size_t field = 1; field < kJsonFields; ++field) {
        if (!same_bytes(layout.fragments[field], values[field - 1].end, values[field].begin)) return false;
      }
      if (!same_bytes(layout.fragments[kJsonFields], values.back().end, line_end)) return false;
    }
    layout.rows.push_back(values);
    line = line_end;
  }
  return !layout.rows.empty();
}

struct AttributeEntry {
  Span key;
  Span value;
};

bool parse_attributes(const Span& attributes, bool& is_null, std::vector<AttributeEntry>& entries) {
  entries.clear();
  if (size_t(attributes.end - attributes.begin) == 4 &&
      std::memcmp(attributes.begin, "null", 4) == 0) {
    is_null = true;
    return true;
  }
  is_null = false;
  const uint8_t* p = attributes.begin;
  if (p == attributes.end || *p++ != '{') return false;
  if (p != attributes.end && *p == '}') return ++p == attributes.end;
  while (p != attributes.end) {
    const uint8_t* key_begin = p;
    if (!skip_json_string(p, attributes.end)) return false;
    const uint8_t* key_end = p;
    if (p == attributes.end || *p++ != ':') return false;
    const uint8_t* value_begin = p;
    if (!skip_json_value(p, attributes.end, 128)) return false;
    const uint8_t* value_end = p;
    entries.push_back({{key_begin, key_end}, {value_begin, value_end}});
    if (p == attributes.end) return false;
    if (*p == '}') return ++p == attributes.end;
    if (*p++ != ',') return false;
  }
  return false;
}

struct StringViewHash {
  size_t operator()(std::string_view value) const noexcept {
    uint64_t hash = 1469598103934665603ULL;
    for (unsigned char byte : value) {
      hash ^= byte;
      hash *= 1099511628211ULL;
    }
    return static_cast<size_t>(hash);
  }
};

#ifdef TERRA_MAXIMUM

bool byte_view_less(std::string_view left, std::string_view right) {
  const size_t common = std::min(left.size(), right.size());
  for (size_t i = 0; i < common; ++i) {
    const uint8_t a = static_cast<uint8_t>(left[i]);
    const uint8_t b = static_cast<uint8_t>(right[i]);
    if (a != b) return a < b;
  }
  return left.size() < right.size();
}

std::string_view span_view(const Span& span) {
  return std::string_view(reinterpret_cast<const char*>(span.begin),
                          static_cast<size_t>(span.end - span.begin));
}

struct StringDictionary {
  struct Entry {
    std::string_view value;
    uint32_t count;
  };

  std::vector<Entry> entries;
  std::unordered_map<std::string_view, uint32_t, StringViewHash> ids;

  void add(std::string_view value) {
    const auto found = ids.find(value);
    if (found == ids.end()) {
      terra::require(entries.size() < std::numeric_limits<uint32_t>::max(), "too many dictionary values");
      const uint32_t id = static_cast<uint32_t>(entries.size());
      ids.emplace(value, id);
      entries.push_back({value, 1});
    } else {
      terra::require(entries[found->second].count != std::numeric_limits<uint32_t>::max(),
                     "dictionary frequency overflow");
      ++entries[found->second].count;
    }
  }

  void finalize_frequency() {
#ifdef TERRA_DICTIONARY_LEXICAL
    std::sort(entries.begin(), entries.end(), [](const Entry& left, const Entry& right) {
      return byte_view_less(left.value, right.value);
    });
#else
    std::sort(entries.begin(), entries.end(), [](const Entry& left, const Entry& right) {
      if (left.count != right.count) return left.count > right.count;
      return byte_view_less(left.value, right.value);
    });
#endif
    rebuild_ids();
  }

  void finalize_lexical() {
    std::sort(entries.begin(), entries.end(), [](const Entry& left, const Entry& right) {
      return byte_view_less(left.value, right.value);
    });
    rebuild_ids();
  }

  uint32_t id(std::string_view value) const {
    const auto found = ids.find(value);
    terra::require(found != ids.end(), "dictionary value missing");
    return found->second;
  }

 private:
  void rebuild_ids() {
    ids.clear();
    ids.reserve(entries.size() * 2 + 1);
    for (uint32_t index = 0; index < entries.size(); ++index) {
      const bool inserted = ids.emplace(entries[index].value, index).second;
      terra::require(inserted, "duplicate dictionary value");
    }
  }
};

void append_dictionary(std::vector<uint8_t>& out, const StringDictionary& dictionary) {
  terra::require(dictionary.entries.size() <= std::numeric_limits<uint32_t>::max(),
                 "dictionary too large");
  const uint32_t count = static_cast<uint32_t>(dictionary.entries.size());
#ifdef TERRA_BWT
  std::vector<uint8_t> body;
  size_t raw_size = 4;
  for (const auto& entry : dictionary.entries) {
    terra::require(entry.value.size() <= std::numeric_limits<uint32_t>::max(),
                   "dictionary token too large");
    terra::require(entry.value.size() <= std::numeric_limits<size_t>::max() - body.size(),
                   "dictionary body too large");
    raw_size += 4 + entry.value.size();
    body.insert(body.end(), entry.value.begin(), entry.value.end());
  }
  // The high count bit selects a length table followed by a TBW1 body. Keep
  // raw entries for short or incompressible dictionaries as the universal
  // escape, so dictionary fitting never expands an arbitrary input needlessly.
  if (body.size() >= 4096) {
    std::vector<uint8_t> packed = terra::encode_bwt_object(body, configured_threads());
    const size_t bwt_size = 4 + size_t(count) * 4 + 4 + packed.size();
    if (bwt_size < raw_size) {
      terra::append_u32(out, count | 0x80000000U);
      for (const auto& entry : dictionary.entries) {
        terra::append_u32(out, static_cast<uint32_t>(entry.value.size()));
      }
      terra::require(packed.size() <= std::numeric_limits<uint32_t>::max(),
                     "compressed dictionary too large");
      terra::append_u32(out, static_cast<uint32_t>(packed.size()));
      out.insert(out.end(), packed.begin(), packed.end());
      return;
    }
  }
#endif
  terra::append_u32(out, count);
  for (const auto& entry : dictionary.entries) {
    terra::require(entry.value.size() <= std::numeric_limits<uint32_t>::max(),
                   "dictionary token too large");
    terra::append_u32(out, static_cast<uint32_t>(entry.value.size()));
    out.insert(out.end(), entry.value.begin(), entry.value.end());
  }
}

struct PatternDictionary {
  std::vector<std::vector<uint8_t>> entries;
  std::unordered_map<std::string, uint32_t> ids;

  static std::string key(const std::vector<uint8_t>& value) {
    return std::string(reinterpret_cast<const char*>(value.data()), value.size());
  }

  void add(const std::vector<uint8_t>& value) {
    const std::string text = key(value);
    if (ids.find(text) != ids.end()) return;
    terra::require(entries.size() < std::numeric_limits<uint32_t>::max(), "too many patterns");
    const uint32_t id = static_cast<uint32_t>(entries.size());
    ids.emplace(text, id);
    entries.push_back(value);
  }

  void finalize() {
    std::sort(entries.begin(), entries.end());
    ids.clear();
    ids.reserve(entries.size() * 2 + 1);
    for (uint32_t index = 0; index < entries.size(); ++index) {
      const bool inserted = ids.emplace(key(entries[index]), index).second;
      terra::require(inserted, "duplicate pattern");
    }
  }

  uint32_t id(const std::vector<uint8_t>& value) const {
    const auto found = ids.find(key(value));
    terra::require(found != ids.end(), "pattern missing");
    return found->second;
  }
};

bool is_null_token(const Span& value) {
  return static_cast<size_t>(value.end - value.begin) == 4 &&
         std::memcmp(value.begin, "null", 4) == 0;
}

bool parse_categories(const Span& categories, bool& is_null, std::vector<Span>& labels) {
  labels.clear();
  if (is_null_token(categories)) {
    is_null = true;
    return true;
  }
  is_null = false;
  if (categories.end - categories.begin < 2 || categories.begin[0] != '"' ||
      categories.end[-1] != '"') return false;
  const uint8_t* checked = categories.begin;
  if (!skip_json_string(checked, categories.end) || checked != categories.end) return false;
  const uint8_t* begin = categories.begin + 1;
  const uint8_t* const end = categories.end - 1;
  const uint8_t* part = begin;
  for (const uint8_t* p = begin; p < end; ++p) {
    if (*p == ',' && p + 1 < end && p[1] == ' ') {
      labels.push_back({part, p});
      part = p + 2;
      ++p;
    }
  }
  labels.push_back({part, end});
  return true;
}

bool append_packed_business_id(const Span& token, std::vector<uint8_t>& output) {
  if (token.end - token.begin != 24 || token.begin[0] != '"' || token.end[-1] != '"') return false;
  uint32_t pending = 0;
  unsigned pending_bits = 0;
  const size_t before = output.size();
  for (const uint8_t* p = token.begin + 1; p != token.end - 1; ++p) {
    uint8_t value;
    if (*p >= 'A' && *p <= 'Z') value = static_cast<uint8_t>(*p - 'A');
    else if (*p >= 'a' && *p <= 'z') value = static_cast<uint8_t>(*p - 'a' + 26);
    else if (*p >= '0' && *p <= '9') value = static_cast<uint8_t>(*p - '0' + 52);
    else if (*p == '-') value = 62;
    else if (*p == '_') value = 63;
    else {
      output.resize(before);
      return false;
    }
    pending = (pending << 6) | value;
    pending_bits += 6;
    while (pending_bits >= 8) {
      pending_bits -= 8;
      output.push_back(static_cast<uint8_t>(pending >> pending_bits));
      pending &= (uint32_t{1} << pending_bits) - 1;
    }
  }
  if (pending_bits != 4 || pending != 0 || output.size() != before + 16) {
    output.resize(before);
    return false;
  }
  return true;
}

std::vector<uint8_t> make_raw_column(const OuterRows& layout, size_t field) {
  std::vector<uint8_t> column;
  column.reserve(layout.rows.size() * 8);
  for (const auto& row : layout.rows) {
    column.insert(column.end(), row[field].begin, row[field].end);
    column.push_back(0);
  }
  return column;
}

#ifdef TERRA_ROW_REORDER
#ifndef TERRA_ROW_ORDER_VARIANT
#define TERRA_ROW_ORDER_VARIANT 0
#endif
#if TERRA_ROW_ORDER_VARIANT == 0
constexpr std::array<size_t, 6> kRowSortFields = {4, 3, 5, 12, 11, 13};
#elif TERRA_ROW_ORDER_VARIANT == 1
constexpr std::array<size_t, 3> kRowSortFields = {4, 3, 5};
#elif TERRA_ROW_ORDER_VARIANT == 2
constexpr std::array<size_t, 5> kRowSortFields = {4, 3, 5, 6, 7};
#elif TERRA_ROW_ORDER_VARIANT == 3
constexpr std::array<size_t, 4> kRowSortFields = {4, 3, 5, 2};
#elif TERRA_ROW_ORDER_VARIANT == 4
constexpr std::array<size_t, 5> kRowSortFields = {4, 3, 5, 2, 1};
#elif TERRA_ROW_ORDER_VARIANT == 5
constexpr std::array<size_t, 7> kRowSortFields = {4, 3, 5, 2, 1, 6, 7};
#elif TERRA_ROW_ORDER_VARIANT == 6
constexpr std::array<size_t, 7> kRowSortFields = {4, 3, 5, 6, 7, 2, 1};
#elif TERRA_ROW_ORDER_VARIANT == 7
constexpr std::array<size_t, 5> kRowSortFields = {4, 3, 5, 1, 2};
#elif TERRA_ROW_ORDER_VARIANT == 8
constexpr std::array<size_t, 7> kRowSortFields = {4, 3, 5, 2, 6, 7, 1};
#else
#error "unsupported row-order variant"
#endif

// The permutation maps each encoded (sorted) row to its original ordinal.
// Sorting spans does not copy any corpus bytes; the spans still point into the
// input kept alive for the entire encode pass.
std::vector<uint32_t> reorder_rows(OuterRows& layout) {
  terra::require(layout.rows.size() <= 0x1000000U, "too many rows for u24 permutation");
  std::vector<uint32_t> permutation(layout.rows.size());
  for (uint32_t row = 0; row < permutation.size(); ++row) permutation[row] = row;
  std::sort(permutation.begin(), permutation.end(), [&](uint32_t left, uint32_t right) {
    for (size_t field : kRowSortFields) {
      const std::string_view a = span_view(layout.rows[left][field]);
      const std::string_view b = span_view(layout.rows[right][field]);
      if (byte_view_less(a, b)) return true;
      if (byte_view_less(b, a)) return false;
    }
    return left < right;
  });
  std::vector<std::array<Span, kJsonFields>> sorted;
  sorted.reserve(layout.rows.size());
  for (uint32_t row : permutation) sorted.push_back(layout.rows[row]);
  layout.rows.swap(sorted);
  return permutation;
}

std::vector<uint8_t> encode_row_permutation(const std::vector<uint32_t>& permutation) {
  std::vector<uint8_t> raw;
  raw.reserve(permutation.size() * 3);
  for (uint32_t row : permutation) {
    terra::require(row < 0x1000000U, "row ordinal does not fit u24");
    raw.push_back(static_cast<uint8_t>(row >> 16));
    raw.push_back(static_cast<uint8_t>(row >> 8));
    raw.push_back(static_cast<uint8_t>(row));
  }
  std::vector<uint8_t> packed = encode_lz(raw);
#ifdef TERRA_BWT
  std::vector<uint8_t> bwt = terra::encode_bwt_object(raw, configured_threads());
  if (bwt.size() < packed.size()) packed = std::move(bwt);
#endif
  return packed;
}
#endif

std::vector<uint8_t> encode_raw_field(const OuterRows& layout, size_t field) {
  return encode_lz(make_raw_column(layout, field));
}

#ifdef TERRA_FRONT_CODED
void append_front_length(std::vector<uint8_t>& output, size_t prefix) {
  terra::require(prefix < std::numeric_limits<uint32_t>::max(), "front prefix too long");
  uint32_t value = static_cast<uint32_t>(prefix + 1);
  while (value >= 128) {
    output.push_back(static_cast<uint8_t>(0x80U | (value & 127U)));
    value >>= 7;
  }
  output.push_back(static_cast<uint8_t>(value));
}

size_t front_common_prefix(const Span& left, const Span& right) {
  const size_t limit = std::min<size_t>(left.end - left.begin, right.end - right.begin);
  size_t prefix = 0;
  while (prefix < limit && left.begin[prefix] == right.begin[prefix]) ++prefix;
  return prefix;
}

std::vector<uint8_t> make_front_column(const OuterRows& layout, size_t field) {
  std::vector<uint8_t> output;
  output.reserve(layout.rows.size() * 8);
  Span previous{nullptr, nullptr};
  for (const auto& row : layout.rows) {
    const Span value = row[field];
    terra::require(std::memchr(value.begin, 0, static_cast<size_t>(value.end - value.begin)) == nullptr,
                   "NUL in front-coded value");
    const size_t prefix = previous.begin == nullptr ? 0 : front_common_prefix(previous, value);
    append_front_length(output, prefix);
    output.insert(output.end(), value.begin + prefix, value.end);
    output.push_back(0);
    previous = value;
  }
  return output;
}

std::vector<uint8_t> encode_front_field(const OuterRows& layout, size_t field) {
  const std::vector<uint8_t> raw = make_raw_column(layout, field);
  const std::vector<uint8_t> front = make_front_column(layout, field);
  std::vector<uint8_t> packed = encode_lz(front);
#ifdef TERRA_BWT
  std::vector<uint8_t> bwt = terra::encode_bwt_object(front, configured_threads());
  if (bwt.size() < packed.size()) packed = std::move(bwt);
#endif
  terra::require(raw.size() <= std::numeric_limits<uint32_t>::max() &&
                 layout.rows.size() <= std::numeric_limits<uint32_t>::max() &&
                 packed.size() <= std::numeric_limits<uint32_t>::max(),
                 "front-coded field too large");
  std::vector<uint8_t> output;
  output.reserve(17 + packed.size());
  output.insert(output.end(), {'T', 'F', 'C', '1'});
  output.push_back(1);
  terra::append_u32(output, static_cast<uint32_t>(raw.size()));
  terra::append_u32(output, static_cast<uint32_t>(layout.rows.size()));
  terra::append_u32(output, static_cast<uint32_t>(packed.size()));
  output.insert(output.end(), packed.begin(), packed.end());
  return output;
}
#endif

std::vector<uint8_t> make_dictionary_field(const OuterRows& layout, size_t field) {
  StringDictionary dictionary;
  dictionary.ids.reserve(layout.rows.size() * 2 + 1);
  for (const auto& row : layout.rows) dictionary.add(span_view(row[field]));
  dictionary.finalize_frequency();
  std::vector<uint32_t> values;
  values.reserve(layout.rows.size());
  for (const auto& row : layout.rows) values.push_back(dictionary.id(span_view(row[field])));
  std::vector<uint8_t> result;
  append_dictionary(result, dictionary);
  terra::append_huffman_stream(result, values);
  return result;
}

bool make_attribute_field(const OuterRows& layout, std::vector<uint8_t>& result) {
  constexpr size_t kAttributesField = 11;
  StringDictionary keys;
  StringDictionary values;
  keys.ids.reserve(128);
  values.ids.reserve(layout.rows.size());
  std::vector<AttributeEntry> entries;
  entries.reserve(64);
  for (const auto& row : layout.rows) {
    bool is_null = false;
    if (!parse_attributes(row[kAttributesField], is_null, entries)) return false;
    for (const AttributeEntry& entry : entries) {
      keys.add(span_view(entry.key));
      values.add(span_view(entry.value));
    }
  }
  keys.finalize_lexical();
  values.finalize_frequency();
  std::vector<uint32_t> counts;
  std::vector<uint32_t> key_order;
  std::vector<std::vector<uint32_t>> values_by_key(keys.entries.size());
  counts.reserve(layout.rows.size());
  for (const auto& row : layout.rows) {
    bool is_null = false;
    if (!parse_attributes(row[kAttributesField], is_null, entries) ||
        entries.size() > keys.entries.size()) return false;
    if (is_null) {
      counts.push_back(0);
      continue;
    }
    counts.push_back(static_cast<uint32_t>(entries.size() + 1));
    std::vector<uint8_t> seen(keys.entries.size(), 0);
    for (const AttributeEntry& entry : entries) {
      const uint32_t key = keys.id(span_view(entry.key));
      if (seen[key] != 0) return false;  // raw escape preserves unusual duplicate keys.
      seen[key] = 1;
      key_order.push_back(key);
      values_by_key[key].push_back(values.id(span_view(entry.value)));
    }
  }
  result.clear();
  append_dictionary(result, keys);
  append_dictionary(result, values);
  terra::append_huffman_stream(result, counts);
  terra::append_huffman_stream(result, key_order);
  for (const auto& stream : values_by_key) terra::append_huffman_stream(result, stream);
  return true;
}

bool make_hours_field(const OuterRows& layout, std::vector<uint8_t>& result) {
  constexpr size_t kHoursField = 13;
  StringDictionary keys;
  StringDictionary values;
  keys.ids.reserve(32);
  values.ids.reserve(4096);
  std::vector<AttributeEntry> entries;
  entries.reserve(16);
  for (const auto& row : layout.rows) {
    bool is_null = false;
    if (!parse_attributes(row[kHoursField], is_null, entries)) return false;
    for (const AttributeEntry& entry : entries) {
      keys.add(span_view(entry.key));
      values.add(span_view(entry.value));
    }
  }
  keys.finalize_lexical();
  values.finalize_frequency();
  PatternDictionary patterns;
  for (const auto& row : layout.rows) {
    bool is_null = false;
    if (!parse_attributes(row[kHoursField], is_null, entries)) return false;
    if (is_null) continue;
    std::vector<uint8_t> pattern;
    pattern.reserve(entries.size());
    std::vector<uint8_t> seen(keys.entries.size(), 0);
    for (const AttributeEntry& entry : entries) {
      const uint32_t key = keys.id(span_view(entry.key));
      if (key > 255 || seen[key] != 0) return false;
      seen[key] = 1;
      pattern.push_back(static_cast<uint8_t>(key));
    }
    patterns.add(pattern);
  }
  patterns.finalize();
  std::vector<uint32_t> pattern_ids;
  std::vector<std::vector<uint32_t>> values_by_key(keys.entries.size());
  pattern_ids.reserve(layout.rows.size());
  for (const auto& row : layout.rows) {
    bool is_null = false;
    if (!parse_attributes(row[kHoursField], is_null, entries)) return false;
    if (is_null) {
      pattern_ids.push_back(0);
      continue;
    }
    std::vector<uint8_t> pattern;
    pattern.reserve(entries.size());
    std::vector<uint8_t> seen(keys.entries.size(), 0);
    for (const AttributeEntry& entry : entries) {
      const uint32_t key = keys.id(span_view(entry.key));
      if (key > 255 || seen[key] != 0) return false;
      seen[key] = 1;
      pattern.push_back(static_cast<uint8_t>(key));
      values_by_key[key].push_back(values.id(span_view(entry.value)));
    }
    pattern_ids.push_back(patterns.id(pattern) + 1);
  }
  result.clear();
  append_dictionary(result, keys);
  append_dictionary(result, values);
  terra::require(patterns.entries.size() <= std::numeric_limits<uint32_t>::max(), "too many hour patterns");
  terra::append_u32(result, static_cast<uint32_t>(patterns.entries.size()));
  for (const auto& pattern : patterns.entries) {
    terra::require(pattern.size() <= std::numeric_limits<uint32_t>::max(), "hour pattern too large");
    terra::append_u32(result, static_cast<uint32_t>(pattern.size()));
    result.insert(result.end(), pattern.begin(), pattern.end());
  }
  terra::append_huffman_stream(result, pattern_ids);
  for (const auto& stream : values_by_key) terra::append_huffman_stream(result, stream);
  return true;
}

bool make_categories_field(const OuterRows& layout, std::vector<uint8_t>& result) {
  constexpr size_t kCategoriesField = 12;
  StringDictionary labels;
  labels.ids.reserve(2048);
  std::vector<Span> pieces;
  pieces.reserve(32);
  for (const auto& row : layout.rows) {
    bool is_null = false;
    if (!parse_categories(row[kCategoriesField], is_null, pieces)) return false;
    if (!is_null) {
      for (const Span& piece : pieces) labels.add(span_view(piece));
    }
  }
  labels.finalize_frequency();
  std::vector<uint32_t> counts;
  std::vector<uint32_t> values;
  counts.reserve(layout.rows.size());
  for (const auto& row : layout.rows) {
    bool is_null = false;
    if (!parse_categories(row[kCategoriesField], is_null, pieces) || pieces.size() == std::numeric_limits<uint32_t>::max()) {
      return false;
    }
    if (is_null) {
      counts.push_back(0);
    } else {
      counts.push_back(static_cast<uint32_t>(pieces.size() + 1));
      for (const Span& piece : pieces) values.push_back(labels.id(span_view(piece)));
    }
  }
  result.clear();
  append_dictionary(result, labels);
  terra::append_huffman_stream(result, counts);
  terra::append_huffman_stream(result, values);
  return true;
}

#ifdef TERRA_CATEGORY_CONTEXT
bool make_categories_context_field(const OuterRows& layout, std::vector<uint8_t>& result) {
  constexpr size_t kCategoriesField = 12;
  constexpr uint32_t kNoPrediction = 0xffffffffU;
  StringDictionary labels;
  labels.ids.reserve(2048);
  std::vector<Span> pieces;
  pieces.reserve(32);
  for (const auto& row : layout.rows) {
    bool is_null = false;
    if (!parse_categories(row[kCategoriesField], is_null, pieces)) return false;
    if (!is_null) {
      for (const Span& piece : pieces) labels.add(span_view(piece));
    }
  }
  labels.finalize_frequency();
  std::vector<uint32_t> counts;
  std::vector<uint32_t> all_values;
  std::unordered_map<uint64_t, uint32_t> transitions;
  counts.reserve(layout.rows.size());
  transitions.reserve(labels.entries.size() * 8 + 1);
  for (const auto& row : layout.rows) {
    bool is_null = false;
    if (!parse_categories(row[kCategoriesField], is_null, pieces) || pieces.size() == std::numeric_limits<uint32_t>::max()) {
      return false;
    }
    if (is_null) {
      counts.push_back(0);
      continue;
    }
    counts.push_back(static_cast<uint32_t>(pieces.size() + 1));
    uint32_t previous = kNoPrediction;
    for (const Span& piece : pieces) {
      const uint32_t value = labels.id(span_view(piece));
      all_values.push_back(value);
      if (previous != kNoPrediction) {
        const uint64_t pair = (uint64_t(previous) << 32) | value;
        uint32_t& count = transitions[pair];
        terra::require(count != std::numeric_limits<uint32_t>::max(), "category transition overflow");
        ++count;
      }
      previous = value;
    }
  }
  std::vector<uint32_t> predictions(labels.entries.size(), kNoPrediction);
  std::vector<uint32_t> prediction_counts(labels.entries.size(), 0);
  for (const auto& item : transitions) {
    const uint32_t previous = static_cast<uint32_t>(item.first >> 32);
    const uint32_t next = static_cast<uint32_t>(item.first);
    terra::require(previous < predictions.size() && next < predictions.size(), "invalid category transition");
    if (item.second > prediction_counts[previous] ||
        (item.second == prediction_counts[previous] && next < predictions[previous])) {
      predictions[previous] = next;
      prediction_counts[previous] = item.second;
    }
  }
  std::vector<uint32_t> first_values;
  std::vector<uint32_t> flags;
  std::vector<uint32_t> escapes;
  first_values.reserve(layout.rows.size());
  size_t position = 0;
  for (uint32_t tag : counts) {
    if (tag == 0) continue;
    terra::require(position < all_values.size(), "missing category first value");
    uint32_t previous = all_values[position++];
    first_values.push_back(previous);
    for (uint32_t index = 1; index < tag - 1; ++index) {
      terra::require(position < all_values.size(), "missing category context value");
      const uint32_t value = all_values[position++];
      const bool hit = predictions[previous] != kNoPrediction && predictions[previous] == value;
      flags.push_back(hit ? 0 : 1);
      if (!hit) escapes.push_back(value);
      previous = value;
    }
  }
  terra::require(position == all_values.size(), "trailing category context values");
  result.clear();
  append_dictionary(result, labels);
  terra::append_huffman_stream(result, counts);
  uint32_t prediction_count = 0;
  for (uint32_t value : predictions) prediction_count += value != kNoPrediction;
  terra::append_u32(result, prediction_count);
  for (uint32_t previous = 0; previous < predictions.size(); ++previous) {
    if (predictions[previous] == kNoPrediction) continue;
    terra::append_u32(result, previous);
    terra::append_u32(result, predictions[previous]);
  }
  terra::append_huffman_stream(result, first_values);
  terra::append_huffman_stream(result, flags);
  terra::append_huffman_stream(result, escapes);
  return true;
}
#endif

struct FieldPayload {
  uint8_t kind = 0;  // 0 raw, 1 packed ID, 2 dictionary, 3 attrs, 4 categories, 5 hours, 6 contextual categories
  std::vector<uint8_t> data;
};

bool encode_typed_json(const std::vector<uint8_t>& input, std::vector<uint8_t>& archive) {
  OuterRows layout;
  if (!parse_outer_rows(input, layout)) return false;
#ifdef TERRA_ROW_REORDER
  const std::vector<uint32_t> permutation = reorder_rows(layout);
  const std::vector<uint8_t> packed_permutation = encode_row_permutation(permutation);
#endif
  constexpr size_t kBusinessIdField = 0;
  constexpr size_t kAttributesField = 11;
  constexpr size_t kCategoriesField = 12;
  constexpr size_t kHoursField = 13;
  std::array<FieldPayload, kJsonFields> fields;
  for (size_t field = 0; field < kJsonFields; ++field) {
    fields[field].data = encode_raw_field(layout, field);
  }

#ifndef TERRA_TYPED_LEVEL
#define TERRA_TYPED_LEVEL 5
#endif
  if (TERRA_TYPED_LEVEL >= 1) {
    std::vector<uint8_t> packed_ids;
    packed_ids.reserve(layout.rows.size() * 16);
    bool valid_ids = true;
    for (const auto& row : layout.rows) valid_ids &= append_packed_business_id(row[kBusinessIdField], packed_ids);
    if (valid_ids && packed_ids.size() < fields[kBusinessIdField].data.size()) {
      fields[kBusinessIdField] = {1, std::move(packed_ids)};
    }
    for (const size_t field : {size_t{8}, size_t{10}}) {
      std::vector<uint8_t> typed = make_dictionary_field(layout, field);
      if (typed.size() < fields[field].data.size()) fields[field] = {2, std::move(typed)};
    }
  }
  if (TERRA_TYPED_LEVEL >= 2) {
    std::vector<uint8_t> typed;
    if (make_attribute_field(layout, typed) && typed.size() < fields[kAttributesField].data.size()) {
      fields[kAttributesField] = {3, std::move(typed)};
    }
  }
  if (TERRA_TYPED_LEVEL >= 3) {
    std::vector<uint8_t> typed;
    if (make_hours_field(layout, typed) && typed.size() < fields[kHoursField].data.size()) {
      fields[kHoursField] = {5, std::move(typed)};
    }
  }
  if (TERRA_TYPED_LEVEL >= 4) {
    std::vector<uint8_t> typed;
    if (make_categories_field(layout, typed) && typed.size() < fields[kCategoriesField].data.size()) {
      fields[kCategoriesField] = {4, std::move(typed)};
    }
#ifdef TERRA_CATEGORY_CONTEXT
    std::vector<uint8_t> contextual;
    if (make_categories_context_field(layout, contextual) && contextual.size() < fields[kCategoriesField].data.size()) {
      fields[kCategoriesField] = {6, std::move(contextual)};
    }
#endif
  }
  if (TERRA_TYPED_LEVEL >= 5) {
    for (const size_t field : {size_t{3}, size_t{4}, size_t{5}, size_t{9}}) {
      std::vector<uint8_t> typed = make_dictionary_field(layout, field);
      if (typed.size() < fields[field].data.size()) fields[field] = {2, std::move(typed)};
    }
  }
#ifdef TERRA_BWT
  // The typed fields have already been selected against their raw escape. Do
  // not spend maximum-mode suffix-sort time on columns that will be discarded.
  for (size_t field = 0; field < kJsonFields; ++field) {
    if (fields[field].kind != 0) continue;
    std::vector<uint8_t> raw = make_raw_column(layout, field);
    std::vector<uint8_t> bwt = terra::encode_bwt_object(raw, configured_threads());
    if (bwt.size() < fields[field].data.size()) fields[field].data = std::move(bwt);
  }
#endif
#ifdef TERRA_FRONT_CODED
  // Front coding pays only for lexical raw columns in the sorted maximum
  // representation. Typed streams retain their direct dictionary models.
  for (const size_t field : {size_t{2}, size_t{3}, size_t{4}, size_t{5}, size_t{6}, size_t{7}}) {
    if (fields[field].kind != 0) continue;
    std::vector<uint8_t> front = encode_front_field(layout, field);
    if (front.size() < fields[field].data.size()) fields[field].data = std::move(front);
  }
#endif

  size_t total = 4 + 1 + 1 + 8 + 4 + 4 * layout.fragments.size() + 4;
  for (const auto& fragment : layout.fragments) total += fragment.size();
#ifdef TERRA_ROW_REORDER
  terra::require(packed_permutation.size() <= std::numeric_limits<uint32_t>::max(),
                 "row permutation too large");
  terra::require(packed_permutation.size() <= std::numeric_limits<size_t>::max() - total - 4,
                 "typed archive too large");
  total += 4 + packed_permutation.size();
#endif
  for (const auto& field : fields) {
    terra::require(field.data.size() <= std::numeric_limits<uint32_t>::max(), "typed field too large");
    terra::require(field.data.size() <= std::numeric_limits<size_t>::max() - total - 5,
                   "typed archive too large");
    total += 5 + field.data.size();
  }
  archive.clear();
  archive.reserve(total);
#ifdef TERRA_ROW_REORDER
  archive.insert(archive.end(), {'T', 'S', 'J', '5'});
#else
  archive.insert(archive.end(), {'T', 'S', 'J', '4'});
#endif
  archive.push_back(1);
  archive.push_back(static_cast<uint8_t>(kJsonFields));
  terra::append_u64(archive, input.size());
  terra::append_u32(archive, static_cast<uint32_t>(layout.rows.size()));
  for (const auto& fragment : layout.fragments) {
    terra::require(fragment.size() <= std::numeric_limits<uint32_t>::max(), "fragment too large");
    terra::append_u32(archive, static_cast<uint32_t>(fragment.size()));
  }
  for (const auto& fragment : layout.fragments) archive.insert(archive.end(), fragment.begin(), fragment.end());
#ifdef TERRA_ROW_REORDER
  terra::append_u32(archive, static_cast<uint32_t>(packed_permutation.size()));
  archive.insert(archive.end(), packed_permutation.begin(), packed_permutation.end());
#endif
  for (const auto& field : fields) {
    archive.push_back(field.kind);
    terra::append_u32(archive, static_cast<uint32_t>(field.data.size()));
    archive.insert(archive.end(), field.data.begin(), field.data.end());
  }
  terra::append_u32(archive, terra::crc32(archive));
  return true;
}

#endif  // TERRA_MAXIMUM

#ifndef TERRA_LEAN_FAST
bool encode_attribute_json(const std::vector<uint8_t>& input, std::vector<uint8_t>& archive) {
  OuterRows layout;
  if (!parse_outer_rows(input, layout)) return false;
  constexpr size_t kAttributesField = 11;
  std::unordered_map<std::string_view, uint8_t, StringViewHash> key_ids;
  std::vector<std::string_view> keys;
  std::vector<std::vector<uint8_t>> values_by_key;
  std::vector<uint8_t> order;
  order.reserve(layout.rows.size() * 9);
  std::vector<AttributeEntry> entries;
  entries.reserve(64);
  for (const auto& row : layout.rows) {
    bool is_null = false;
    if (!parse_attributes(row[kAttributesField], is_null, entries) || entries.size() > 254) return false;
    if (is_null) {
      order.push_back(0);
      continue;
    }
    order.push_back(static_cast<uint8_t>(entries.size() + 1));
    for (const AttributeEntry& entry : entries) {
      const std::string_view key(reinterpret_cast<const char*>(entry.key.begin),
                                 size_t(entry.key.end - entry.key.begin));
      auto found = key_ids.find(key);
      uint8_t id;
      if (found == key_ids.end()) {
        if (keys.size() == 255) return false;
        id = static_cast<uint8_t>(keys.size());
        key_ids.emplace(key, id);
        keys.push_back(key);
        values_by_key.emplace_back();
      } else {
        id = found->second;
      }
      order.push_back(id);
      auto& values = values_by_key[id];
      values.insert(values.end(), entry.value.begin, entry.value.end);
      values.push_back(0);
    }
  }

  std::array<std::vector<uint8_t>, kJsonFields> raw_columns;
  for (size_t field = 0; field < kJsonFields; ++field) {
    if (field != kAttributesField) raw_columns[field].reserve(input.size() / kJsonFields);
  }
  for (const auto& row : layout.rows) {
    for (size_t field = 0; field < kJsonFields; ++field) {
      if (field == kAttributesField) continue;
      raw_columns[field].insert(raw_columns[field].end(), row[field].begin, row[field].end);
      raw_columns[field].push_back(0);
    }
  }

  std::array<std::vector<uint8_t>, kJsonFields> packed_columns;
  for (size_t field = 0; field < kJsonFields; ++field) {
    if (field != kAttributesField) packed_columns[field] = encode_lz(raw_columns[field]);
  }
  std::vector<uint8_t> packed_order = encode_lz(order);
  std::vector<std::vector<uint8_t>> packed_values;
  packed_values.reserve(values_by_key.size());
  for (const auto& values : values_by_key) packed_values.push_back(encode_lz(values));

  size_t total = 4 + 1 + 1 + 8 + 4 + 4 * layout.fragments.size() + 4 + 4;
  for (const auto& fragment : layout.fragments) total += fragment.size();
  for (const auto& key : keys) total += 4 + key.size();
  for (size_t field = 0; field < kJsonFields; ++field) {
    if (field == kAttributesField) continue;
    terra::require(packed_columns[field].size() <= std::numeric_limits<uint32_t>::max(),
                   "outer column too large");
    total += 4 + packed_columns[field].size();
  }
  terra::require(packed_order.size() <= std::numeric_limits<uint32_t>::max(), "attribute order too large");
  total += 4 + packed_order.size();
  for (const auto& values : packed_values) {
    terra::require(values.size() <= std::numeric_limits<uint32_t>::max(), "attribute value stream too large");
    total += 4 + values.size();
  }

  archive.clear();
  archive.reserve(total);
  archive.insert(archive.end(), {'T', 'S', 'J', '2'});
  archive.push_back(1);
  archive.push_back(static_cast<uint8_t>(kJsonFields));
  terra::append_u64(archive, input.size());
  terra::append_u32(archive, static_cast<uint32_t>(layout.rows.size()));
  for (const auto& fragment : layout.fragments) {
    terra::require(fragment.size() <= std::numeric_limits<uint32_t>::max(), "fragment too large");
    terra::append_u32(archive, static_cast<uint32_t>(fragment.size()));
  }
  for (const auto& fragment : layout.fragments) archive.insert(archive.end(), fragment.begin(), fragment.end());
  terra::append_u32(archive, static_cast<uint32_t>(keys.size()));
  for (const auto& key : keys) {
    terra::append_u32(archive, static_cast<uint32_t>(key.size()));
    archive.insert(archive.end(), key.begin(), key.end());
  }
  for (size_t field = 0; field < kJsonFields; ++field) {
    if (field == kAttributesField) continue;
    terra::append_u32(archive, static_cast<uint32_t>(packed_columns[field].size()));
    archive.insert(archive.end(), packed_columns[field].begin(), packed_columns[field].end());
  }
  terra::append_u32(archive, static_cast<uint32_t>(packed_order.size()));
  archive.insert(archive.end(), packed_order.begin(), packed_order.end());
  for (const auto& values : packed_values) {
    terra::append_u32(archive, static_cast<uint32_t>(values.size()));
    archive.insert(archive.end(), values.begin(), values.end());
  }
  terra::append_u32(archive, terra::crc32(archive));
  return true;
}
#endif

#ifndef TERRA_MAXIMUM
bool encode_attribute_hour_json(const std::vector<uint8_t>& input, std::vector<uint8_t>& archive) {
  OuterRows layout;
  if (!parse_outer_rows(input, layout)) return false;
  constexpr size_t kAttributesField = 11;
  constexpr size_t kHoursField = 13;
  std::unordered_map<std::string_view, uint8_t, StringViewHash> attribute_ids;
  std::vector<std::string_view> attribute_keys;
  std::vector<std::vector<uint8_t>> attribute_values;
  std::unordered_map<std::string_view, uint8_t, StringViewHash> weekday_ids;
  std::vector<std::string_view> weekday_keys;
  std::vector<std::vector<uint8_t>> hour_values;
  std::unordered_map<std::string, uint8_t> pattern_ids;
  std::vector<std::vector<uint8_t>> patterns;
  std::vector<uint8_t> attribute_order;
  std::vector<uint8_t> hour_order;
  attribute_order.reserve(layout.rows.size() * 9);
  hour_order.reserve(layout.rows.size());
  std::vector<AttributeEntry> entries;
  entries.reserve(64);
  for (const auto& row : layout.rows) {
    bool is_null = false;
    if (!parse_attributes(row[kAttributesField], is_null, entries) || entries.size() > 254) return false;
    if (is_null) {
      attribute_order.push_back(0);
    } else {
      attribute_order.push_back(static_cast<uint8_t>(entries.size() + 1));
      for (const AttributeEntry& entry : entries) {
        const std::string_view key(reinterpret_cast<const char*>(entry.key.begin),
                                   size_t(entry.key.end - entry.key.begin));
        auto found = attribute_ids.find(key);
        uint8_t id;
        if (found == attribute_ids.end()) {
          if (attribute_keys.size() == 255) return false;
          id = static_cast<uint8_t>(attribute_keys.size());
          attribute_ids.emplace(key, id);
          attribute_keys.push_back(key);
          attribute_values.emplace_back();
        } else {
          id = found->second;
        }
        attribute_order.push_back(id);
        auto& values = attribute_values[id];
        values.insert(values.end(), entry.value.begin, entry.value.end);
        values.push_back(0);
      }
    }

    if (!parse_attributes(row[kHoursField], is_null, entries) || entries.size() > 254) return false;
    if (is_null) {
      hour_order.push_back(0);
    } else {
      std::vector<uint8_t> pattern;
      pattern.reserve(entries.size());
      for (const AttributeEntry& entry : entries) {
        const std::string_view key(reinterpret_cast<const char*>(entry.key.begin),
                                   size_t(entry.key.end - entry.key.begin));
        auto found = weekday_ids.find(key);
        uint8_t id;
        if (found == weekday_ids.end()) {
          if (weekday_keys.size() == 255) return false;
          id = static_cast<uint8_t>(weekday_keys.size());
          weekday_ids.emplace(key, id);
          weekday_keys.push_back(key);
          hour_values.emplace_back();
        } else {
          id = found->second;
        }
        pattern.push_back(id);
        auto& values = hour_values[id];
        values.insert(values.end(), entry.value.begin, entry.value.end);
        values.push_back(0);
      }
      const std::string pattern_key(pattern.begin(), pattern.end());
      auto found_pattern = pattern_ids.find(pattern_key);
      uint8_t id;
      if (found_pattern == pattern_ids.end()) {
        if (patterns.size() == 255) return false;
        id = static_cast<uint8_t>(patterns.size());
        pattern_ids.emplace(pattern_key, id);
        patterns.push_back(std::move(pattern));
      } else {
        id = found_pattern->second;
      }
      hour_order.push_back(static_cast<uint8_t>(id + 1));
    }
  }

  std::array<std::vector<uint8_t>, kJsonFields> raw_columns;
  for (size_t field = 0; field < kJsonFields; ++field) {
    if (field != kAttributesField && field != kHoursField) {
      raw_columns[field].reserve(input.size() / kJsonFields);
    }
  }
  for (const auto& row : layout.rows) {
    for (size_t field = 0; field < kJsonFields; ++field) {
      if (field == kAttributesField || field == kHoursField) continue;
      raw_columns[field].insert(raw_columns[field].end(), row[field].begin, row[field].end);
      raw_columns[field].push_back(0);
    }
  }
  std::array<std::vector<uint8_t>, kJsonFields> packed_columns;
  for (size_t field = 0; field < kJsonFields; ++field) {
    if (field != kAttributesField && field != kHoursField) packed_columns[field] = encode_lz(raw_columns[field]);
  }
  const std::vector<uint8_t> packed_attribute_order = encode_lz(attribute_order);
  const std::vector<uint8_t> packed_hour_order = encode_lz(hour_order);
  std::vector<std::vector<uint8_t>> packed_attribute_values;
  packed_attribute_values.reserve(attribute_values.size());
  for (const auto& values : attribute_values) packed_attribute_values.push_back(encode_lz(values));
  std::vector<std::vector<uint8_t>> packed_hour_values;
  packed_hour_values.reserve(hour_values.size());
  for (const auto& values : hour_values) packed_hour_values.push_back(encode_lz(values));

  size_t total = 4 + 1 + 1 + 8 + 4 + 4 * layout.fragments.size() + 4 + 4 + 4 + 4;
  for (const auto& fragment : layout.fragments) total += fragment.size();
  for (const auto& key : attribute_keys) total += 4 + key.size();
  for (const auto& key : weekday_keys) total += 4 + key.size();
  for (const auto& pattern : patterns) total += 1 + pattern.size();
  for (size_t field = 0; field < kJsonFields; ++field) {
    if (field == kAttributesField || field == kHoursField) continue;
    terra::require(packed_columns[field].size() <= std::numeric_limits<uint32_t>::max(),
                   "outer column too large");
    total += 4 + packed_columns[field].size();
  }
  terra::require(packed_attribute_order.size() <= std::numeric_limits<uint32_t>::max(),
                 "attribute order too large");
  terra::require(packed_hour_order.size() <= std::numeric_limits<uint32_t>::max(), "hour order too large");
  total += 4 + packed_attribute_order.size() + 4 + packed_hour_order.size();
  for (const auto& values : packed_attribute_values) {
    terra::require(values.size() <= std::numeric_limits<uint32_t>::max(), "attribute value stream too large");
    total += 4 + values.size();
  }
  for (const auto& values : packed_hour_values) {
    terra::require(values.size() <= std::numeric_limits<uint32_t>::max(), "hour value stream too large");
    total += 4 + values.size();
  }

  archive.clear();
  archive.reserve(total);
  archive.insert(archive.end(), {'T', 'S', 'J', '3'});
  archive.push_back(1);
  archive.push_back(static_cast<uint8_t>(kJsonFields));
  terra::append_u64(archive, input.size());
  terra::append_u32(archive, static_cast<uint32_t>(layout.rows.size()));
  for (const auto& fragment : layout.fragments) {
    terra::require(fragment.size() <= std::numeric_limits<uint32_t>::max(), "fragment too large");
    terra::append_u32(archive, static_cast<uint32_t>(fragment.size()));
  }
  for (const auto& fragment : layout.fragments) archive.insert(archive.end(), fragment.begin(), fragment.end());
  terra::append_u32(archive, static_cast<uint32_t>(attribute_keys.size()));
  for (const auto& key : attribute_keys) {
    terra::append_u32(archive, static_cast<uint32_t>(key.size()));
    archive.insert(archive.end(), key.begin(), key.end());
  }
  terra::append_u32(archive, static_cast<uint32_t>(weekday_keys.size()));
  for (const auto& key : weekday_keys) {
    terra::append_u32(archive, static_cast<uint32_t>(key.size()));
    archive.insert(archive.end(), key.begin(), key.end());
  }
  terra::append_u32(archive, static_cast<uint32_t>(patterns.size()));
  for (const auto& pattern : patterns) {
    archive.push_back(static_cast<uint8_t>(pattern.size()));
    archive.insert(archive.end(), pattern.begin(), pattern.end());
  }
  for (size_t field = 0; field < kJsonFields; ++field) {
    if (field == kAttributesField || field == kHoursField) continue;
    terra::append_u32(archive, static_cast<uint32_t>(packed_columns[field].size()));
    archive.insert(archive.end(), packed_columns[field].begin(), packed_columns[field].end());
  }
  terra::append_u32(archive, static_cast<uint32_t>(packed_attribute_order.size()));
  archive.insert(archive.end(), packed_attribute_order.begin(), packed_attribute_order.end());
  for (const auto& values : packed_attribute_values) {
    terra::append_u32(archive, static_cast<uint32_t>(values.size()));
    archive.insert(archive.end(), values.begin(), values.end());
  }
  terra::append_u32(archive, static_cast<uint32_t>(packed_hour_order.size()));
  archive.insert(archive.end(), packed_hour_order.begin(), packed_hour_order.end());
  for (const auto& values : packed_hour_values) {
    terra::append_u32(archive, static_cast<uint32_t>(values.size()));
    archive.insert(archive.end(), values.begin(), values.end());
  }
  terra::append_u32(archive, terra::crc32(archive));
  return true;
}
#endif  // TERRA_MAXIMUM

std::vector<uint8_t> encode_object(const std::vector<uint8_t>& input) {
  std::vector<uint8_t> structural;
#ifdef TERRA_MAXIMUM
  if (encode_typed_json(input, structural)) return structural;
#else
#ifndef TERRA_GENERIC_ONLY
#ifdef TERRA_OUTER_ONLY
  if (encode_structural_json(input, structural)) return structural;
#else
  if (encode_attribute_hour_json(input, structural)) return structural;
#ifndef TERRA_LEAN_FAST
  if (encode_attribute_json(input, structural)) return structural;
  if (encode_structural_json(input, structural)) return structural;
#endif
#endif
#endif
#endif
  return encode_lz(input);
}

std::vector<Record> encode_records(const std::vector<Record>& input) {
  std::vector<Record> output;
  output.reserve(input.size());
  for (const Record& record : input) output.push_back({record.alias, encode_object(record.payload)});
  return output;
}

void encode_stream() {
  const char input_magic[4] = {'H', 'B', 'I', '1'};
  const char output_magic[4] = {'H', 'B', 'A', '1'};
  auto records = terra::parse_batch(terra::read_all(std::cin), input_magic);
  auto archive = terra::make_batch(encode_records(records), output_magic);
  std::cout.write(reinterpret_cast<const char*>(archive.data()), archive.size());
  terra::require(static_cast<bool>(std::cout), "stream write failure");
}

void encode_dir(const char* input_dir, const char* output_dir) {
  const char input_magic[4] = {'H', 'B', 'I', '1'};
  const char output_magic[4] = {'H', 'B', 'A', '1'};
  auto records = terra::read_directory(input_dir, input_magic);
  terra::write_directory(output_dir, encode_records(records), output_magic);
}

}  // namespace

int main(int argc, char** argv) {
  try {
    if (argc == 2 && std::string(argv[1]) == "encode-stream") {
      encode_stream();
    } else if (argc == 4 && std::string(argv[1]) == "encode-dir") {
      encode_dir(argv[2], argv[3]);
    } else {
      terra::die("usage: codec encode-stream | codec encode-dir INPUT_DIR OUTPUT_DIR");
    }
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "codec: " << error.what() << '\n';
    return 1;
  }
}
