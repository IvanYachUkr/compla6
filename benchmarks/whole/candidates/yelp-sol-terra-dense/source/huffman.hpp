#pragma once

#include "common.hpp"

#include <algorithm>
#include <array>
#include <queue>

namespace terra {

// The typed Yelp streams use ordinary deterministic canonical Huffman codes.
// Codes are represented most-significant-bit first and are intentionally kept
// separate from the LZ format so either side can reject malformed models before
// allocating decoded content.
struct HuffmanCode {
  uint64_t bits = 0;
  uint8_t length = 0;
};

struct HuffmanTable {
  std::vector<HuffmanCode> codes;
  uint8_t maximum_length = 0;
};

inline HuffmanTable canonical_huffman(const std::vector<uint8_t>& lengths) {
  HuffmanTable table;
  table.codes.resize(lengths.size());
  std::array<uint32_t, 57> counts{};
  uint32_t active = 0;
  for (uint8_t length : lengths) {
    require(length <= 56, "Huffman code too long");
    if (length != 0) {
      ++counts[length];
      ++active;
      table.maximum_length = std::max(table.maximum_length, length);
    }
  }
  if (active == 0) return table;
  if (active == 1) {
    require(table.maximum_length == 1, "invalid single-symbol Huffman code");
  }

  std::array<uint64_t, 57> next{};
  uint64_t code = 0;
  for (unsigned length = 1; length <= table.maximum_length; ++length) {
    code = (code + counts[length - 1]) << 1;
    const uint64_t capacity = uint64_t{1} << length;
    require(code <= capacity && counts[length] <= capacity - code,
            "oversubscribed Huffman code");
    next[length] = code;
  }
  if (active > 1) {
    const uint64_t capacity = uint64_t{1} << table.maximum_length;
    require(code + counts[table.maximum_length] == capacity,
            "incomplete Huffman code");
  }
  for (size_t symbol = 0; symbol < lengths.size(); ++symbol) {
    const uint8_t length = lengths[symbol];
    if (length != 0) table.codes[symbol] = {next[length]++, length};
  }
  return table;
}

inline std::vector<uint8_t> huffman_lengths(const std::vector<uint64_t>& weights) {
  struct Node {
    uint64_t weight;
    uint32_t minimum_symbol;
    int left;
    int right;
  };
  struct Earlier {
    const std::vector<Node>* nodes;
    bool operator()(int a, int b) const {
      const Node& left = (*nodes)[a];
      const Node& right = (*nodes)[b];
      if (left.weight != right.weight) return left.weight > right.weight;
      return left.minimum_symbol > right.minimum_symbol;
    }
  };

  std::vector<uint8_t> lengths(weights.size(), 0);
  std::vector<Node> nodes;
  nodes.reserve(weights.size() * 2);
  for (uint32_t symbol = 0; symbol < weights.size(); ++symbol) {
    if (weights[symbol] != 0) nodes.push_back({weights[symbol], symbol, -1, -1});
  }
  if (nodes.empty()) return lengths;
  if (nodes.size() == 1) {
    lengths[nodes[0].minimum_symbol] = 1;
    return lengths;
  }
  Earlier earlier{&nodes};
  std::priority_queue<int, std::vector<int>, Earlier> queue(earlier);
  for (int index = 0; index < static_cast<int>(nodes.size()); ++index) queue.push(index);
  while (queue.size() > 1) {
    const int left = queue.top();
    queue.pop();
    const int right = queue.top();
    queue.pop();
    require(nodes[left].weight <= std::numeric_limits<uint64_t>::max() - nodes[right].weight,
            "Huffman frequency overflow");
    const int parent = static_cast<int>(nodes.size());
    nodes.push_back({nodes[left].weight + nodes[right].weight,
                     std::min(nodes[left].minimum_symbol, nodes[right].minimum_symbol), left, right});
    queue.push(parent);
  }
  const int root = queue.top();
  const auto assign_lengths = [&](auto&& self, int node, uint8_t depth) -> void {
    if (nodes[node].left < 0) {
      require(depth != 0 && depth <= 56, "Huffman tree too deep");
      lengths[nodes[node].minimum_symbol] = depth;
      return;
    }
    require(depth < 56, "Huffman tree too deep");
    self(self, nodes[node].left, static_cast<uint8_t>(depth + 1));
    self(self, nodes[node].right, static_cast<uint8_t>(depth + 1));
  };
  assign_lengths(assign_lengths, root, 0);
  (void)canonical_huffman(lengths);
  return lengths;
}

class HuffmanWriter {
 public:
  void put(const HuffmanCode& code) {
    require(code.length != 0 && code.length <= 56, "invalid Huffman encoder code");
    require(code.bits < (uint64_t{1} << code.length), "noncanonical Huffman encoder code");
    // Every call flushes complete bytes, keeping fewer than eight pending bits.
    pending_ = (pending_ << code.length) | code.bits;
    pending_bits_ += code.length;
    while (pending_bits_ >= 8) {
      bytes_.push_back(static_cast<uint8_t>(pending_ >> (pending_bits_ - 8)));
      pending_bits_ -= 8;
      if (pending_bits_ == 0) {
        pending_ = 0;
      } else {
        pending_ &= (uint64_t{1} << pending_bits_) - 1;
      }
    }
  }

