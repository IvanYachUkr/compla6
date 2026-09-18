#pragma once

#include <array>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace terra {

[[noreturn]] inline void die(const std::string& message) {
  throw std::runtime_error(message);
}

inline void require(bool condition, const char* message) {
  if (!condition) die(message);
}

inline void append_u32(std::vector<uint8_t>& out, uint32_t value) {
  out.push_back(static_cast<uint8_t>(value >> 24));
  out.push_back(static_cast<uint8_t>(value >> 16));
  out.push_back(static_cast<uint8_t>(value >> 8));
  out.push_back(static_cast<uint8_t>(value));
}

inline void append_u64(std::vector<uint8_t>& out, uint64_t value) {
  for (int shift = 56; shift >= 0; shift -= 8) {
    out.push_back(static_cast<uint8_t>(value >> shift));
  }
}

class Reader {
 public:
  explicit Reader(const std::vector<uint8_t>& bytes) : data_(bytes.data()), size_(bytes.size()) {}
  Reader(const uint8_t* data, size_t size) : data_(data), size_(size) {}

  size_t remaining() const { return size_ - pos_; }
  size_t position() const { return pos_; }
  const uint8_t* current() const { return data_ + pos_; }

  uint8_t u8() {
    require(remaining() >= 1, "truncated byte");
    return data_[pos_++];
  }

  uint32_t u32() {
    require(remaining() >= 4, "truncated u32");
    uint32_t v = (uint32_t(data_[pos_]) << 24) | (uint32_t(data_[pos_ + 1]) << 16) |
                 (uint32_t(data_[pos_ + 2]) << 8) | uint32_t(data_[pos_ + 3]);
    pos_ += 4;
    return v;
  }

  uint64_t u64() {
    require(remaining() >= 8, "truncated u64");
    uint64_t v = 0;
    for (unsigned i = 0; i != 8; ++i) v = (v << 8) | data_[pos_ + i];
    pos_ += 8;
    return v;
  }

  void expect(const char* bytes, size_t count, const char* message) {
    require(remaining() >= count && std::memcmp(data_ + pos_, bytes, count) == 0, message);
    pos_ += count;
  }

  const uint8_t* bytes(size_t count) {
    require(count <= remaining(), "truncated byte range");
    const uint8_t* result = data_ + pos_;
    pos_ += count;
    return result;
  }

  void finish() const { require(pos_ == size_, "trailing bytes"); }

 private:
  const uint8_t* data_;
  size_t size_;
  size_t pos_ = 0;
};

