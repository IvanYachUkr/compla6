#pragma once

#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <dirent.h>
#include <fcntl.h>
#include <stdexcept>
#include <string>
#include <sys/stat.h>
#include <unistd.h>
#include <utility>
#include <vector>

// HBI1/HBA1 framing is independent of the per-object compression format.
// Each object is processed once, and the transform sees no aliases or peers.
// Transformed records remain in memory until all input has been accepted, so
// malformed input produces neither stdout bytes nor output directory files.
namespace transport {
using Bytes = std::vector<uint8_t>;
struct Record {
  std::string alias;
  Bytes data;
};
// Whole-object coding bounds raw data separately at 128 MiB. Allow its
// framing overhead here without allowing a much larger archive allocation.
constexpr uint64_t max_object_bytes = (uint64_t{128} << 20) + 128;
constexpr uint64_t max_batch_bytes = uint64_t{256} << 20;
constexpr uint32_t max_records = 1000000;
constexpr uint32_t max_alias_bytes = 1048576;

[[noreturn]] inline void fail(const char *message) {
  throw std::runtime_error(message);
}
[[noreturn]] inline void fail_system(const char *operation) {
  const int error = errno;
  throw std::runtime_error(std::string(operation) + ": " +
                           std::strerror(error));
}

class File {
  FILE *file_;

public:
  explicit File(int fd, const char *mode) : file_(fdopen(fd, mode)) {
    if (!file_) {
      const int error = errno;
      close(fd);
      errno = error;
      fail_system("fdopen");
    }
  }
  File(const File &) = delete;
  File &operator=(const File &) = delete;
  ~File() {
    if (file_)
      std::fclose(file_);
  }
  FILE *get() const { return file_; }
  void finish() {
    FILE *file = std::exchange(file_, nullptr);
    if (std::fclose(file) != 0)
      fail_system("close file");
  }
};

inline void read_exact(FILE *file, void *destination, size_t length) {
  if (length && std::fread(destination, 1, length, file) != length) {
    if (std::ferror(file))
      fail_system("read");
    fail("truncated input");
  }
}
inline void write_exact(FILE *file, const void *source, size_t length) {
  if (length && std::fwrite(source, 1, length, file) != length)
    fail_system("write");
}
inline void require_eof(FILE *file) {
  if (std::fgetc(file) != EOF)
    fail("unexpected trailing bytes");
  if (std::ferror(file))
    fail_system("read");
}
inline uint64_t read_be(FILE *file, unsigned width) {
  uint8_t bytes[8];
  read_exact(file, bytes, width);
  uint64_t value = 0;
  for (unsigned i = 0; i < width; ++i)
    value = (value << 8) | bytes[i];
  return value;
}
inline void write_be(FILE *file, uint64_t value, unsigned width) {
  uint8_t bytes[8];
  for (unsigned i = width; i != 0; --i) {
    bytes[i - 1] = static_cast<uint8_t>(value);
    value >>= 8;
  }
  write_exact(file, bytes, width);
}
inline void charge(uint64_t &used, uint64_t amount) {
  if (amount > max_batch_bytes - used)
    fail("batch exceeds 256 MiB limit");
  used += amount;
}

inline uint32_t read_header(FILE *file, const char *magic) {
  uint8_t header[5];
  read_exact(file, header, sizeof(header));
  if (std::memcmp(header, magic, 4) || header[4] != 1)
    fail("invalid transport magic or version");
  const uint32_t count = static_cast<uint32_t>(read_be(file, 4));
  if (!count || count > max_records)
    fail("invalid record count");
  return count;
}
inline void write_header(FILE *file, const char *magic, uint32_t count) {
  write_exact(file, magic, 4);
  const uint8_t version = 1;
  write_exact(file, &version, 1);
  write_be(file, count, 4);
}
inline std::string read_alias(FILE *file, const std::string &previous,
                              uint64_t &bytes) {
  const uint32_t length = static_cast<uint32_t>(read_be(file, 4));
  if (!length || length > max_alias_bytes)
    fail("invalid alias length");
  charge(bytes, uint64_t{12} + length);
  std::string alias(length, '\0');
  read_exact(file, alias.data(), length);
  bool after_dash = true;
  for (const unsigned char c : alias) {
    if ((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')) {
      after_dash = false;
    } else if (c == '-' && !after_dash) {
      after_dash = true;
    } else {
      fail("invalid alias syntax");
    }
  }
  if (after_dash)
    fail("alias ends with a hyphen");
  if (!previous.empty() && !(previous < alias))
    fail("aliases must be unique and strictly increasing");
  return alias;
}
inline void write_record_prefix(FILE *file, const std::string &alias,
                                uint64_t payload_size) {
  write_be(file, alias.size(), 4);
  write_exact(file, alias.data(), alias.size());
  write_be(file, payload_size, 8);
}
inline void check_object_size(uint64_t size) {
  if (size > max_object_bytes)
    fail("transport frame exceeds 128 MiB plus 128 bytes");
}

class Directory {
  int fd_;

public:
  explicit Directory(const char *path, bool output) {
    fd_ = open(path, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    if (fd_ < 0 && output && errno == ENOENT) {
      if (mkdir(path, 0700) != 0)
        fail_system("create output directory");
      fd_ = open(path, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    }
    if (fd_ < 0)
      fail_system("open directory");
  }
  Directory(const Directory &) = delete;
  Directory &operator=(const Directory &) = delete;
  ~Directory() { close(fd_); }
  int fd() const { return fd_; }
};

inline std::string ordinal_name(uint32_t index) {
  char name[] = "00000000.bin";
  for (unsigned i = 8; i != 0; --i) {
    name[i - 1] = static_cast<char>('0' + index % 10);
    index /= 10;
  }
  return name;
}
inline int open_regular_input(int directory, const char *name,
                              uint64_t *size = nullptr) {
  const int fd =
      openat(directory, name, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
  if (fd < 0)
    fail_system("open input file");
  struct stat status{};
  if (fstat(fd, &status) != 0) {
    const int error = errno;
    close(fd);
    errno = error;
    fail_system("stat input file");
  }
  if (!S_ISREG(status.st_mode) || status.st_size < 0) {
    close(fd);
    fail("input file is not a regular file");
  }
  if (size)
    *size = static_cast<uint64_t>(status.st_size);
  return fd;
}
inline int create_output(int directory, const char *name) {
  const int fd =
      openat(directory, name,
             O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
  if (fd < 0)
    fail_system("create output file");
  return fd;
}

// Validate the complete directory before reading payloads or producing output.
// Directory entries are unique, so valid ordinal ranges and the total count
// together prove that all required files are present without an extra bitmap.
inline void check_directory(int directory, uint32_t count, bool output) {
  const int copied_fd = dup(directory);
  if (copied_fd < 0)
    fail_system("duplicate directory descriptor");
  DIR *entries = fdopendir(copied_fd);
  if (!entries) {
    const int error = errno;
    close(copied_fd);
    errno = error;
    fail_system("read directory");
  }
  uint32_t seen = 0;
  try {
    for (;;) {
      errno = 0;
      const dirent *entry = readdir(entries);
      if (!entry) {
        if (errno)
          fail_system("read directory");
        break;
      }
      const char *name = entry->d_name;
      if (!std::strcmp(name, ".") || !std::strcmp(name, ".."))
        continue;
      if (output)
        fail("output directory must be empty");
      struct stat status{};
      if (fstatat(directory, name, &status, AT_SYMLINK_NOFOLLOW) != 0)
        fail_system("stat directory entry");
      if (!S_ISREG(status.st_mode))
        fail("directory contains a nonregular file");
      if (std::strcmp(name, "names.bin")) {
        if (std::strlen(name) != 12 || std::strcmp(name + 8, ".bin"))
          fail("unexpected input directory entry");
        uint32_t index = 0;
        for (unsigned i = 0; i < 8; ++i) {
          if (name[i] < '0' || name[i] > '9')
            fail("unexpected input directory entry");
          index = index * 10 + static_cast<unsigned>(name[i] - '0');
        }
        if (index >= count)
          fail("unexpected object ordinal");
      }
      ++seen;
    }
    if (!output && seen != count + 1)
      fail("missing input directory file");
  } catch (...) {
    closedir(entries);
    throw;
  }
  if (closedir(entries) != 0)
    fail_system("close directory listing");
}

inline Bytes read_payload(int directory, const std::string &name,
                          uint64_t &bytes) {
  uint64_t size;
  File file(open_regular_input(directory, name.c_str(), &size), "rb");
  check_object_size(size);
  charge(bytes, size);
  Bytes data(static_cast<size_t>(size));
  read_exact(file.get(), data.data(), data.size());
  require_eof(file.get());
  return data;
}

template <class Transform> void stream(bool encode, Transform &transform) {
  const uint32_t count = read_header(stdin, encode ? "HBI1" : "HBA1");
  uint64_t input_bytes = 9, output_bytes = 9;
  std::vector<Record> records;
  records.reserve(count);
  const std::string empty;
  for (uint32_t i = 0; i < count; ++i) {
    std::string alias =
        read_alias(stdin, i ? records.back().alias : empty, input_bytes);
    const uint64_t size = read_be(stdin, 8);
    check_object_size(size);
    charge(input_bytes, size);
    Bytes input(static_cast<size_t>(size));
    read_exact(stdin, input.data(), input.size());
    Bytes output = transform(input);
    check_object_size(output.size());
    charge(output_bytes, uint64_t{12} + alias.size() + output.size());
    records.push_back({std::move(alias), std::move(output)});
  }
  require_eof(stdin);
  write_header(stdout, encode ? "HBA1" : "HBI1", count);
  for (const Record &record : records) {
    write_record_prefix(stdout, record.alias, record.data.size());
    write_exact(stdout, record.data.data(), record.data.size());
  }
  if (std::fflush(stdout) != 0)
    fail_system("flush output");
}

template <class Transform>
void directory(bool encode, const char *input_path, const char *output_path,
               Transform &transform) {
  Directory input(input_path, false);
  Directory output(output_path, true);
  check_directory(output.fd(), 0, true);
  File index(open_regular_input(input.fd(), "names.bin"), "rb");
  const uint32_t count = read_header(index.get(), encode ? "HBI1" : "HBA1");
  uint64_t input_bytes = 9;
  std::vector<Record> records;
  records.reserve(count);
  const std::string empty;
  for (uint32_t i = 0; i < count; ++i) {
    records.push_back(
        {read_alias(index.get(), i ? records.back().alias : empty, input_bytes),
         {}});
    if (read_be(index.get(), 8))
      fail("directory index payload must be empty");
  }
  require_eof(index.get());
  check_directory(input.fd(), count, false);
  uint64_t output_bytes = input_bytes;
  for (uint32_t i = 0; i < count; ++i) {
    Bytes raw = read_payload(input.fd(), ordinal_name(i), input_bytes);
    records[i].data = transform(raw);
    check_object_size(records[i].data.size());
    charge(output_bytes, records[i].data.size());
  }
  File output_index(create_output(output.fd(), "names.bin"), "wb");
  write_header(output_index.get(), encode ? "HBA1" : "HBI1", count);
  for (uint32_t i = 0; i < count; ++i) {
    const std::string filename = ordinal_name(i);
    File payload(create_output(output.fd(), filename.c_str()), "wb");
    write_exact(payload.get(), records[i].data.data(), records[i].data.size());
    payload.finish();
    write_record_prefix(output_index.get(), records[i].alias, 0);
  }
  output_index.finish();
}

template <class Transform>
int run_cli(int argc, char **argv, bool encode, Transform transform) {
  try {
    if (argc == 2 &&
        !std::strcmp(argv[1], encode ? "encode-stream" : "decode-stream")) {
      stream(encode, transform);
    } else if (argc == 4 &&
               !std::strcmp(argv[1], encode ? "encode-dir" : "decode-dir")) {
      directory(encode, argv[2], argv[3], transform);
    } else {
      fail(encode ? "usage: codec encode-stream | encode-dir INPUT OUTPUT"
                  : "usage: decoder decode-stream | decode-dir INPUT OUTPUT");
    }
    return 0;
  } catch (const std::exception &error) {
    std::fprintf(stderr, "codec: %s\n", error.what());
    return 1;
  } catch (...) {
    std::fputs("codec: unknown transform failure\n", stderr);
    return 1;
  }
}
} // namespace transport
