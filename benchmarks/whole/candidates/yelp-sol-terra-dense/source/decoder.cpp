#include "common.hpp"
#if defined(TERRA_MAXIMUM) || defined(TERRA_LZ_HUFFMAN)
#include "huffman.hpp"
#endif
#ifdef TERRA_BWT
#include "bwt.hpp"
#endif

#include <algorithm>
#include <unordered_set>

namespace {

using terra::Record;

constexpr size_t kJsonFields = 14;
constexpr size_t kMaximumObjectBytes = size_t{1} << 30;

size_t read_lz_length(terra::Reader& reader, size_t base) {
  size_t length = base;
  if (base != 15) return length;
  for (;;) {
    const uint8_t extra = reader.u8();
    terra::require(length <= std::numeric_limits<size_t>::max() - extra, "LZ length overflow");
    length += extra;
    if (extra != 255) return length;
  }
}

void decode_lz_block(terra::Reader& reader, uint8_t* output, size_t size) {
  size_t written = 0;
  while (written < size) {
    const uint8_t token = reader.u8();
    const size_t literal_length = read_lz_length(reader, token >> 4);
    terra::require(literal_length <= size - written, "literal exceeds block");
    const uint8_t* literals = reader.bytes(literal_length);
    if (literal_length != 0) std::memcpy(output + written, literals, literal_length);
    written += literal_length;
    if (written == size) return;
    terra::require(reader.remaining() >= 2, "truncated LZ offset");
    const size_t distance = size_t(reader.u8()) | (size_t(reader.u8()) << 8);
    terra::require(distance != 0 && distance <= written, "invalid LZ offset");
    const size_t match_length = read_lz_length(reader, token & 15) + 4;
    terra::require(match_length <= size - written, "match exceeds block");
    uint8_t* destination = output + written;
    const uint8_t* source = destination - distance;
    if (distance >= match_length) {
      std::memcpy(destination, source, match_length);
    } else {
      size_t copied = distance;
      std::memcpy(destination, source, copied);
      while (copied < match_length) {
        const size_t chunk = std::min(copied, match_length - copied);
        std::memcpy(destination + copied, destination, chunk);
        copied += chunk;
      }
    }
    written += match_length;
    if (written == size) return;
  }
  terra::die("LZ block lacks final literal sequence");
}

#ifdef TERRA_LZ_HUFFMAN
void decode_lz_huffman_block(terra::Reader& reader, uint8_t* output, size_t raw_size) {
  const uint32_t fixed_size = reader.u32();
  terra::require(fixed_size != 0 && fixed_size < raw_size, "invalid Huffman LZ length");
  std::vector<uint8_t> lengths(256);
  for (uint8_t& length : lengths) length = reader.u8();
  const terra::HuffmanTable table = terra::canonical_huffman(lengths);
  terra::HuffmanDecoder decoder(table);
  const uint8_t* entropy = reader.current();
  const size_t entropy_size = reader.remaining();
  (void)reader.bytes(entropy_size);
  terra::HuffmanReader bits(entropy, entropy_size);
  std::vector<uint8_t> fixed(fixed_size);
  for (uint8_t& byte : fixed) byte = static_cast<uint8_t>(decoder.symbol(bits));
  bits.finish_zero_padding();
  terra::Reader fixed_reader(fixed);
  decode_lz_block(fixed_reader, output, raw_size);
  fixed_reader.finish();
}
#endif

std::vector<uint8_t> decode_lz_object(const std::vector<uint8_t>& archive,
                                      size_t maximum_output = kMaximumObjectBytes) {
  terra::require(archive.size() >= 26, "truncated object checksum");
  const size_t checksum_offset = archive.size() - 4;
  const uint32_t expected_object_crc = (uint32_t(archive[checksum_offset]) << 24) |
                                       (uint32_t(archive[checksum_offset + 1]) << 16) |
                                       (uint32_t(archive[checksum_offset + 2]) << 8) |
                                       uint32_t(archive[checksum_offset + 3]);
  terra::require(terra::crc32(archive.data(), checksum_offset) == expected_object_crc,
                 "object checksum mismatch");
  terra::Reader reader(archive.data(), checksum_offset);
  const uint8_t* magic = reader.bytes(4);
  bool huffman_container = false;
  if (std::memcmp(magic, "TLZ2", 4) != 0) {
#ifdef TERRA_LZ_HUFFMAN
    terra::require(std::memcmp(magic, "TLH1", 4) == 0, "wrong object magic");
    huffman_container = true;
#else
    terra::die("wrong object magic");
#endif
  }
  terra::require(reader.u8() == 1, "unsupported object version");
  terra::require(reader.u8() == 1, "unsupported object mode");
  const uint64_t original_size64 = reader.u64();
  terra::require(original_size64 <= std::numeric_limits<size_t>::max(), "object too large");
  const size_t original_size = static_cast<size_t>(original_size64);
  terra::require(original_size <= maximum_output, "object exceeds decode limit");
  const uint32_t block_size = reader.u32();
  terra::require(block_size != 0 && block_size <= 64U * 1024U * 1024U, "invalid block size");
  const uint32_t count = reader.u32();
  const uint64_t expected_count = original_size64 / block_size +
                                  (original_size64 % block_size != 0 ? 1 : 0);
  terra::require(expected_count == count, "invalid block count");
  std::vector<uint8_t> output(original_size);
  size_t written = 0;
  for (uint32_t index = 0; index < count; ++index) {
    const uint32_t raw_size = reader.u32();
    const uint32_t packed_size = reader.u32();
    const uint32_t expected_crc = reader.u32();
    const uint8_t kind = reader.u8();
    terra::require(raw_size != 0 && raw_size <= block_size && raw_size <= original_size - written,
                   "invalid uncompressed block size");
    const bool final_block = index + 1 == count;
    terra::require(final_block || raw_size == block_size, "short nonfinal block");
    terra::require(packed_size <= reader.remaining(), "truncated compressed block");
    const uint8_t* packed = reader.bytes(packed_size);
    if (kind == 0) {
      terra::require(packed_size == raw_size, "raw block size mismatch");
      std::memcpy(output.data() + written, packed, raw_size);
    } else if (kind == 1) {
      terra::Reader block_reader(packed, packed_size);
      decode_lz_block(block_reader, output.data() + written, raw_size);
      block_reader.finish();
#ifdef TERRA_LZ_HUFFMAN
    } else if (kind == 2 && huffman_container) {
      terra::Reader block_reader(packed, packed_size);
      decode_lz_huffman_block(block_reader, output.data() + written, raw_size);
      block_reader.finish();
#endif
    } else {
      terra::die("unknown block kind");
    }
    terra::require(terra::crc32(output.data() + written, raw_size) == expected_crc,
                   "block checksum mismatch");
    written += raw_size;
  }
  terra::require(written == original_size, "object length mismatch");
  reader.finish();
  return output;
}

#if !defined(TERRA_LEAN_FAST) && !defined(TERRA_MAXIMUM)
std::vector<uint8_t> decode_structural_json(const std::vector<uint8_t>& archive) {
  terra::require(archive.size() >= 82, "truncated structural archive");
  const size_t checksum_offset = archive.size() - 4;
  const uint32_t expected_object_crc = (uint32_t(archive[checksum_offset]) << 24) |
                                       (uint32_t(archive[checksum_offset + 1]) << 16) |
                                       (uint32_t(archive[checksum_offset + 2]) << 8) |
                                       uint32_t(archive[checksum_offset + 3]);
  terra::require(terra::crc32(archive.data(), checksum_offset) == expected_object_crc,
                 "structural checksum mismatch");
  terra::Reader reader(archive.data(), checksum_offset);
  const uint8_t* magic = reader.bytes(4);
  bool row_major_layout = false;
  if (std::memcmp(magic, "TSJ1", 4) != 0) {
#ifdef TERRA_ROW_MAJOR_COLUMNS
    terra::require(std::memcmp(magic, "TSJ0", 4) == 0, "wrong structural magic");
    row_major_layout = true;
#else
    terra::die("wrong structural magic");
#endif
  }
  terra::require(reader.u8() == 1, "unsupported structural version");
  terra::require(reader.u8() == kJsonFields, "unsupported structural field count");
  const uint64_t original_size64 = reader.u64();
  terra::require(original_size64 <= kMaximumObjectBytes, "structural object too large");
  const size_t original_size = static_cast<size_t>(original_size64);
  const uint32_t records = reader.u32();
  terra::require(records != 0 && records <= 1000000, "invalid structural record count");
  std::array<uint32_t, kJsonFields + 1> fragment_sizes{};
  for (uint32_t& size : fragment_sizes) size = reader.u32();
  std::array<std::vector<uint8_t>, kJsonFields + 1> fragments;
  for (size_t i = 0; i < fragments.size(); ++i) {
    const size_t size = fragment_sizes[i];
    terra::require(size <= reader.remaining(), "truncated structural fragment");
    const uint8_t* bytes = reader.bytes(size);
    fragments[i].assign(bytes, bytes + size);
  }
  terra::require(original_size <= std::numeric_limits<size_t>::max() - records,
                 "structural column bound overflow");
  const size_t maximum_column = original_size + records;
#ifdef TERRA_ROW_MAJOR_COLUMNS
  terra::require(size_t(records) <= std::numeric_limits<size_t>::max() / kJsonFields &&
                 original_size <= kMaximumObjectBytes - size_t(records) * kJsonFields,
                 "structural row-major bound overflow");
  const size_t maximum_row_major = original_size + size_t(records) * kJsonFields;
#endif
  std::array<std::vector<uint8_t>, kJsonFields> columns;
  std::vector<uint8_t> row_major;
  if (row_major_layout) {
#ifdef TERRA_ROW_MAJOR_COLUMNS
    const uint32_t packed_size = reader.u32();
    terra::require(packed_size <= reader.remaining(), "truncated row-major structural stream");
    const uint8_t* packed = reader.bytes(packed_size);
    row_major = decode_lz_object(std::vector<uint8_t>(packed, packed + packed_size), maximum_row_major);
#endif
  } else {
    for (size_t field = 0; field < kJsonFields; ++field) {
      const uint32_t packed_size = reader.u32();
      terra::require(packed_size <= reader.remaining(), "truncated structural column");
      const uint8_t* packed = reader.bytes(packed_size);
      columns[field] = decode_lz_object(std::vector<uint8_t>(packed, packed + packed_size),
                                        maximum_column);
    }
  }
  reader.finish();

  std::array<size_t, kJsonFields> positions{};
  size_t row_major_position = 0;
  std::vector<uint8_t> output;
  output.reserve(original_size);
  const auto append_checked = [&](const uint8_t* bytes, size_t size) {
    terra::require(size <= original_size - output.size(), "structural output exceeds declared length");
    if (size != 0) output.insert(output.end(), bytes, bytes + size);
  };
  for (uint32_t row = 0; row < records; ++row) {
    append_checked(fragments[0].data(), fragments[0].size());
    for (size_t field = 0; field < kJsonFields; ++field) {
      const std::vector<uint8_t>& column = row_major_layout ? row_major : columns[field];
      size_t& position = row_major_layout ? row_major_position : positions[field];
      terra::require(position < column.size(), "missing structural value");
      const void* found = std::memchr(column.data() + position, 0, column.size() - position);
      terra::require(found != nullptr, "missing structural delimiter");
      const uint8_t* delimiter = static_cast<const uint8_t*>(found);
      append_checked(column.data() + position, size_t(delimiter - (column.data() + position)));
      position = size_t(delimiter - column.data()) + 1;
      append_checked(fragments[field + 1].data(), fragments[field + 1].size());
    }
  }
  if (row_major_layout) {
    terra::require(row_major_position == row_major.size(), "trailing row-major structural values");
  } else {
    for (size_t field = 0; field < kJsonFields; ++field) {
      terra::require(positions[field] == columns[field].size(), "trailing structural values");
    }
  }
  terra::require(output.size() == original_size, "structural output length mismatch");
  return output;
}

std::vector<uint8_t> decode_attribute_json(const std::vector<uint8_t>& archive) {
  constexpr size_t kAttributesField = 11;
  terra::require(archive.size() >= 82, "truncated attribute archive");
  const size_t checksum_offset = archive.size() - 4;
  const uint32_t expected_object_crc = (uint32_t(archive[checksum_offset]) << 24) |
                                       (uint32_t(archive[checksum_offset + 1]) << 16) |
                                       (uint32_t(archive[checksum_offset + 2]) << 8) |
                                       uint32_t(archive[checksum_offset + 3]);
  terra::require(terra::crc32(archive.data(), checksum_offset) == expected_object_crc,
                 "attribute checksum mismatch");
  terra::Reader reader(archive.data(), checksum_offset);
  reader.expect("TSJ2", 4, "wrong attribute magic");
  terra::require(reader.u8() == 1, "unsupported attribute version");
  terra::require(reader.u8() == kJsonFields, "unsupported attribute field count");
  const uint64_t original_size64 = reader.u64();
  terra::require(original_size64 <= kMaximumObjectBytes, "attribute object too large");
  const size_t original_size = static_cast<size_t>(original_size64);
  const uint32_t records = reader.u32();
  terra::require(records != 0 && records <= 1000000, "invalid attribute record count");
  std::array<uint32_t, kJsonFields + 1> fragment_sizes{};
  for (uint32_t& size : fragment_sizes) size = reader.u32();
  std::array<std::vector<uint8_t>, kJsonFields + 1> fragments;
  for (size_t i = 0; i < fragments.size(); ++i) {
    const size_t size = fragment_sizes[i];
    terra::require(size <= reader.remaining(), "truncated attribute fragment");
    const uint8_t* bytes = reader.bytes(size);
    fragments[i].assign(bytes, bytes + size);
  }
  const uint32_t key_count = reader.u32();
  terra::require(key_count <= 255, "too many attribute keys");
  std::vector<std::vector<uint8_t>> keys;
  keys.reserve(key_count);
  for (uint32_t id = 0; id < key_count; ++id) {
    const uint32_t size = reader.u32();
    terra::require(size >= 2 && size <= reader.remaining(), "invalid attribute key");
    const uint8_t* bytes = reader.bytes(size);
    terra::require(bytes[0] == '"' && bytes[size - 1] == '"', "invalid attribute key token");
    for (uint32_t previous = 0; previous < id; ++previous) {
      terra::require(keys[previous].size() != size ||
                         std::memcmp(keys[previous].data(), bytes, size) != 0,
                     "duplicate attribute key");
    }
    keys.emplace_back(bytes, bytes + size);
  }
  terra::require(original_size <= std::numeric_limits<size_t>::max() - records,
                 "attribute column bound overflow");
  const size_t maximum_column = original_size + records;
  const auto read_embedded = [&](size_t limit) {
    const uint32_t packed_size = reader.u32();
    terra::require(packed_size <= reader.remaining(), "truncated embedded stream");
    const uint8_t* packed = reader.bytes(packed_size);
    return decode_lz_object(std::vector<uint8_t>(packed, packed + packed_size), limit);
  };
  std::array<std::vector<uint8_t>, kJsonFields> columns;
  for (size_t field = 0; field < kJsonFields; ++field) {
    if (field != kAttributesField) columns[field] = read_embedded(maximum_column);
  }
  std::vector<uint8_t> order = read_embedded(maximum_column);
  std::vector<std::vector<uint8_t>> values_by_key;
  values_by_key.reserve(key_count);
  for (uint32_t id = 0; id < key_count; ++id) values_by_key.push_back(read_embedded(maximum_column));
  reader.finish();

  std::array<size_t, kJsonFields> positions{};
  std::vector<size_t> value_positions(key_count, 0);
  size_t order_position = 0;
  std::vector<uint8_t> output;
  output.reserve(original_size);
  const auto append_checked = [&](const uint8_t* bytes, size_t size) {
    terra::require(size <= original_size - output.size(), "attribute output exceeds declared length");
    if (size != 0) output.insert(output.end(), bytes, bytes + size);
  };
  const auto append_delimited = [&](const std::vector<uint8_t>& column, size_t& position) {
    terra::require(position < column.size(), "missing delimited value");
    const void* found = std::memchr(column.data() + position, 0, column.size() - position);
    terra::require(found != nullptr, "missing value delimiter");
    const uint8_t* delimiter = static_cast<const uint8_t*>(found);
    append_checked(column.data() + position, size_t(delimiter - (column.data() + position)));
    position = size_t(delimiter - column.data()) + 1;
  };
  for (uint32_t row = 0; row < records; ++row) {
    append_checked(fragments[0].data(), fragments[0].size());
    for (size_t field = 0; field < kJsonFields; ++field) {
      if (field != kAttributesField) {
        append_delimited(columns[field], positions[field]);
      } else {
        terra::require(order_position < order.size(), "missing attribute order");
        const uint8_t tag = order[order_position++];
        if (tag == 0) {
          append_checked(reinterpret_cast<const uint8_t*>("null"), 4);
        } else {
          const size_t count = tag - 1;
          append_checked(reinterpret_cast<const uint8_t*>("{"), 1);
          for (size_t entry = 0; entry < count; ++entry) {
            terra::require(order_position < order.size(), "truncated attribute key order");
            const uint8_t key_id = order[order_position++];
            terra::require(key_id < key_count, "attribute key ID out of range");
            if (entry != 0) append_checked(reinterpret_cast<const uint8_t*>(","), 1);
            append_checked(keys[key_id].data(), keys[key_id].size());
            append_checked(reinterpret_cast<const uint8_t*>(":"), 1);
            append_delimited(values_by_key[key_id], value_positions[key_id]);
          }
          append_checked(reinterpret_cast<const uint8_t*>("}"), 1);
        }
      }
      append_checked(fragments[field + 1].data(), fragments[field + 1].size());
    }
  }
  for (size_t field = 0; field < kJsonFields; ++field) {
    if (field != kAttributesField) {
      terra::require(positions[field] == columns[field].size(), "trailing outer values");
    }
  }
  terra::require(order_position == order.size(), "trailing attribute order");
  for (size_t id = 0; id < values_by_key.size(); ++id) {
    terra::require(value_positions[id] == values_by_key[id].size(), "trailing attribute values");
  }
  terra::require(output.size() == original_size, "attribute output length mismatch");
  return output;
}

#endif

#ifndef TERRA_MAXIMUM
std::vector<uint8_t> decode_attribute_hour_json(const std::vector<uint8_t>& archive) {
  constexpr size_t kAttributesField = 11;
  constexpr size_t kHoursField = 13;
  terra::require(archive.size() >= 82, "truncated attribute-hour archive");
  const size_t checksum_offset = archive.size() - 4;
  const uint32_t expected_object_crc = (uint32_t(archive[checksum_offset]) << 24) |
                                       (uint32_t(archive[checksum_offset + 1]) << 16) |
                                       (uint32_t(archive[checksum_offset + 2]) << 8) |
                                       uint32_t(archive[checksum_offset + 3]);
  terra::require(terra::crc32(archive.data(), checksum_offset) == expected_object_crc,
                 "attribute-hour checksum mismatch");
  terra::Reader reader(archive.data(), checksum_offset);
  reader.expect("TSJ3", 4, "wrong attribute-hour magic");
  terra::require(reader.u8() == 1, "unsupported attribute-hour version");
  terra::require(reader.u8() == kJsonFields, "unsupported attribute-hour field count");
  const uint64_t original_size64 = reader.u64();
  terra::require(original_size64 <= kMaximumObjectBytes, "attribute-hour object too large");
  const size_t original_size = static_cast<size_t>(original_size64);
  const uint32_t records = reader.u32();
  terra::require(records != 0 && records <= 1000000, "invalid attribute-hour record count");
  std::array<uint32_t, kJsonFields + 1> fragment_sizes{};
  for (uint32_t& size : fragment_sizes) size = reader.u32();
  std::array<std::vector<uint8_t>, kJsonFields + 1> fragments;
  for (size_t i = 0; i < fragments.size(); ++i) {
    const size_t size = fragment_sizes[i];
    terra::require(size <= reader.remaining(), "truncated attribute-hour fragment");
    const uint8_t* bytes = reader.bytes(size);
    fragments[i].assign(bytes, bytes + size);
  }
  const auto read_key_dictionary = [&](const char* error_prefix) {
    const uint32_t count = reader.u32();
    terra::require(count <= 255, error_prefix);
    std::vector<std::vector<uint8_t>> dictionary;
    dictionary.reserve(count);
    for (uint32_t id = 0; id < count; ++id) {
      const uint32_t size = reader.u32();
      terra::require(size >= 2 && size <= reader.remaining(), "invalid nested key");
      const uint8_t* bytes = reader.bytes(size);
      terra::require(bytes[0] == '"' && bytes[size - 1] == '"', "invalid nested key token");
      for (uint32_t previous = 0; previous < id; ++previous) {
        terra::require(dictionary[previous].size() != size ||
                           std::memcmp(dictionary[previous].data(), bytes, size) != 0,
                       "duplicate nested key");
      }
      dictionary.emplace_back(bytes, bytes + size);
    }
    return dictionary;
  };
  const std::vector<std::vector<uint8_t>> attribute_keys = read_key_dictionary("too many attribute keys");
  const std::vector<std::vector<uint8_t>> weekday_keys = read_key_dictionary("too many weekday keys");
  const uint32_t pattern_count = reader.u32();
  terra::require(pattern_count <= 255, "too many hour patterns");
  std::vector<std::vector<uint8_t>> patterns;
  patterns.reserve(pattern_count);
  for (uint32_t id = 0; id < pattern_count; ++id) {
    const uint8_t size = reader.u8();
    terra::require(size <= weekday_keys.size(), "invalid hour pattern size");
    const uint8_t* bytes = reader.bytes(size);
    for (uint8_t entry = 0; entry < size; ++entry) {
      terra::require(bytes[entry] < weekday_keys.size(), "hour pattern key ID out of range");
    }
    patterns.emplace_back(bytes, bytes + size);
  }
  terra::require(original_size <= std::numeric_limits<size_t>::max() - records,
                 "attribute-hour column bound overflow");
  const size_t maximum_column = std::min(kMaximumObjectBytes, original_size + records);
  const auto read_embedded = [&](size_t limit) {
    const uint32_t packed_size = reader.u32();
    terra::require(packed_size <= reader.remaining(), "truncated embedded stream");
    const uint8_t* packed = reader.bytes(packed_size);
    return decode_lz_object(std::vector<uint8_t>(packed, packed + packed_size), limit);
  };
  std::array<std::vector<uint8_t>, kJsonFields> columns;
  for (size_t field = 0; field < kJsonFields; ++field) {
    if (field != kAttributesField && field != kHoursField) columns[field] = read_embedded(maximum_column);
  }
  const std::vector<uint8_t> attribute_order = read_embedded(maximum_column);
  std::vector<std::vector<uint8_t>> attribute_values;
  attribute_values.reserve(attribute_keys.size());
  for (size_t id = 0; id < attribute_keys.size(); ++id) attribute_values.push_back(read_embedded(maximum_column));
  const std::vector<uint8_t> hour_order = read_embedded(maximum_column);
  std::vector<std::vector<uint8_t>> hour_values;
  hour_values.reserve(weekday_keys.size());
  for (size_t id = 0; id < weekday_keys.size(); ++id) hour_values.push_back(read_embedded(maximum_column));
  reader.finish();

  std::array<size_t, kJsonFields> positions{};
  std::vector<size_t> attribute_positions(attribute_keys.size(), 0);
  std::vector<size_t> hour_positions(weekday_keys.size(), 0);
  size_t attribute_order_position = 0;
  size_t hour_order_position = 0;
  std::vector<uint8_t> output;
  output.reserve(original_size);
  const auto append_checked = [&](const uint8_t* bytes, size_t size) {
    terra::require(size <= original_size - output.size(), "attribute-hour output exceeds declared length");
    if (size != 0) output.insert(output.end(), bytes, bytes + size);
  };
  const auto append_delimited = [&](const std::vector<uint8_t>& column, size_t& position) {
    terra::require(position < column.size(), "missing delimited value");
    const void* found = std::memchr(column.data() + position, 0, column.size() - position);
    terra::require(found != nullptr, "missing value delimiter");
    const uint8_t* delimiter = static_cast<const uint8_t*>(found);
    append_checked(column.data() + position, size_t(delimiter - (column.data() + position)));
    position = size_t(delimiter - column.data()) + 1;
  };
  for (uint32_t row = 0; row < records; ++row) {
    append_checked(fragments[0].data(), fragments[0].size());
    for (size_t field = 0; field < kJsonFields; ++field) {
      if (field != kAttributesField && field != kHoursField) {
        append_delimited(columns[field], positions[field]);
      } else if (field == kAttributesField) {
        terra::require(attribute_order_position < attribute_order.size(), "missing attribute order");
        const uint8_t tag = attribute_order[attribute_order_position++];
        if (tag == 0) {
          append_checked(reinterpret_cast<const uint8_t*>("null"), 4);
        } else {
          const size_t count = tag - 1;
          append_checked(reinterpret_cast<const uint8_t*>("{"), 1);
          for (size_t entry = 0; entry < count; ++entry) {
            terra::require(attribute_order_position < attribute_order.size(), "truncated attribute key order");
            const uint8_t key_id = attribute_order[attribute_order_position++];
            terra::require(key_id < attribute_keys.size(), "attribute key ID out of range");
            if (entry != 0) append_checked(reinterpret_cast<const uint8_t*>(","), 1);
            append_checked(attribute_keys[key_id].data(), attribute_keys[key_id].size());
            append_checked(reinterpret_cast<const uint8_t*>(":"), 1);
            append_delimited(attribute_values[key_id], attribute_positions[key_id]);
          }
          append_checked(reinterpret_cast<const uint8_t*>("}"), 1);
        }
      } else {
        terra::require(hour_order_position < hour_order.size(), "missing hour order");
        const uint8_t tag = hour_order[hour_order_position++];
        if (tag == 0) {
          append_checked(reinterpret_cast<const uint8_t*>("null"), 4);
        } else {
          const size_t pattern_id = tag - 1;
          terra::require(pattern_id < patterns.size(), "hour pattern ID out of range");
          const auto& pattern = patterns[pattern_id];
          append_checked(reinterpret_cast<const uint8_t*>("{"), 1);
          for (size_t entry = 0; entry < pattern.size(); ++entry) {
            const uint8_t key_id = pattern[entry];
            if (entry != 0) append_checked(reinterpret_cast<const uint8_t*>(","), 1);
            append_checked(weekday_keys[key_id].data(), weekday_keys[key_id].size());
            append_checked(reinterpret_cast<const uint8_t*>(":"), 1);
            append_delimited(hour_values[key_id], hour_positions[key_id]);
          }
          append_checked(reinterpret_cast<const uint8_t*>("}"), 1);
        }
      }
      append_checked(fragments[field + 1].data(), fragments[field + 1].size());
    }
  }
  for (size_t field = 0; field < kJsonFields; ++field) {
    if (field != kAttributesField && field != kHoursField) {
      terra::require(positions[field] == columns[field].size(), "trailing outer values");
    }
  }
  terra::require(attribute_order_position == attribute_order.size(), "trailing attribute order");
  for (size_t id = 0; id < attribute_values.size(); ++id) {
    terra::require(attribute_positions[id] == attribute_values[id].size(), "trailing attribute values");
  }
  terra::require(hour_order_position == hour_order.size(), "trailing hour order");
  for (size_t id = 0; id < hour_values.size(); ++id) {
    terra::require(hour_positions[id] == hour_values[id].size(), "trailing hour values");
  }
  terra::require(output.size() == original_size, "attribute-hour output length mismatch");
  return output;
}
#endif  // TERRA_MAXIMUM

#ifdef TERRA_MAXIMUM

using ByteDictionary = std::vector<std::vector<uint8_t>>;

ByteDictionary read_dictionary(terra::Reader& reader, size_t maximum_bytes) {
  const uint32_t marker = reader.u32();
  const bool compressed_body = (marker & 0x80000000U) != 0;
  const uint32_t count = marker & 0x7fffffffU;
  terra::require(count <= 1000000, "too many dictionary entries");
  ByteDictionary dictionary;
  dictionary.reserve(count);
  std::unordered_set<std::string> seen;
  seen.reserve(static_cast<size_t>(count) * 2 + 1);
  if (compressed_body) {
#ifdef TERRA_BWT
    std::vector<uint32_t> lengths;
    lengths.reserve(count);
    size_t total = 0;
    for (uint32_t index = 0; index < count; ++index) {
      const uint32_t size = reader.u32();
      terra::require(size <= maximum_bytes - total, "invalid compressed dictionary length");
      total += size;
      lengths.push_back(size);
    }
    const uint32_t packed_size = reader.u32();
    terra::require(packed_size <= reader.remaining(), "truncated compressed dictionary body");
    const uint8_t* packed = reader.bytes(packed_size);
    const std::vector<uint8_t> body = terra::decode_bwt_object(
        std::vector<uint8_t>(packed, packed + packed_size), maximum_bytes);
    terra::require(body.size() == total, "compressed dictionary body length mismatch");
    size_t position = 0;
    for (uint32_t size : lengths) {
      terra::require(size <= body.size() - position, "compressed dictionary split overflow");
      const uint8_t* bytes = body.data() + position;
      position += size;
      std::string identity(reinterpret_cast<const char*>(bytes), size);
      terra::require(seen.emplace(std::move(identity)).second, "duplicate dictionary entry");
      dictionary.emplace_back(bytes, bytes + size);
    }
    return dictionary;
#else
    terra::die("unsupported compressed dictionary body");
#endif
  }
  size_t total = 0;
  for (uint32_t index = 0; index < count; ++index) {
    const uint32_t size = reader.u32();
    terra::require(size <= reader.remaining() && size <= maximum_bytes - total,
                   "invalid dictionary entry length");
    const uint8_t* bytes = reader.bytes(size);
    total += size;
    std::string identity(reinterpret_cast<const char*>(bytes), size);
    terra::require(seen.emplace(std::move(identity)).second, "duplicate dictionary entry");
    dictionary.emplace_back(bytes, bytes + size);
  }
  return dictionary;
}

uint32_t dictionary_maximum_symbol(const ByteDictionary& dictionary) {
  terra::require(!dictionary.empty(), "empty dictionary for nonempty symbol stream");
  terra::require(dictionary.size() <= std::numeric_limits<uint32_t>::max(), "dictionary too large");
  return static_cast<uint32_t>(dictionary.size() - 1);
}

// TFC1 is an independently checked front-coded raw column.  The inner body is
// a normal TLZ2/TBW1 object; each decoded record is a nonzero, canonical
// unsigned LEB128 prefix length plus a NUL-terminated suffix.  The prefix is
// relative to the preceding reconstructed token, so this routine returns the
// ordinary NUL-delimited raw column expected by the common reconstruction path.
#ifdef TERRA_FRONT_CODED
uint32_t read_front_prefix_plus_one(terra::Reader& reader) {
  uint32_t value = 0;
  unsigned shift = 0;
  for (unsigned bytes = 0; bytes != 5; ++bytes) {
    const uint8_t byte = reader.u8();
    const uint32_t part = byte & 127U;
    terra::require(shift < 32 && part <= (std::numeric_limits<uint32_t>::max() >> shift),
                   "front prefix varint overflow");
    value |= part << shift;
    if ((byte & 128U) == 0) {
      terra::require(value != 0, "zero front prefix varint");
      terra::require(bytes == 0 || part != 0, "noncanonical front prefix varint");
      return value;
    }
    shift += 7;
  }
  terra::die("front prefix varint too long");
}

std::vector<uint8_t> decode_front_coded_field(const uint8_t* packed, size_t packed_size,
                                              size_t maximum_output, uint32_t expected_records) {
  terra::require(expected_records != 0, "invalid expected front record count");
  terra::Reader wrapper(packed, packed_size);
  wrapper.expect("TFC1", 4, "wrong front field magic");
  terra::require(wrapper.u8() == 1, "unsupported front field version");
  const uint32_t raw_size32 = wrapper.u32();
  const uint32_t records = wrapper.u32();
  terra::require(records == expected_records, "front field record count mismatch");
  terra::require(raw_size32 <= maximum_output && raw_size32 >= records,
                 "invalid front field raw size");
  const uint32_t inner_size = wrapper.u32();
  terra::require(inner_size <= wrapper.remaining(), "truncated front field body");
  const uint8_t* inner_bytes = wrapper.bytes(inner_size);
  wrapper.finish();

  // A front record has one terminator and at most five LEB128 bytes in place
  // of a raw token prefix.  This is an exact safe upper bound for a valid
  // input and keeps an adversarial inner container from allocating freely.
  size_t maximum_front = raw_size32;
  terra::require(size_t(records) <= (std::numeric_limits<size_t>::max() - maximum_front) / 4,
                 "front field bound overflow");
  maximum_front += size_t(records) * 4;
  terra::require(inner_size >= 4, "truncated front inner codec");
  const std::vector<uint8_t> inner(inner_bytes, inner_bytes + inner_size);
  std::vector<uint8_t> front;
  if (std::memcmp(inner.data(), "TLZ2", 4) == 0) {
    front = decode_lz_object(inner, maximum_front);
#ifdef TERRA_LZ_HUFFMAN
  } else if (std::memcmp(inner.data(), "TLH1", 4) == 0) {
    front = decode_lz_object(inner, maximum_front);
#endif
#ifdef TERRA_BWT
  } else if (std::memcmp(inner.data(), "TBW1", 4) == 0) {
    front = terra::decode_bwt_object(inner, maximum_front);
#endif
  } else {
    terra::die("unknown front inner codec");
  }

  terra::Reader front_reader(front);
  std::vector<uint8_t> output;
  output.reserve(raw_size32);
  std::vector<uint8_t> previous;
  for (uint32_t row = 0; row < records; ++row) {
    const uint32_t prefix_plus_one = read_front_prefix_plus_one(front_reader);
    const size_t prefix = size_t(prefix_plus_one - 1);
    terra::require(prefix <= previous.size(), "front prefix exceeds previous token");
    terra::require(front_reader.remaining() != 0, "missing front suffix delimiter");
    const uint8_t* suffix_begin = front_reader.current();
    const void* delimiter_found = std::memchr(suffix_begin, 0, front_reader.remaining());
    terra::require(delimiter_found != nullptr, "missing front suffix delimiter");
    const uint8_t* delimiter = static_cast<const uint8_t*>(delimiter_found);
    const size_t suffix_size = static_cast<size_t>(delimiter - suffix_begin);
    terra::require(prefix <= std::numeric_limits<size_t>::max() - suffix_size,
                   "front token length overflow");
    const size_t token_size = prefix + suffix_size;
    terra::require(token_size < raw_size32 && output.size() <= raw_size32 - token_size - 1,
                   "front field output exceeds declared length");
    previous.resize(prefix);
    previous.insert(previous.end(), suffix_begin, delimiter);
    output.insert(output.end(), previous.begin(), previous.end());
    output.push_back(0);
    (void)front_reader.bytes(suffix_size + 1);
  }
  front_reader.finish();
  terra::require(output.size() == raw_size32, "front field raw length mismatch");
  return output;
}
#endif

std::vector<uint8_t> decode_raw_typed_field(const uint8_t* packed, size_t packed_size,
                                            size_t maximum_output, uint32_t expected_records) {
  terra::require(packed_size >= 4, "truncated typed raw field");
  std::vector<uint8_t> archive(packed, packed + packed_size);
  if (std::memcmp(packed, "TLZ2", 4) == 0) return decode_lz_object(archive, maximum_output);
#ifdef TERRA_LZ_HUFFMAN
  if (std::memcmp(packed, "TLH1", 4) == 0) return decode_lz_object(archive, maximum_output);
#endif
#ifdef TERRA_BWT
  if (std::memcmp(packed, "TBW1", 4) == 0) return terra::decode_bwt_object(archive, maximum_output);
#endif
#ifdef TERRA_FRONT_CODED
  if (std::memcmp(packed, "TFC1", 4) == 0) {
    return decode_front_coded_field(packed, packed_size, maximum_output, expected_records);
  }
#endif
  terra::die("unknown typed raw field codec");
}

struct DecodedAttributes {
  ByteDictionary keys;
  ByteDictionary values;
  std::vector<uint32_t> counts;
  std::vector<uint32_t> key_order;
  std::vector<std::vector<uint32_t>> values_by_key;
};

struct DecodedCategories {
  ByteDictionary labels;
  std::vector<uint32_t> counts;
  std::vector<uint32_t> values;
  bool contextual = false;
  std::vector<uint32_t> predictions;
  std::vector<uint32_t> first_values;
  std::vector<uint32_t> flags;
  std::vector<uint32_t> escapes;
};

struct DecodedHours {
  ByteDictionary keys;
  ByteDictionary values;
  std::vector<std::vector<uint8_t>> patterns;
  std::vector<uint32_t> pattern_ids;
  std::vector<std::vector<uint32_t>> values_by_key;
};

struct TypedField {
  uint8_t kind = 0;
  std::vector<uint8_t> raw;
  std::vector<uint8_t> packed_ids;
  ByteDictionary dictionary;
  std::vector<uint32_t> values;
  DecodedAttributes attributes;
  DecodedCategories categories;
  DecodedHours hours;
};

DecodedAttributes read_attribute_field(terra::Reader& reader, uint32_t records,
                                       size_t maximum_bytes) {
  DecodedAttributes result;
  result.keys = read_dictionary(reader, maximum_bytes);
  result.values = read_dictionary(reader, maximum_bytes);
  const uint64_t maximum_tag64 = uint64_t(result.keys.size()) + 1;
  terra::require(maximum_tag64 <= std::numeric_limits<uint32_t>::max(), "too many attribute keys");
  result.counts = terra::read_huffman_stream(reader, records, static_cast<uint32_t>(maximum_tag64));
  uint64_t total_keys = 0;
  for (uint32_t tag : result.counts) {
    terra::require(tag == 0 || tag <= maximum_tag64, "invalid attribute count");
    if (tag != 0) total_keys += tag - 1;
    terra::require(total_keys <= maximum_bytes, "attribute key stream too large");
  }
  terra::require(total_keys <= std::numeric_limits<uint32_t>::max(), "attribute key stream overflow");
  if (total_keys == 0) {
    result.key_order = terra::read_huffman_stream(reader, 0, 0);
  } else {
    result.key_order = terra::read_huffman_stream(reader, static_cast<uint32_t>(total_keys),
                                                   dictionary_maximum_symbol(result.keys));
  }
  std::vector<uint32_t> occurrences(result.keys.size(), 0);
  for (uint32_t key : result.key_order) {
    terra::require(key < occurrences.size(), "attribute key ID out of range");
    terra::require(occurrences[key] != std::numeric_limits<uint32_t>::max(),
                   "attribute value count overflow");
    ++occurrences[key];
  }
  result.values_by_key.resize(result.keys.size());
  for (size_t key = 0; key < result.keys.size(); ++key) {
    if (occurrences[key] == 0) {
      result.values_by_key[key] = terra::read_huffman_stream(reader, 0, 0);
    } else {
      result.values_by_key[key] = terra::read_huffman_stream(
          reader, occurrences[key], dictionary_maximum_symbol(result.values));
    }
  }
  return result;
}

DecodedCategories read_categories_field(terra::Reader& reader, uint32_t records,
                                        size_t maximum_bytes) {
  DecodedCategories result;
  result.labels = read_dictionary(reader, maximum_bytes);
  result.counts = terra::read_huffman_stream(reader, records, static_cast<uint32_t>(maximum_bytes));
  uint64_t total_labels = 0;
  for (uint32_t tag : result.counts) {
    if (tag != 0) {
      terra::require(tag >= 2, "invalid category list count");
      total_labels += tag - 1;
    }
    terra::require(total_labels <= maximum_bytes, "category label stream too large");
  }
  terra::require(total_labels <= std::numeric_limits<uint32_t>::max(), "category label stream overflow");
  if (total_labels == 0) {
    result.values = terra::read_huffman_stream(reader, 0, 0);
  } else {
    result.values = terra::read_huffman_stream(reader, static_cast<uint32_t>(total_labels),
                                                dictionary_maximum_symbol(result.labels));
  }
  return result;
}

#ifdef TERRA_CATEGORY_CONTEXT
DecodedCategories read_categories_context_field(terra::Reader& reader, uint32_t records,
                                                size_t maximum_bytes) {
  constexpr uint32_t kNoPrediction = 0xffffffffU;
  DecodedCategories result;
  result.contextual = true;
  result.labels = read_dictionary(reader, maximum_bytes);
  result.counts = terra::read_huffman_stream(reader, records, static_cast<uint32_t>(maximum_bytes));
  uint64_t total_labels = 0;
  uint32_t list_count = 0;
  for (uint32_t tag : result.counts) {
    if (tag == 0) continue;
    terra::require(tag >= 2, "invalid category context list count");
    ++list_count;
    total_labels += tag - 1;
    terra::require(total_labels <= maximum_bytes, "category context stream too large");
  }
  terra::require(total_labels >= list_count && total_labels <= std::numeric_limits<uint32_t>::max(),
                 "invalid category context counts");
  const uint32_t prediction_count = reader.u32();
  terra::require(prediction_count <= result.labels.size(), "too many category predictions");
  result.predictions.assign(result.labels.size(), kNoPrediction);
  uint32_t previous = 0;
  for (uint32_t index = 0; index < prediction_count; ++index) {
    const uint32_t source = reader.u32();
    const uint32_t target = reader.u32();
    terra::require(source < result.labels.size() && target < result.labels.size() &&
                   (index == 0 || previous < source), "invalid category prediction");
    previous = source;
    result.predictions[source] = target;
  }
  if (list_count == 0) {
    result.first_values = terra::read_huffman_stream(reader, 0, 0);
  } else {
    result.first_values = terra::read_huffman_stream(reader, list_count,
                                                      dictionary_maximum_symbol(result.labels));
  }
  const uint32_t transition_count = static_cast<uint32_t>(total_labels) - list_count;
  result.flags = terra::read_huffman_stream(reader, transition_count, 1);
  uint32_t escape_count = 0;
  for (uint32_t flag : result.flags) {
    terra::require(flag <= 1, "invalid category prediction flag");
    escape_count += flag;
  }
  if (escape_count == 0) {
    result.escapes = terra::read_huffman_stream(reader, 0, 0);
  } else {
    result.escapes = terra::read_huffman_stream(reader, escape_count,
                                                 dictionary_maximum_symbol(result.labels));
  }
  return result;
}
#endif

DecodedHours read_hours_field(terra::Reader& reader, uint32_t records, size_t maximum_bytes) {
  DecodedHours result;
  result.keys = read_dictionary(reader, maximum_bytes);
  result.values = read_dictionary(reader, maximum_bytes);
  const uint32_t pattern_count = reader.u32();
  terra::require(pattern_count <= records, "too many hour patterns");
  result.patterns.reserve(pattern_count);
  std::unordered_set<std::string> seen;
  seen.reserve(static_cast<size_t>(pattern_count) * 2 + 1);
  for (uint32_t pattern_id = 0; pattern_id < pattern_count; ++pattern_id) {
    const uint32_t size = reader.u32();
    terra::require(size <= result.keys.size(), "invalid hour pattern length");
    const uint8_t* bytes = reader.bytes(size);
    std::vector<uint8_t> pattern(bytes, bytes + size);
    std::vector<uint8_t> used(result.keys.size(), 0);
    for (uint8_t key : pattern) {
      terra::require(key < result.keys.size() && used[key] == 0, "invalid hour pattern key");
      used[key] = 1;
    }
    const std::string identity(reinterpret_cast<const char*>(bytes), size);
    terra::require(seen.emplace(identity).second, "duplicate hour pattern");
    result.patterns.push_back(std::move(pattern));
  }
  result.pattern_ids = terra::read_huffman_stream(reader, records, pattern_count);
  std::vector<uint32_t> occurrences(result.keys.size(), 0);
  for (uint32_t tag : result.pattern_ids) {
    terra::require(tag <= pattern_count, "hour pattern ID out of range");
    if (tag == 0) continue;
    for (uint8_t key : result.patterns[tag - 1]) {
      terra::require(occurrences[key] != std::numeric_limits<uint32_t>::max(),
                     "hour value count overflow");
      ++occurrences[key];
    }
  }
  result.values_by_key.resize(result.keys.size());
  for (size_t key = 0; key < result.keys.size(); ++key) {
    if (occurrences[key] == 0) {
      result.values_by_key[key] = terra::read_huffman_stream(reader, 0, 0);
    } else {
      result.values_by_key[key] = terra::read_huffman_stream(
          reader, occurrences[key], dictionary_maximum_symbol(result.values));
    }
  }
  return result;
}

std::vector<uint8_t> decode_typed_json(const std::vector<uint8_t>& archive) {
  constexpr size_t kBusinessIdField = 0;
  constexpr size_t kAttributesField = 11;
  constexpr size_t kCategoriesField = 12;
  constexpr size_t kHoursField = 13;
  terra::require(archive.size() >= 82, "truncated typed archive");
  const size_t checksum_offset = archive.size() - 4;
  const uint32_t expected_object_crc = (uint32_t(archive[checksum_offset]) << 24) |
                                       (uint32_t(archive[checksum_offset + 1]) << 16) |
                                       (uint32_t(archive[checksum_offset + 2]) << 8) |
                                       uint32_t(archive[checksum_offset + 3]);
  terra::require(terra::crc32(archive.data(), checksum_offset) == expected_object_crc,
                 "typed checksum mismatch");
  terra::Reader reader(archive.data(), checksum_offset);
  const uint8_t* magic = reader.bytes(4);
  bool reordered = false;
  if (std::memcmp(magic, "TSJ4", 4) != 0) {
#ifdef TERRA_ROW_REORDER
    terra::require(std::memcmp(magic, "TSJ5", 4) == 0, "wrong typed magic");
    reordered = true;
#else
    terra::die("wrong typed magic");
#endif
  }
  terra::require(reader.u8() == 1, "unsupported typed version");
  terra::require(reader.u8() == kJsonFields, "unsupported typed field count");
  const uint64_t original_size64 = reader.u64();
  terra::require(original_size64 != 0 && original_size64 <= kMaximumObjectBytes, "typed object too large");
  const size_t original_size = static_cast<size_t>(original_size64);
  const uint32_t records = reader.u32();
  terra::require(records != 0 && records <= 1000000, "invalid typed record count");
  std::array<uint32_t, kJsonFields + 1> fragment_sizes{};
  for (uint32_t& size : fragment_sizes) size = reader.u32();
  std::array<std::vector<uint8_t>, kJsonFields + 1> fragments;
  size_t fragment_total = 0;
  for (size_t index = 0; index < fragments.size(); ++index) {
    const size_t size = fragment_sizes[index];
    terra::require(size <= reader.remaining() && size <= original_size - fragment_total,
                   "truncated typed fragment");
    const uint8_t* bytes = reader.bytes(size);
    fragments[index].assign(bytes, bytes + size);
    fragment_total += size;
  }
#ifdef TERRA_ROW_REORDER
  std::vector<uint32_t> original_rows;
  if (reordered) {
    const uint32_t packed_size = reader.u32();
    terra::require(packed_size <= reader.remaining(), "truncated row permutation");
    const uint8_t* packed = reader.bytes(packed_size);
    const size_t raw_size = size_t(records) * 3;
    const std::vector<uint8_t> raw = decode_raw_typed_field(packed, packed_size, raw_size, records);
    terra::require(raw.size() == raw_size, "row permutation length mismatch");
    std::vector<uint8_t> seen((size_t(records) + 7) / 8, 0);
    original_rows.reserve(records);
    for (size_t position = 0; position < raw.size(); position += 3) {
      const uint32_t row = (uint32_t(raw[position]) << 16) |
                           (uint32_t(raw[position + 1]) << 8) | raw[position + 2];
      terra::require(row < records, "row permutation ID out of range");
      const size_t byte = row >> 3;
      const uint8_t bit = static_cast<uint8_t>(1U << (row & 7));
      terra::require((seen[byte] & bit) == 0, "duplicate row permutation ID");
      seen[byte] |= bit;
      original_rows.push_back(row);
    }
    terra::require(original_rows.size() == records, "incomplete row permutation");
  }
#endif
  terra::require(original_size <= std::numeric_limits<size_t>::max() - records,
                 "typed raw-column bound overflow");
  const size_t maximum_column = std::min(kMaximumObjectBytes, original_size + records);
  std::array<TypedField, kJsonFields> fields;
  for (size_t field = 0; field < kJsonFields; ++field) {
    TypedField& result = fields[field];
    result.kind = reader.u8();
    const uint32_t packed_size = reader.u32();
    terra::require(packed_size <= reader.remaining(), "truncated typed field");
    const uint8_t* packed = reader.bytes(packed_size);
    terra::Reader field_reader(packed, packed_size);
    if (result.kind == 0) {
      result.raw = decode_raw_typed_field(packed, packed_size, maximum_column, records);
      (void)field_reader.bytes(packed_size);
    } else if (result.kind == 1 && field == kBusinessIdField) {
      terra::require(packed_size == uint64_t(records) * 16, "invalid packed business ID length");
      result.packed_ids.assign(packed, packed + packed_size);
      (void)field_reader.bytes(packed_size);
    } else if (result.kind == 2 &&
               (field == 3 || field == 4 || field == 5 || field == 8 || field == 9 || field == 10)) {
      result.dictionary = read_dictionary(field_reader, original_size);
      result.values = terra::read_huffman_stream(field_reader, records,
                                                  dictionary_maximum_symbol(result.dictionary));
    } else if (result.kind == 3 && field == kAttributesField) {
      result.attributes = read_attribute_field(field_reader, records, original_size);
    } else if (result.kind == 4 && field == kCategoriesField) {
      result.categories = read_categories_field(field_reader, records, original_size);
#ifdef TERRA_CATEGORY_CONTEXT
    } else if (result.kind == 6 && field == kCategoriesField) {
      result.categories = read_categories_context_field(field_reader, records, original_size);
#endif
    } else if (result.kind == 5 && field == kHoursField) {
      result.hours = read_hours_field(field_reader, records, original_size);
    } else {
      terra::die("unknown typed field kind");
    }
    field_reader.finish();
  }
  reader.finish();

  std::array<size_t, kJsonFields> raw_positions{};
  size_t attribute_key_position = 0;
  std::vector<size_t> attribute_value_positions(fields[kAttributesField].attributes.keys.size(), 0);
  size_t category_value_position = 0;
  size_t category_first_position = 0;
  size_t category_flag_position = 0;
  size_t category_escape_position = 0;
  std::vector<size_t> hour_value_positions(fields[kHoursField].hours.keys.size(), 0);
#ifdef TERRA_ROW_REORDER
  std::vector<std::vector<uint8_t>> reordered_rows;
  if (reordered) reordered_rows.resize(records);
#endif
  std::vector<uint8_t> output;
  output.reserve(original_size);
  size_t reconstructed_size = 0;
  const auto append_checked = [&](const uint8_t* bytes, size_t size) {
    terra::require(reconstructed_size <= original_size && size <= original_size - reconstructed_size,
                   "typed output exceeds declared length");
    if (size != 0) output.insert(output.end(), bytes, bytes + size);
    reconstructed_size += size;
  };
  const auto append_delimited = [&](const std::vector<uint8_t>& column, size_t& position) {
    terra::require(position < column.size(), "missing typed raw value");
    const void* found = std::memchr(column.data() + position, 0, column.size() - position);
    terra::require(found != nullptr, "missing typed raw delimiter");
    const uint8_t* delimiter = static_cast<const uint8_t*>(found);
    append_checked(column.data() + position, static_cast<size_t>(delimiter - (column.data() + position)));
    position = static_cast<size_t>(delimiter - column.data()) + 1;
  };
  const auto append_packed_id = [&](const uint8_t* packed) {
    static constexpr char alphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    terra::require(reconstructed_size <= original_size && 24 <= original_size - reconstructed_size,
                   "packed ID exceeds typed output");
    output.push_back('"');
    uint32_t pending = 0;
    unsigned pending_bits = 0;
    for (unsigned index = 0; index < 16; ++index) {
      pending = (pending << 8) | packed[index];
      pending_bits += 8;
      while (pending_bits >= 6) {
        pending_bits -= 6;
        output.push_back(static_cast<uint8_t>(alphabet[pending >> pending_bits]));
        pending &= (uint32_t{1} << pending_bits) - 1;
      }
    }
    terra::require(pending_bits == 2, "invalid packed ID state");
    output.push_back(static_cast<uint8_t>(alphabet[pending << 4]));
    output.push_back('"');
    reconstructed_size += 24;
  };
  const auto append_attribute = [&](uint32_t row) {
    const DecodedAttributes& data = fields[kAttributesField].attributes;
    const uint32_t tag = data.counts[row];
    if (tag == 0) {
      append_checked(reinterpret_cast<const uint8_t*>("null"), 4);
      return;
    }
    const size_t count = tag - 1;
    append_checked(reinterpret_cast<const uint8_t*>("{"), 1);
    for (size_t entry = 0; entry < count; ++entry) {
      terra::require(attribute_key_position < data.key_order.size(), "truncated typed attribute order");
      const uint32_t key = data.key_order[attribute_key_position++];
      terra::require(key < data.keys.size() && attribute_value_positions[key] < data.values_by_key[key].size(),
                     "truncated typed attribute value");
      const uint32_t value = data.values_by_key[key][attribute_value_positions[key]++];
      terra::require(value < data.values.size(), "typed attribute value ID out of range");
      if (entry != 0) append_checked(reinterpret_cast<const uint8_t*>(","), 1);
      append_checked(data.keys[key].data(), data.keys[key].size());
      append_checked(reinterpret_cast<const uint8_t*>(":"), 1);
      append_checked(data.values[value].data(), data.values[value].size());
    }
    append_checked(reinterpret_cast<const uint8_t*>("}"), 1);
  };
  const auto append_categories = [&](uint32_t row) {
    const DecodedCategories& data = fields[kCategoriesField].categories;
    const uint32_t tag = data.counts[row];
    if (tag == 0) {
      append_checked(reinterpret_cast<const uint8_t*>("null"), 4);
      return;
    }
    const size_t count = tag - 1;
    append_checked(reinterpret_cast<const uint8_t*>("\""), 1);
    if (!data.contextual) {
      for (size_t index = 0; index < count; ++index) {
        terra::require(category_value_position < data.values.size(), "truncated typed category values");
        const uint32_t value = data.values[category_value_position++];
        terra::require(value < data.labels.size(), "typed category ID out of range");
        if (index != 0) append_checked(reinterpret_cast<const uint8_t*>(", "), 2);
        append_checked(data.labels[value].data(), data.labels[value].size());
      }
    } else {
      terra::require(category_first_position < data.first_values.size(),
                     "truncated contextual category first value");
      uint32_t previous = data.first_values[category_first_position++];
      terra::require(previous < data.labels.size(), "contextual category first ID out of range");
      append_checked(data.labels[previous].data(), data.labels[previous].size());
      for (size_t index = 1; index < count; ++index) {
        terra::require(category_flag_position < data.flags.size(),
                       "truncated contextual category flags");
        const uint32_t flag = data.flags[category_flag_position++];
        uint32_t value;
        if (flag == 0) {
          terra::require(previous < data.predictions.size() &&
                         data.predictions[previous] != std::numeric_limits<uint32_t>::max(),
                         "missing contextual category prediction");
          value = data.predictions[previous];
        } else {
          terra::require(flag == 1 && category_escape_position < data.escapes.size(),
                         "truncated contextual category escape");
          value = data.escapes[category_escape_position++];
        }
        terra::require(value < data.labels.size(), "contextual category ID out of range");
        append_checked(reinterpret_cast<const uint8_t*>(", "), 2);
        append_checked(data.labels[value].data(), data.labels[value].size());
        previous = value;
      }
    }
    append_checked(reinterpret_cast<const uint8_t*>("\""), 1);
  };
  const auto append_hours = [&](uint32_t row) {
    const DecodedHours& data = fields[kHoursField].hours;
    const uint32_t tag = data.pattern_ids[row];
    if (tag == 0) {
      append_checked(reinterpret_cast<const uint8_t*>("null"), 4);
      return;
    }
    terra::require(tag - 1 < data.patterns.size(), "typed hour pattern ID out of range");
    const auto& pattern = data.patterns[tag - 1];
    append_checked(reinterpret_cast<const uint8_t*>("{"), 1);
    for (size_t entry = 0; entry < pattern.size(); ++entry) {
      const uint32_t key = pattern[entry];
      terra::require(key < data.keys.size() && hour_value_positions[key] < data.values_by_key[key].size(),
                     "truncated typed hour value");
      const uint32_t value = data.values_by_key[key][hour_value_positions[key]++];
      terra::require(value < data.values.size(), "typed hour value ID out of range");
      if (entry != 0) append_checked(reinterpret_cast<const uint8_t*>(","), 1);
      append_checked(data.keys[key].data(), data.keys[key].size());
      append_checked(reinterpret_cast<const uint8_t*>(":"), 1);
      append_checked(data.values[value].data(), data.values[value].size());
    }
    append_checked(reinterpret_cast<const uint8_t*>("}"), 1);
  };
  for (uint32_t row = 0; row < records; ++row) {
#ifdef TERRA_ROW_REORDER
    const size_t row_begin = output.size();
#endif
    append_checked(fragments[0].data(), fragments[0].size());
    for (size_t field = 0; field < kJsonFields; ++field) {
      const TypedField& data = fields[field];
      if (data.kind == 0) {
        append_delimited(data.raw, raw_positions[field]);
      } else if (data.kind == 1) {
        append_packed_id(data.packed_ids.data() + size_t(row) * 16);
      } else if (data.kind == 2) {
        terra::require(row < data.values.size() && data.values[row] < data.dictionary.size(),
                       "typed dictionary value ID out of range");
        const auto& value = data.dictionary[data.values[row]];
        append_checked(value.data(), value.size());
      } else if (data.kind == 3) {
        append_attribute(row);
      } else if (data.kind == 4 || data.kind == 6) {
        append_categories(row);
      } else if (data.kind == 5) {
        append_hours(row);
      } else {
        terra::die("unknown typed field kind");
      }
      append_checked(fragments[field + 1].data(), fragments[field + 1].size());
    }
#ifdef TERRA_ROW_REORDER
    if (reordered) {
      terra::require(row < original_rows.size(), "missing row permutation ID");
      std::vector<uint8_t>& restored = reordered_rows[original_rows[row]];
      terra::require(restored.empty(), "duplicate restored row");
      restored.assign(output.begin() + row_begin, output.end());
      output.resize(row_begin);
    }
#endif
  }
  for (size_t field = 0; field < kJsonFields; ++field) {
    if (fields[field].kind == 0) terra::require(raw_positions[field] == fields[field].raw.size(),
                                                  "trailing typed raw values");
  }
  const DecodedAttributes& attributes = fields[kAttributesField].attributes;
  if (fields[kAttributesField].kind == 3) {
    terra::require(attribute_key_position == attributes.key_order.size(), "trailing typed attribute order");
    for (size_t key = 0; key < attributes.keys.size(); ++key) {
      terra::require(attribute_value_positions[key] == attributes.values_by_key[key].size(),
                     "trailing typed attribute values");
    }
  }
  if (fields[kCategoriesField].kind == 4) {
    terra::require(category_value_position == fields[kCategoriesField].categories.values.size(),
                   "trailing typed category values");
  }
  if (fields[kCategoriesField].kind == 6) {
    const DecodedCategories& categories = fields[kCategoriesField].categories;
    terra::require(category_first_position == categories.first_values.size(),
                   "trailing contextual category first values");
    terra::require(category_flag_position == categories.flags.size(),
                   "trailing contextual category flags");
    terra::require(category_escape_position == categories.escapes.size(),
                   "trailing contextual category escapes");
  }
  const DecodedHours& hours = fields[kHoursField].hours;
  if (fields[kHoursField].kind == 5) {
    for (size_t key = 0; key < hours.keys.size(); ++key) {
      terra::require(hour_value_positions[key] == hours.values_by_key[key].size(),
                     "trailing typed hour values");
    }
  }
  terra::require(reconstructed_size == original_size, "typed reconstructed length mismatch");
#ifdef TERRA_ROW_REORDER
  if (reordered) {
    terra::require(output.empty(), "unexpected reordered output prefix");
    for (const std::vector<uint8_t>& row : reordered_rows) {
      terra::require(!row.empty(), "missing restored row");
      output.insert(output.end(), row.begin(), row.end());
    }
  }
#endif
  terra::require(output.size() == original_size, "typed output length mismatch");
  return output;
}

#endif  // TERRA_MAXIMUM

std::vector<uint8_t> decode_object(const std::vector<uint8_t>& archive) {
  terra::require(archive.size() >= 4, "truncated object");
  if (std::memcmp(archive.data(), "TLZ2", 4) == 0) return decode_lz_object(archive);
#ifdef TERRA_LZ_HUFFMAN
  if (std::memcmp(archive.data(), "TLH1", 4) == 0) return decode_lz_object(archive);
#endif
#ifdef TERRA_MAXIMUM
  if (std::memcmp(archive.data(), "TSJ4", 4) == 0) return decode_typed_json(archive);
#ifdef TERRA_ROW_REORDER
  if (std::memcmp(archive.data(), "TSJ5", 4) == 0) return decode_typed_json(archive);
#endif
#else
#ifndef TERRA_GENERIC_ONLY
#ifndef TERRA_LEAN_FAST
#ifdef TERRA_ROW_MAJOR_COLUMNS
  if (std::memcmp(archive.data(), "TSJ0", 4) == 0) return decode_structural_json(archive);
#endif
  if (std::memcmp(archive.data(), "TSJ1", 4) == 0) return decode_structural_json(archive);
  if (std::memcmp(archive.data(), "TSJ2", 4) == 0) return decode_attribute_json(archive);
#endif
  if (std::memcmp(archive.data(), "TSJ3", 4) == 0) return decode_attribute_hour_json(archive);
#endif
#endif
  terra::die("wrong object magic");
}

std::vector<Record> decode_records(const std::vector<Record>& input) {
  std::vector<Record> output;
  output.reserve(input.size());
  for (const Record& record : input) output.push_back({record.alias, decode_object(record.payload)});
  return output;
}

void decode_stream() {
  const char input_magic[4] = {'H', 'B', 'A', '1'};
  const char output_magic[4] = {'H', 'B', 'I', '1'};
  auto records = terra::parse_batch(terra::read_all(std::cin), input_magic);
  auto original = terra::make_batch(decode_records(records), output_magic);
  std::cout.write(reinterpret_cast<const char*>(original.data()), original.size());
  terra::require(static_cast<bool>(std::cout), "stream write failure");
}

void decode_dir(const char* input_dir, const char* output_dir) {
  const char input_magic[4] = {'H', 'B', 'A', '1'};
  const char output_magic[4] = {'H', 'B', 'I', '1'};
  auto records = terra::read_directory(input_dir, input_magic);
  terra::write_directory(output_dir, decode_records(records), output_magic);
}

}  // namespace

int main(int argc, char** argv) {
  try {
    if (argc == 2 && std::string(argv[1]) == "decode-stream") {
      decode_stream();
    } else if (argc == 4 && std::string(argv[1]) == "decode-dir") {
      decode_dir(argv[2], argv[3]);
    } else {
      terra::die("usage: decoder decode-stream | decoder decode-dir INPUT_DIR OUTPUT_DIR");
    }
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "decoder: " << error.what() << '\n';
    return 1;
  }
}