  std::vector<uint8_t> finish() {
    if (pending_bits_ != 0) {
      bytes_.push_back(static_cast<uint8_t>(pending_ << (8 - pending_bits_)));
      pending_bits_ = 0;
      pending_ = 0;
    }
    return std::move(bytes_);
  }

 private:
  std::vector<uint8_t> bytes_;
  uint64_t pending_ = 0;
  unsigned pending_bits_ = 0;
};

class HuffmanReader {
 public:
  HuffmanReader(const uint8_t* bytes, size_t size) : bytes_(bytes), size_(size) {}

  uint8_t bit() {
    require(byte_position_ < size_, "truncated Huffman data");
    const uint8_t value = static_cast<uint8_t>((bytes_[byte_position_] >> (7 - bit_position_)) & 1U);
    if (++bit_position_ == 8) {
      bit_position_ = 0;
      ++byte_position_;
    }
    return value;
  }

  void finish_zero_padding() const {
    if (bit_position_ == 0) {
      require(byte_position_ == size_, "trailing Huffman data");
    } else {
      require(byte_position_ + 1 == size_, "trailing Huffman data");
      const uint8_t mask = static_cast<uint8_t>((uint16_t{1} << (8 - bit_position_)) - 1);
      require((bytes_[byte_position_] & mask) == 0, "nonzero Huffman padding");
    }
  }

 private:
  const uint8_t* bytes_;
  size_t size_;
  size_t byte_position_ = 0;
  uint8_t bit_position_ = 0;
};

class HuffmanDecoder {
 public:
  explicit HuffmanDecoder(const HuffmanTable& table) {
    nodes_.push_back({{-1, -1}, -1});
    for (uint32_t symbol = 0; symbol < table.codes.size(); ++symbol) {
      const HuffmanCode code = table.codes[symbol];
      if (code.length == 0) continue;
      int node = 0;
      for (int bit = code.length - 1; bit >= 0; --bit) {
        require(nodes_[node].symbol < 0, "Huffman prefix collision");
        const unsigned direction = unsigned((code.bits >> bit) & 1U);
        int next = nodes_[node].child[direction];
        if (next < 0) {
          next = static_cast<int>(nodes_.size());
          nodes_[node].child[direction] = next;
          nodes_.push_back({{-1, -1}, -1});
        }
        node = next;
      }
      require(nodes_[node].symbol < 0 && nodes_[node].child[0] < 0 && nodes_[node].child[1] < 0,
              "duplicate Huffman code");
      nodes_[node].symbol = static_cast<int>(symbol);
    }
  }

  uint32_t symbol(HuffmanReader& reader) const {
    int node = 0;
    for (unsigned bits = 0; bits < 56; ++bits) {
      const int next = nodes_[node].child[reader.bit()];
      require(next >= 0, "invalid Huffman code");
      node = next;
      if (nodes_[node].symbol >= 0) return static_cast<uint32_t>(nodes_[node].symbol);
    }
    die("Huffman code exceeds maximum length");
  }