inline bool valid_alias(const std::string& alias) {
  if (alias.empty() || alias.size() > 1048576) return false;
  bool need_alnum = true;
  for (unsigned char c : alias) {
    const bool alnum = (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9');
    if (need_alnum) {
      if (!alnum) return false;
      need_alnum = false;
    } else if (c == '-') {
      need_alnum = true;
    } else if (!alnum) {
      return false;
    }
  }
  return !need_alnum;
}

struct Record {
  std::string alias;
  std::vector<uint8_t> payload;
};

inline std::vector<uint8_t> read_all(std::istream& input) {
  std::vector<uint8_t> result;
  constexpr size_t kChunk = 1U << 20;
  std::array<char, kChunk> buffer{};
  while (input) {
    input.read(buffer.data(), buffer.size());
    const std::streamsize got = input.gcount();
    require(got >= 0, "input read failure");
    if (got == 0) break;
    const size_t add = static_cast<size_t>(got);
    require(add <= std::numeric_limits<size_t>::max() - result.size(), "input too large");
    result.insert(result.end(), reinterpret_cast<const uint8_t*>(buffer.data()),
                  reinterpret_cast<const uint8_t*>(buffer.data()) + add);
  }
  require(input.eof(), "input read failure");
  return result;
}

inline std::vector<uint8_t> read_file(const std::filesystem::path& path) {
  std::ifstream input(path, std::ios::binary);
  require(static_cast<bool>(input), "cannot open input file");
  auto result = read_all(input);
  return result;
}

inline void write_file(const std::filesystem::path& path, const std::vector<uint8_t>& bytes) {
  std::ofstream output(path, std::ios::binary | std::ios::trunc);
  require(static_cast<bool>(output), "cannot create output file");
  if (!bytes.empty()) output.write(reinterpret_cast<const char*>(bytes.data()), bytes.size());
  require(static_cast<bool>(output), "output write failure");
}

inline uint32_t crc32(const uint8_t* data, size_t size) {
  static const std::array<uint32_t, 256> table = [] {
    std::array<uint32_t, 256> values{};
    for (uint32_t i = 0; i != values.size(); ++i) {
      uint32_t value = i;
      for (unsigned bit = 0; bit != 8; ++bit) {
        value = (value >> 1) ^ (0xedb88320U & static_cast<uint32_t>(-(value & 1U)));
      }
      values[i] = value;
    }
    return values;
  }();
  uint32_t crc = 0xffffffffU;
  for (size_t i = 0; i < size; ++i) {
    crc = table[(crc ^ data[i]) & 0xffU] ^ (crc >> 8);
  }
  return ~crc;
}

inline uint32_t crc32(const std::vector<uint8_t>& bytes) {
  return crc32(bytes.data(), bytes.size());
}

inline std::vector<Record> parse_batch(const std::vector<uint8_t>& bytes, const char magic[4]) {
  Reader reader(bytes);
  reader.expect(magic, 4, "wrong batch magic");
  require(reader.u8() == 1, "unsupported batch version");
  const uint32_t count = reader.u32();
  require(count != 0 && count <= 1000000, "invalid record count");
  std::vector<Record> records;
  records.reserve(count);
  std::string previous;
  for (uint32_t i = 0; i < count; ++i) {
    const uint32_t alias_size = reader.u32();
    require(alias_size != 0 && alias_size <= 1048576, "invalid alias length");
    const uint8_t* alias_bytes = reader.bytes(alias_size);
    std::string alias(reinterpret_cast<const char*>(alias_bytes), alias_size);
    require(valid_alias(alias), "invalid alias");
    require(previous.empty() || previous < alias, "aliases not strictly increasing");
    previous = alias;
    const uint64_t payload_size64 = reader.u64();
    require(payload_size64 <= reader.remaining(), "invalid payload length");
    require(payload_size64 <= std::numeric_limits<size_t>::max(), "payload too large");
    const size_t payload_size = static_cast<size_t>(payload_size64);
    const uint8_t* payload = reader.bytes(payload_size);
    records.push_back({std::move(alias), std::vector<uint8_t>(payload, payload + payload_size)});
  }
  reader.finish();
  return records;
}

inline std::vector<uint8_t> make_batch(const std::vector<Record>& records, const char magic[4]) {
  require(!records.empty() && records.size() <= 1000000, "invalid output record count");
  std::vector<uint8_t> out;
  out.reserve(9);
  out.insert(out.end(), magic, magic + 4);
  out.push_back(1);
  append_u32(out, static_cast<uint32_t>(records.size()));
  std::string previous;
  for (const Record& record : records) {
    require(valid_alias(record.alias), "invalid output alias");
    require(previous.empty() || previous < record.alias, "output aliases not ordered");
    previous = record.alias;
    append_u32(out, static_cast<uint32_t>(record.alias.size()));
    out.insert(out.end(), record.alias.begin(), record.alias.end());
    append_u64(out, record.payload.size());
    out.insert(out.end(), record.payload.begin(), record.payload.end());
  }
  return out;
}

inline std::string ordinal_name(size_t index) {
  require(index < 100000000, "too many ordinal files");
  char name[13] = {'0','0','0','0','0','0','0','0','.','b','i','n','\0'};
  for (int pos = 7; pos >= 0; --pos) {
    name[pos] = static_cast<char>('0' + (index % 10));
    index /= 10;
  }
  return name;
}

inline std::vector<Record> read_directory(const std::filesystem::path& dir, const char magic[4]) {
  namespace fs = std::filesystem;
  require(fs::is_directory(dir), "input is not a directory");
  const fs::path names = dir / "names.bin";
  require(fs::is_regular_file(names), "missing names.bin");
  auto name_records = parse_batch(read_file(names), magic);
  std::vector<Record> records;
  records.reserve(name_records.size());
  for (size_t i = 0; i < name_records.size(); ++i) {
    require(name_records[i].payload.empty(), "names.bin payload must be empty");
    const fs::path file = dir / ordinal_name(i);
    require(fs::is_regular_file(file), "missing ordinal payload file");
    records.push_back({std::move(name_records[i].alias), read_file(file)});
  }
  size_t entries = 0;
  for (const auto& entry : fs::directory_iterator(dir)) {
    require(!entry.is_symlink() && entry.is_regular_file(), "unexpected directory entry");
    ++entries;
  }
  require(entries == records.size() + 1, "unexpected directory file");
  return records;
}

inline void write_directory(const std::filesystem::path& dir, const std::vector<Record>& records,
                            const char magic[4]) {
  namespace fs = std::filesystem;
  require(fs::is_directory(dir), "output is not a directory");
  require(fs::is_empty(dir), "output directory is not empty");
  std::vector<Record> names;
  names.reserve(records.size());
  for (const Record& record : records) names.push_back({record.alias, {}});
  write_file(dir / "names.bin", make_batch(names, magic));
  for (size_t i = 0; i < records.size(); ++i) write_file(dir / ordinal_name(i), records[i].payload);
}

}  // namespace terra
