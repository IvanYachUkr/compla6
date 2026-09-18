#pragma once

#include "common.hpp"
#include "huffman.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <thread>

namespace terra {

#ifndef TERRA_BWT_BLOCK_BYTES
#define TERRA_BWT_BLOCK_BYTES 614400
#endif
#ifndef TERRA_BWT_TABLES
#define TERRA_BWT_TABLES 1
#endif

static_assert(TERRA_BWT_BLOCK_BYTES >= 1024 && TERRA_BWT_BLOCK_BYTES <= 4 * 1024 * 1024,
              "invalid BWT block size");
static_assert(TERRA_BWT_TABLES >= 1 && TERRA_BWT_TABLES <= 8, "invalid BWT table count");
constexpr size_t kBwtBlockBytes = TERRA_BWT_BLOCK_BYTES;
constexpr unsigned kBwtTables = TERRA_BWT_TABLES;
constexpr uint32_t kBwtAlphabet = 257;
constexpr uint32_t kBwtRunZero = 0;
constexpr uint32_t kBwtRunOne = 1;
constexpr uint32_t kBwtEnd = 258;
constexpr uint32_t kBwtTokenAlphabet = 259;

struct BwtEncodedBlock {
  uint32_t raw_size = 0;
  uint32_t checksum = 0;
  uint8_t kind = 0;  // 0 raw, 1 BWT/MTF/RLE/Huffman
  std::vector<uint8_t> data;
};

inline std::vector<uint32_t> bwt_suffix_array(const uint8_t* input, size_t input_size) {
  require(input_size <= std::numeric_limits<uint32_t>::max() - 1, "BWT block too large");
  const uint32_t size = static_cast<uint32_t>(input_size + 1);
  std::vector<uint16_t> symbols(size);
  for (size_t index = 0; index < input_size; ++index) symbols[index] = uint16_t(input[index]) + 1;
  symbols[input_size] = 0;  // a unique virtual sentinel, distinct from every byte

  std::vector<uint32_t> ranks(size);
  std::array<uint8_t, kBwtAlphabet> present{};
  std::array<uint32_t, kBwtAlphabet> initial_rank{};
  for (uint16_t symbol : symbols) present[symbol] = 1;
  uint32_t classes = 0;
  for (uint32_t symbol = 0; symbol < kBwtAlphabet; ++symbol) {
    if (present[symbol] != 0) initial_rank[symbol] = ++classes;
  }
  for (uint32_t index = 0; index < size; ++index) ranks[index] = initial_rank[symbols[index]];
  std::vector<uint32_t> suffixes(size);
  std::vector<uint32_t> temporary(size);
  std::vector<uint32_t> next_ranks(size);
  std::vector<uint32_t> counts(std::max<uint32_t>(size + 1, kBwtAlphabet + 1));

  std::fill(counts.begin(), counts.begin() + classes + 1, 0);
  for (uint32_t rank : ranks) ++counts[rank];
  uint32_t total = 0;
  for (uint32_t rank = 0; rank <= classes; ++rank) {
    const uint32_t count = counts[rank];
    counts[rank] = total;
    total += count;
  }
  for (uint32_t index = 0; index < size; ++index) suffixes[counts[ranks[index]]++] = index;

  for (uint32_t step = 1; step < size && classes < size; step <<= 1) {
    const auto counting_sort = [&](const std::vector<uint32_t>& source, std::vector<uint32_t>& target,
                                   bool second) {
      std::fill(counts.begin(), counts.begin() + classes + 1, 0);
      for (uint32_t index : source) {
        const uint32_t key = second && index + step < size ? ranks[index + step] :
                             (second ? 0 : ranks[index]);
        ++counts[key];
      }
      uint32_t position = 0;
      for (uint32_t key = 0; key <= classes; ++key) {
        const uint32_t count = counts[key];
        counts[key] = position;
        position += count;
      }
      for (uint32_t index : source) {
        const uint32_t key = second && index + step < size ? ranks[index + step] :
                             (second ? 0 : ranks[index]);
        target[counts[key]++] = index;
      }
    };
    counting_sort(suffixes, temporary, true);
    counting_sort(temporary, suffixes, false);
    uint32_t new_classes = 1;
    next_ranks[suffixes[0]] = new_classes;
    for (uint32_t rank = 1; rank < size; ++rank) {
      const uint32_t left = suffixes[rank - 1];
      const uint32_t right = suffixes[rank];
      const uint32_t left_second = left + step < size ? ranks[left + step] : 0;
      const uint32_t right_second = right + step < size ? ranks[right + step] : 0;
      if (ranks[left] != ranks[right] || left_second != right_second) ++new_classes;
      next_ranks[right] = new_classes;
    }
    ranks.swap(next_ranks);
    classes = new_classes;
    if (step > size / 2) break;
  }
  require(classes == size, "BWT suffix sort did not distinguish sentinel suffixes");
  return suffixes;
}

inline void bwt_move_to_front(uint16_t symbol, std::array<uint16_t, kBwtAlphabet>& list,
                              std::array<uint16_t, kBwtAlphabet>& positions) {
  const uint16_t position = positions[symbol];
  for (uint16_t index = position; index != 0; --index) {
    list[index] = list[index - 1];
    positions[list[index]] = index;
  }
  list[0] = symbol;
  positions[symbol] = 0;
}

inline void bwt_append_zero_run(std::vector<uint16_t>& tokens, size_t count) {
  require(count != 0, "empty BWT zero run");
  while (count != 0) {
    tokens.push_back((count & 1U) == 0 ? kBwtRunZero : kBwtRunOne);
    count >>= 1;
  }
}

inline BwtEncodedBlock encode_bwt_block(const uint8_t* input, size_t input_size) {
  require(input_size != 0 && input_size <= kBwtBlockBytes, "invalid BWT block length");
  const std::vector<uint32_t> suffixes = bwt_suffix_array(input, input_size);
  const uint32_t transformed_size = static_cast<uint32_t>(suffixes.size());
  std::vector<uint16_t> transformed(transformed_size);
  uint32_t primary = std::numeric_limits<uint32_t>::max();
  for (uint32_t index = 0; index < transformed_size; ++index) {
    const uint32_t suffix = suffixes[index];
    if (suffix == 0) primary = index;
    transformed[index] = suffix == 0 ? 0 : uint16_t(input[suffix - 1]) + 1;
  }
  require(primary < transformed_size, "missing BWT primary index");

  std::array<uint16_t, kBwtAlphabet> list{};
  std::array<uint16_t, kBwtAlphabet> positions{};
  for (uint16_t index = 0; index < kBwtAlphabet; ++index) list[index] = positions[index] = index;
  std::vector<uint16_t> tokens;
  tokens.reserve(transformed_size + 32);
  size_t zero_run = 0;
  for (uint16_t symbol : transformed) {
    const uint16_t position = positions[symbol];
    if (position == 0) {
      ++zero_run;
      continue;
    }
    if (zero_run != 0) {
      bwt_append_zero_run(tokens, zero_run);
      zero_run = 0;
    }
    tokens.push_back(uint16_t(position + 1));
    bwt_move_to_front(symbol, list, positions);
  }
  if (zero_run != 0) bwt_append_zero_run(tokens, zero_run);
  tokens.push_back(kBwtEnd);

  const unsigned table_count = std::min<unsigned>(kBwtTables, static_cast<unsigned>(tokens.size()));
  std::vector<std::vector<uint8_t>> lengths_by_table;
  std::vector<HuffmanTable> tables;
  lengths_by_table.reserve(table_count);
  tables.reserve(table_count);
  for (unsigned table_index = 0; table_index < table_count; ++table_index) {
    const size_t begin = tokens.size() * table_index / table_count;
    const size_t end = tokens.size() * (table_index + 1) / table_count;
    std::vector<uint64_t> weights(kBwtTokenAlphabet, 0);
    for (size_t index = begin; index < end; ++index) {
      require(tokens[index] < kBwtTokenAlphabet, "invalid BWT token");
      ++weights[tokens[index]];
    }
    lengths_by_table.push_back(huffman_lengths(weights));
    tables.push_back(canonical_huffman(lengths_by_table.back()));
  }
  HuffmanWriter writer;
  for (unsigned table_index = 0; table_index < table_count; ++table_index) {
    const size_t begin = tokens.size() * table_index / table_count;
    const size_t end = tokens.size() * (table_index + 1) / table_count;
    for (size_t index = begin; index < end; ++index) writer.put(tables[table_index].codes[tokens[index]]);
  }
  std::vector<uint8_t> entropy = writer.finish();
  require(entropy.size() <= std::numeric_limits<uint32_t>::max(), "BWT entropy block too large");

  BwtEncodedBlock result;
  result.raw_size = static_cast<uint32_t>(input_size);
  result.checksum = crc32(input, input_size);
  result.data.reserve(4 + (kBwtTables == 1 ? kBwtTokenAlphabet :
                           5 + size_t(table_count) * kBwtTokenAlphabet) + entropy.size());
  append_u32(result.data, primary);
  if (kBwtTables == 1) {
    result.data.insert(result.data.end(), lengths_by_table[0].begin(), lengths_by_table[0].end());
  } else {
    require(tokens.size() <= std::numeric_limits<uint32_t>::max(), "too many BWT tokens");
    append_u32(result.data, static_cast<uint32_t>(tokens.size()));
    result.data.push_back(static_cast<uint8_t>(table_count));
    for (const auto& lengths : lengths_by_table) {
      result.data.insert(result.data.end(), lengths.begin(), lengths.end());
    }
  }
  result.data.insert(result.data.end(), entropy.begin(), entropy.end());
  if (result.data.size() < input_size) {
    result.kind = 1;
  } else {
    result.kind = 0;
    result.data.assign(input, input + input_size);
  }
  return result;
}

inline std::vector<uint8_t> encode_bwt_object(const std::vector<uint8_t>& input, unsigned maximum_threads) {
  const size_t block_count_size = (input.size() + kBwtBlockBytes - 1) / kBwtBlockBytes;
  require(block_count_size <= std::numeric_limits<uint32_t>::max(), "too many BWT blocks");
  std::vector<BwtEncodedBlock> blocks(block_count_size);
  std::atomic<size_t> next{0};
  const unsigned workers = std::min<unsigned>(std::max(1U, maximum_threads),
                                               static_cast<unsigned>(block_count_size));
  const auto worker = [&] {
    for (;;) {
      const size_t index = next.fetch_add(1, std::memory_order_relaxed);
      if (index >= block_count_size) break;
      const size_t offset = index * kBwtBlockBytes;
      blocks[index] = encode_bwt_block(input.data() + offset,
                                       std::min(kBwtBlockBytes, input.size() - offset));
    }
  };
  std::vector<std::thread> helpers;
  for (unsigned worker_index = 1; worker_index < workers; ++worker_index) helpers.emplace_back(worker);
  if (workers != 0) worker();
  for (std::thread& helper : helpers) helper.join();

  size_t total = 22;
  for (const BwtEncodedBlock& block : blocks) {
    require(block.data.size() <= std::numeric_limits<uint32_t>::max(), "BWT block too large");
    require(block.data.size() <= std::numeric_limits<size_t>::max() - total - 13, "BWT object too large");
    total += 13 + block.data.size();
  }
  std::vector<uint8_t> archive;
  archive.reserve(total);
  archive.insert(archive.end(), {'T', 'B', 'W', '1'});
  archive.push_back(kBwtTables == 1 ? 1 : 2);
  archive.push_back(1);
  append_u64(archive, input.size());
  append_u32(archive, static_cast<uint32_t>(kBwtBlockBytes));
  append_u32(archive, static_cast<uint32_t>(blocks.size()));
  for (const BwtEncodedBlock& block : blocks) {
    append_u32(archive, block.raw_size);
    append_u32(archive, static_cast<uint32_t>(block.data.size()));
    append_u32(archive, block.checksum);
    archive.push_back(block.kind);
    archive.insert(archive.end(), block.data.begin(), block.data.end());
  }
  append_u32(archive, crc32(archive));
  return archive;
}

inline std::vector<uint8_t> decode_bwt_block(const uint8_t* packed, size_t packed_size,
                                             uint32_t raw_size) {
  require(raw_size != 0, "zero-length BWT block");
  Reader reader(packed, packed_size);
  const uint32_t primary = reader.u32();
  const uint32_t transformed_size = raw_size + 1;
  require(primary < transformed_size, "BWT primary index out of range");
  std::vector<uint8_t> lengths(kBwtTokenAlphabet);
  for (uint8_t& length : lengths) length = reader.u8();
  const HuffmanTable table = canonical_huffman(lengths);
  HuffmanDecoder decoder(table);
  const uint8_t* entropy = reader.current();
  const size_t entropy_size = reader.remaining();
  (void)reader.bytes(entropy_size);
  HuffmanReader bits(entropy, entropy_size);
  std::array<uint16_t, kBwtAlphabet> list{};
  std::array<uint16_t, kBwtAlphabet> positions{};
  for (uint16_t index = 0; index < kBwtAlphabet; ++index) list[index] = positions[index] = index;
  std::vector<uint16_t> transformed;
  transformed.reserve(transformed_size);
  uint64_t run = 0;
  unsigned run_bits = 0;
  const auto flush_run = [&] {
    if (run_bits == 0) return;
    require(run != 0 && run <= transformed_size - transformed.size(), "invalid BWT zero run");
    transformed.insert(transformed.end(), static_cast<size_t>(run), list[0]);
    run = 0;
    run_bits = 0;
  };
  for (;;) {
    const uint32_t token = decoder.symbol(bits);
    if (token == kBwtRunZero || token == kBwtRunOne) {
      require(run_bits < 32, "BWT zero run overflow");
      if (token == kBwtRunOne) run |= uint64_t{1} << run_bits;
      ++run_bits;
      continue;
    }
    flush_run();
    if (token == kBwtEnd) break;
    require(token >= 2 && token <= 257 && transformed.size() < transformed_size,
            "invalid BWT MTF token");
    const uint16_t position = static_cast<uint16_t>(token - 1);
    const uint16_t symbol = list[position];
    transformed.push_back(symbol);
    bwt_move_to_front(symbol, list, positions);
  }
  bits.finish_zero_padding();
  require(transformed.size() == transformed_size, "BWT transformed length mismatch");
  uint32_t sentinels = 0;
  for (uint16_t symbol : transformed) {
    require(symbol < kBwtAlphabet, "invalid BWT symbol");
    sentinels += symbol == 0;
  }
  require(sentinels == 1, "invalid BWT sentinel count");
  std::array<uint32_t, kBwtAlphabet> frequencies{};
  for (uint16_t symbol : transformed) ++frequencies[symbol];
  std::array<uint32_t, kBwtAlphabet> starts{};
  uint32_t sum = 0;
  for (uint32_t symbol = 0; symbol < kBwtAlphabet; ++symbol) {
    starts[symbol] = sum;
    sum += frequencies[symbol];
  }
  require(sum == transformed_size, "BWT frequency mismatch");
  std::array<uint32_t, kBwtAlphabet> occurrence = starts;
  std::vector<uint32_t> next(transformed_size);
  for (uint32_t index = 0; index < transformed_size; ++index) next[index] = occurrence[transformed[index]]++;
  std::vector<uint16_t> restored(transformed_size);
  uint32_t row = primary;
  for (uint32_t position = transformed_size; position != 0; --position) {
    restored[position - 1] = transformed[row];
    row = next[row];
  }
  require(row == primary && restored.back() == 0, "BWT inverse cycle mismatch");
  std::vector<uint8_t> output(raw_size);
  for (uint32_t index = 0; index < raw_size; ++index) {
    require(restored[index] != 0, "early BWT sentinel");
    output[index] = static_cast<uint8_t>(restored[index] - 1);
  }
  return output;
}

inline std::vector<uint8_t> decode_bwt_block_grouped(const uint8_t* packed, size_t packed_size,
                                                      uint32_t raw_size) {
  require(raw_size != 0, "zero-length grouped BWT block");
  Reader reader(packed, packed_size);
  const uint32_t primary = reader.u32();
  const uint32_t transformed_size = raw_size + 1;
  require(primary < transformed_size, "grouped BWT primary index out of range");
  const uint32_t token_count = reader.u32();
  const uint8_t table_count = reader.u8();
  require(table_count != 0 && table_count <= 8 && table_count <= token_count,
          "invalid grouped BWT table count");
  require(token_count <= uint64_t{2} * transformed_size + 1, "grouped BWT token count too large");
  std::vector<HuffmanTable> tables;
  tables.reserve(table_count);
  for (uint8_t table_index = 0; table_index < table_count; ++table_index) {
    std::vector<uint8_t> lengths(kBwtTokenAlphabet);
    for (uint8_t& length : lengths) length = reader.u8();
    tables.push_back(canonical_huffman(lengths));
  }
  const uint8_t* entropy = reader.current();
  const size_t entropy_size = reader.remaining();
  (void)reader.bytes(entropy_size);
  HuffmanReader bits(entropy, entropy_size);
  std::array<uint16_t, kBwtAlphabet> list{};
  std::array<uint16_t, kBwtAlphabet> positions{};
  for (uint16_t index = 0; index < kBwtAlphabet; ++index) list[index] = positions[index] = index;
  std::vector<uint16_t> transformed;
  transformed.reserve(transformed_size);
  uint64_t run = 0;
  unsigned run_bits = 0;
  const auto flush_run = [&] {
    if (run_bits == 0) return;
    require(run != 0 && run <= transformed_size - transformed.size(), "invalid grouped BWT zero run");
    transformed.insert(transformed.end(), static_cast<size_t>(run), list[0]);
    run = 0;
    run_bits = 0;
  };
  bool saw_end = false;
  for (uint8_t table_index = 0; table_index < table_count; ++table_index) {
    HuffmanDecoder decoder(tables[table_index]);
    const uint32_t begin = uint64_t(token_count) * table_index / table_count;
    const uint32_t end = uint64_t(token_count) * (table_index + 1) / table_count;
    for (uint32_t index = begin; index < end; ++index) {
      const uint32_t token = decoder.symbol(bits);
      require(!saw_end, "trailing grouped BWT token");
      if (token == kBwtRunZero || token == kBwtRunOne) {
        require(run_bits < 32, "grouped BWT zero run overflow");
        if (token == kBwtRunOne) run |= uint64_t{1} << run_bits;
        ++run_bits;
      } else {
        flush_run();
        if (token == kBwtEnd) {
          require(index + 1 == end && table_index + 1 == table_count,
                  "early grouped BWT end token");
          saw_end = true;
        } else {
          require(token >= 2 && token <= 257 && transformed.size() < transformed_size,
                  "invalid grouped BWT MTF token");
          const uint16_t position = static_cast<uint16_t>(token - 1);
          const uint16_t symbol = list[position];
          transformed.push_back(symbol);
          bwt_move_to_front(symbol, list, positions);
        }
      }
    }
  }
  require(saw_end, "missing grouped BWT end token");
  bits.finish_zero_padding();
  require(transformed.size() == transformed_size, "grouped BWT transformed length mismatch");
  uint32_t sentinels = 0;
  for (uint16_t symbol : transformed) {
    require(symbol < kBwtAlphabet, "invalid grouped BWT symbol");
    sentinels += symbol == 0;
  }
  require(sentinels == 1, "invalid grouped BWT sentinel count");
  std::array<uint32_t, kBwtAlphabet> frequencies{};
  for (uint16_t symbol : transformed) ++frequencies[symbol];
  std::array<uint32_t, kBwtAlphabet> starts{};
  uint32_t sum = 0;
  for (uint32_t symbol = 0; symbol < kBwtAlphabet; ++symbol) {
    starts[symbol] = sum;
    sum += frequencies[symbol];
  }
  require(sum == transformed_size, "grouped BWT frequency mismatch");
  std::array<uint32_t, kBwtAlphabet> occurrence = starts;
  std::vector<uint32_t> next(transformed_size);
  for (uint32_t index = 0; index < transformed_size; ++index) next[index] = occurrence[transformed[index]]++;
  std::vector<uint16_t> restored(transformed_size);
  uint32_t row = primary;
  for (uint32_t position = transformed_size; position != 0; --position) {
    restored[position - 1] = transformed[row];
    row = next[row];
  }
  require(row == primary && restored.back() == 0, "grouped BWT inverse cycle mismatch");
  std::vector<uint8_t> output(raw_size);
  for (uint32_t index = 0; index < raw_size; ++index) {
    require(restored[index] != 0, "early grouped BWT sentinel");
    output[index] = static_cast<uint8_t>(restored[index] - 1);
  }
  return output;
}

inline std::vector<uint8_t> decode_bwt_object(const std::vector<uint8_t>& archive,
                                               size_t maximum_output) {
  require(archive.size() >= 26, "truncated BWT object checksum");
  const size_t checksum_offset = archive.size() - 4;
  const uint32_t expected_crc = (uint32_t(archive[checksum_offset]) << 24) |
                                (uint32_t(archive[checksum_offset + 1]) << 16) |
                                (uint32_t(archive[checksum_offset + 2]) << 8) |
                                uint32_t(archive[checksum_offset + 3]);
  require(crc32(archive.data(), checksum_offset) == expected_crc, "BWT object checksum mismatch");
  Reader reader(archive.data(), checksum_offset);
  reader.expect("TBW1", 4, "wrong BWT object magic");
  const uint8_t version = reader.u8();
  require((version == 1 || version == 2) && reader.u8() == 1, "unsupported BWT object version");
  const uint64_t original_size64 = reader.u64();
  require(original_size64 <= maximum_output, "BWT object exceeds decode limit");
  const size_t original_size = static_cast<size_t>(original_size64);
  const uint32_t block_size = reader.u32();
  require(block_size >= 1024 && block_size <= 4 * 1024 * 1024, "invalid BWT block size");
  const uint32_t count = reader.u32();
  const uint64_t expected_count = original_size64 / block_size +
                                  (original_size64 % block_size == 0 ? 0 : 1);
  require(expected_count == count, "invalid BWT block count");
  std::vector<uint8_t> output(original_size);
  size_t written = 0;
  for (uint32_t index = 0; index < count; ++index) {
    const uint32_t raw_size = reader.u32();
    const uint32_t packed_size = reader.u32();
    const uint32_t expected_block_crc = reader.u32();
    const uint8_t kind = reader.u8();
    require(raw_size != 0 && raw_size <= block_size && raw_size <= original_size - written,
            "invalid BWT raw block size");
    require(index + 1 == count || raw_size == block_size, "short nonfinal BWT block");
    require(packed_size <= reader.remaining(), "truncated BWT block");
    const uint8_t* packed = reader.bytes(packed_size);
    if (kind == 0) {
      require(packed_size == raw_size, "raw BWT block length mismatch");
      std::memcpy(output.data() + written, packed, raw_size);
    } else if (kind == 1) {
      const std::vector<uint8_t> decoded = version == 1
          ? decode_bwt_block(packed, packed_size, raw_size)
          : decode_bwt_block_grouped(packed, packed_size, raw_size);
      std::memcpy(output.data() + written, decoded.data(), decoded.size());
    } else {
      die("unknown BWT block kind");
    }
    require(crc32(output.data() + written, raw_size) == expected_block_crc, "BWT block checksum mismatch");
    written += raw_size;
  }
  require(written == original_size, "BWT object length mismatch");
  reader.finish();
  return output;
}

}  // namespace terra