 private:
  struct Node {
    std::array<int, 2> child;
    int symbol;
  };
  std::vector<Node> nodes_;
};

// A stream carries its exact symbol count, a compact local alphabet, canonical
// code lengths, and byte-aligned coded bits. External IDs are ordered so the
// decoder can reject ambiguous dictionary/model metadata before decoding.
inline void append_huffman_stream(std::vector<uint8_t>& out, const std::vector<uint32_t>& values) {
  require(values.size() <= std::numeric_limits<uint32_t>::max(), "too many Huffman values");
  std::vector<uint32_t> alphabet = values;
  std::sort(alphabet.begin(), alphabet.end());
  alphabet.erase(std::unique(alphabet.begin(), alphabet.end()), alphabet.end());
  require(alphabet.size() <= std::numeric_limits<uint32_t>::max(), "Huffman alphabet too large");
  std::vector<uint64_t> weights(alphabet.size(), 0);
  for (uint32_t value : values) {
    const auto found = std::lower_bound(alphabet.begin(), alphabet.end(), value);
    require(found != alphabet.end() && *found == value, "missing Huffman symbol");
    ++weights[static_cast<size_t>(found - alphabet.begin())];
  }
  const std::vector<uint8_t> lengths = huffman_lengths(weights);
  const HuffmanTable table = canonical_huffman(lengths);
  HuffmanWriter writer;
  for (uint32_t value : values) {
    const auto found = std::lower_bound(alphabet.begin(), alphabet.end(), value);
    writer.put(table.codes[static_cast<size_t>(found - alphabet.begin())]);
  }
  std::vector<uint8_t> data = writer.finish();
  require(data.size() <= std::numeric_limits<uint32_t>::max(), "Huffman data too large");
  append_u32(out, static_cast<uint32_t>(values.size()));
  append_u32(out, static_cast<uint32_t>(alphabet.size()));
  for (size_t i = 0; i < alphabet.size(); ++i) {
    append_u32(out, alphabet[i]);
    out.push_back(lengths[i]);
  }
  append_u32(out, static_cast<uint32_t>(data.size()));
  out.insert(out.end(), data.begin(), data.end());
}

inline std::vector<uint32_t> read_huffman_stream(Reader& reader, uint32_t expected_values,
                                                  uint32_t maximum_symbol) {
  require(reader.u32() == expected_values, "Huffman value count mismatch");
  const uint32_t alphabet_count = reader.u32();
  require(alphabet_count <= expected_values || (expected_values == 0 && alphabet_count == 0),
          "invalid Huffman alphabet size");
  std::vector<uint32_t> alphabet;
  std::vector<uint8_t> lengths;
  alphabet.reserve(alphabet_count);
  lengths.reserve(alphabet_count);
  uint32_t previous = 0;
  for (uint32_t i = 0; i < alphabet_count; ++i) {
    const uint32_t value = reader.u32();
    require(value <= maximum_symbol && (i == 0 || previous < value), "invalid Huffman alphabet symbol");
    previous = value;
    alphabet.push_back(value);
    lengths.push_back(reader.u8());
  }
  const uint32_t data_size = reader.u32();
  require(data_size <= reader.remaining(), "truncated Huffman stream");
  const uint8_t* data = reader.bytes(data_size);
  if (expected_values == 0) {
    require(alphabet.empty() && data_size == 0, "nonempty zero-length Huffman stream");
    return {};
  }
  require(!alphabet.empty() && data_size != 0, "empty Huffman stream");
  const HuffmanTable table = canonical_huffman(lengths);
  HuffmanDecoder decoder(table);
  HuffmanReader bits(data, data_size);
  std::vector<uint32_t> values;
  values.reserve(expected_values);
  for (uint32_t i = 0; i < expected_values; ++i) values.push_back(alphabet[decoder.symbol(bits)]);
  bits.finish_zero_padding();
  return values;
}

}  // namespace terra
