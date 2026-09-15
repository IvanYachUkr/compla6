// SPDX-License-Identifier: MIT
// Compression Lab: independent, parallel, conventional native baseline.
// Wire specification and build commands: docs/native_parallel/FORMAT.md.
// No dependency on or changes to baseline.cpp or the Python engine.
#include <algorithm>
#include <array>
#include <cerrno>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <system_error>
#include <vector>
#include <fcntl.h>
#include <unistd.h>
#include "lab_crc.hpp"
#include "parallel_workers.hpp"

#ifndef CL_CODEC
#define CL_CODEC 0
#endif
#ifndef CL_LEVEL
#define CL_LEVEL 1
#endif
#ifndef CL_THREADS
#define CL_THREADS 1
#endif
#ifndef CL_CHUNK_SIZE
#define CL_CHUNK_SIZE 4194304
#endif
#ifndef CL_DECODE_ONLY
#define CL_DECODE_ONLY 0
#endif
#ifndef CL_TRANSFORM
#define CL_TRANSFORM 0
#endif
#ifndef CL_MAX_BYTES
#define CL_MAX_BYTES (uint64_t(8) << 30)
#endif
#ifndef CL_XZ_DECODE_MEMORY
#define CL_XZ_DECODE_MEMORY (uint64_t(128) << 20)
#endif

static_assert(CL_CODEC >= 0 && CL_CODEC <= 6, "CL_CODEC must be 0..6");
static_assert(CL_THREADS >= 1 && CL_THREADS <= 256, "CL_THREADS must be 1..256 (including main)");
static_assert(CL_CHUNK_SIZE >= 1 && CL_CHUNK_SIZE <= 67108864, "CL_CHUNK_SIZE must be 1..64 MiB");
static_assert(CL_TRANSFORM == 0, "parallel baseline supports identity only");
static_assert(CL_DECODE_ONLY == 0 || CL_DECODE_ONLY == 1, "CL_DECODE_ONLY must be 0 or 1");
static_assert(sizeof(std::size_t) >= 8, "64-bit target required");
static_assert(CL_MAX_BYTES >= 1024 && CL_MAX_BYTES <= (uint64_t(8) << 30), "CL_MAX_BYTES must be 1 KiB..8 GiB");
static_assert(CL_XZ_DECODE_MEMORY > 0, "XZ memory bound must be positive");

#if CL_CODEC == 1
#include <zstd.h>
#elif CL_CODEC == 2
#include <lz4.h>
#if !CL_DECODE_ONLY
#include <lz4hc.h>
static_assert(CL_LEVEL == 1 || (CL_LEVEL >= 3 && CL_LEVEL <= 12), "LZ4: level 1 fast; levels 3..12 HC");
#endif
#elif CL_CODEC == 3
#include <brotli/decode.h>
#if !CL_DECODE_ONLY
#include <brotli/encode.h>
static_assert(CL_LEVEL >= 0 && CL_LEVEL <= 11, "Brotli level must be 0..11");
#endif
#elif CL_CODEC == 4
#include <lzma.h>
#if !CL_DECODE_ONLY
static_assert(CL_LEVEL >= 0 && CL_LEVEL <= 9, "XZ preset must be 0..9");
#endif
#elif CL_CODEC == 5
#include <zlib.h>
#if !CL_DECODE_ONLY
static_assert(CL_LEVEL >= -1 && CL_LEVEL <= 9, "zlib level must be -1..9");
#endif
#elif CL_CODEC == 6
#include <bzlib.h>
#if !CL_DECODE_ONLY
static_assert(CL_LEVEL >= 1 && CL_LEVEL <= 9, "bzip2 block level must be 1..9");
#endif
#endif

namespace pb {
namespace fs = std::filesystem;
using Bytes = std::vector<uint8_t>;
constexpr uint64_t kLimit = CL_MAX_BYTES;
constexpr uint32_t kMaxAlias = 1048576, kMaxRecords = 1000000;
constexpr uint32_t kMaxChunk = 67108864, kMaxChunks = 1000000, kMaxDictionary = 67108864;
constexpr std::size_t kHeader = 48, kDescriptor = 20;

void require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}
uint64_t add(uint64_t a, uint64_t b, uint64_t limit = kLimit) {
    require(a <= limit && b <= limit - a, "size limit/overflow");
    return a + b;
}
uint32_t get32(const uint8_t* p) {
    return (uint32_t(p[0]) << 24) | (uint32_t(p[1]) << 16) | (uint32_t(p[2]) << 8) | p[3];
}
uint64_t get64(const uint8_t* p) { return (uint64_t(get32(p)) << 32) | get32(p + 4); }
void set32(uint8_t* p, uint32_t n) {
    for (unsigned i = 0; i < 4; ++i) p[i] = uint8_t(n >> (24 - 8 * i));
}
void set64(uint8_t* p, uint64_t n) { set32(p, uint32_t(n >> 32)); set32(p + 4, uint32_t(n)); }

struct Slice { const uint8_t* data = nullptr; std::size_t size = 0; };
struct Reader {
    Slice input;
    std::size_t position = 0;
    const uint8_t* take(uint64_t size) {
        require(position <= input.size && size <= input.size - position, "truncated framing");
        const auto* result = input.data + position;
        position += std::size_t(size);
        return result;
    }
    uint32_t u32() { return get32(take(4)); }
    uint64_t u64() { return get64(take(8)); }
};
Slice view(const Bytes& b) { return {b.data(), b.size()}; }

bool valid_alias(const std::string& name) {
    if (name.empty() || name.size() > kMaxAlias) return false;
    bool need_letter = true;
    for (unsigned char c : name) {
        if ((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')) need_letter = false;
        else if (c == '-' && !need_letter) need_letter = true;
        else return false;
    }
    return !need_letter;
}
struct InputRecord { std::string name; Slice payload; };
struct OutputRecord { std::string name; Bytes payload; };

std::vector<InputRecord> unpack(Slice bytes, bool archive) {
    require(bytes.size <= kLimit && bytes.size >= 9, "invalid packed length");
    Reader reader{bytes};
    const auto* header = reader.take(5);
    require(std::memcmp(header, archive ? "HBA1" : "HBI1", 4) == 0 && header[4] == 1, "packed magic/version");
    const auto count = reader.u32();
    require(count > 0 && count <= kMaxRecords && uint64_t(count) * 13 <= bytes.size - 9, "invalid record count");
    std::vector<InputRecord> records;
    records.reserve(count);
    for (uint32_t i = 0; i < count; ++i) {
        const auto length = reader.u32();
        require(length > 0 && length <= kMaxAlias, "invalid alias length");
        const auto* name_bytes = reader.take(length);
        std::string name(reinterpret_cast<const char*>(name_bytes), length);
        require(valid_alias(name) && (records.empty() || records.back().name < name), "alias syntax/order");
        const auto length64 = reader.u64();
        const auto* payload = reader.take(length64);
        records.push_back({std::move(name), {payload, std::size_t(length64)}});
    }
    require(reader.position == bytes.size, "trailing packed bytes");
    return records;
}

Bytes read_stdin() {
    Bytes result;
    std::array<char, 65536> block{};
    for (;;) {
        std::cin.read(block.data(), std::streamsize(block.size()));
        const auto count = std::cin.gcount();
        require(count >= 0, "stdin read failure");
        (void)add(result.size(), uint64_t(count));
        result.insert(result.end(), block.data(), block.data() + count);
        if (std::cin.eof()) break;
        require(bool(std::cin), "stdin read failure");
    }
    return result;
}
Bytes read_file(const fs::path& path, uint64_t limit = kLimit) {
    require(fs::is_regular_file(fs::symlink_status(path)), "nonregular/symlink input");
    require(fs::hard_link_count(path) == 1, "hard-linked input");
    const auto size = fs::file_size(path);
    require(size <= limit, "file size limit");
    std::ifstream file(path, std::ios::binary);
    require(bool(file), "cannot open input");
    Bytes result(static_cast<std::size_t>(size));
    if (size) file.read(reinterpret_cast<char*>(result.data()), std::streamsize(size));
    require(bool(file) && file.peek() == std::char_traits<char>::eof() && !file.bad(), "changed/truncated input or read failure");
    return result;
}
std::string ordinal(std::size_t index) {
    auto number = std::to_string(index);
    require(number.size() <= 8, "ordinal overflow");
    return std::string(8 - number.size(), '0') + number + ".bin";
}
struct InputBundle {
    Bytes transport;
    std::vector<Bytes> files;
    std::vector<InputRecord> records;
    InputBundle() = default;
    InputBundle(const InputBundle&) = delete;
    InputBundle& operator=(const InputBundle&) = delete;
    InputBundle(InputBundle&&) = default;
    InputBundle& operator=(InputBundle&&) = default;
};
InputBundle input_bundle(const fs::path* directory, bool archive) {
    InputBundle bundle;
    if (!directory) {
        bundle.transport = read_stdin();
        bundle.records = unpack(view(bundle.transport), archive);
        return bundle;
    }
    require(fs::is_directory(fs::symlink_status(*directory)), "input is not a real directory");
    bundle.transport = read_file(*directory / "names.bin");
    bundle.records = unpack(view(bundle.transport), archive);
    for (const auto& record : bundle.records) require(record.payload.size == 0, "index payload must be empty");
    std::size_t entries = 0;
    for (const auto& entry : fs::directory_iterator(*directory)) {
        require(fs::is_regular_file(entry.symlink_status()) && fs::hard_link_count(entry.path()) == 1, "nonregular/hard-linked directory entry");
        ++entries;
        require(entries <= bundle.records.size() + 1, "extra directory entry");
    }
    require(entries == bundle.records.size() + 1, "directory inventory mismatch");
    uint64_t total = bundle.transport.size();
    // Check every file size and the aggregate BEFORE allocating object buffers.
    for (std::size_t i = 0; i < bundle.records.size(); ++i) {
        const auto path = *directory / ordinal(i);
        require(fs::is_regular_file(fs::symlink_status(path)), "missing/nonregular ordinal");
        total = add(total, fs::file_size(path));
    }
    bundle.files.reserve(bundle.records.size());
    for (std::size_t i = 0; i < bundle.records.size(); ++i) {
        bundle.files.push_back(read_file(*directory / ordinal(i)));
        bundle.records[i].payload = view(bundle.files.back());
    }
    total = bundle.transport.size();
    for (const auto& record : bundle.records) total = add(total, record.payload.size);
    return bundle;
}

std::size_t worker_budget() {
    const char* text = std::getenv("COMPRESSION_LAB_THREADS");
    if (!text) return CL_THREADS;
    require(*text != '\0', "empty worker setting");
    unsigned value = 0;
    for (; *text; ++text) {
        require(*text >= '0' && *text <= '9', "invalid worker setting");
        // Saturation caps arbitrarily long decimal settings without overflow.
        value = std::min(unsigned(CL_THREADS), value * 10 + unsigned(*text - '0'));
    }
    require(value > 0, "worker budget must be positive");
    return value;
}

struct Dictionary {
    uint32_t size = 0, crc = 0;
#if CL_CODEC == 1
#if !CL_DECODE_ONLY
    std::unique_ptr<ZSTD_CDict, decltype(&ZSTD_freeCDict)> cdict{nullptr, ZSTD_freeCDict};
#endif
    std::unique_ptr<ZSTD_DDict, decltype(&ZSTD_freeDDict)> ddict{nullptr, ZSTD_freeDDict};
#endif
    Dictionary(const Bytes& bytes, bool encode) {
        (void)encode;
        require(bytes.size() <= kMaxDictionary, "dictionary size limit");
        size = uint32_t(bytes.size());
        crc = lab::crc32(bytes.data(), bytes.size());
#if CL_CODEC == 1
#if !CL_DECODE_ONLY
        if (encode) {
            require(CL_LEVEL >= ZSTD_minCLevel() && CL_LEVEL <= ZSTD_maxCLevel(), "invalid Zstd level");
            if (size) {
                cdict.reset(ZSTD_createCDict(bytes.data(), bytes.size(), CL_LEVEL));
                require(bool(cdict), "Zstd CDict allocation/initialization failed");
            }
        } else
#endif
        if (size) {
            ddict.reset(ZSTD_createDDict(bytes.data(), bytes.size()));
            require(bool(ddict), "Zstd DDict allocation/initialization failed");
        }
#else
        require(bytes.empty(), "dictionary supported only by Zstd");
#endif
    }
};

struct CodecState {
#if !CL_DECODE_ONLY
    Bytes scratch;
#endif
#if CL_CODEC == 1
#if !CL_DECODE_ONLY
    std::unique_ptr<ZSTD_CCtx, decltype(&ZSTD_freeCCtx)> cctx{nullptr, ZSTD_freeCCtx};
#endif
    std::unique_ptr<ZSTD_DCtx, decltype(&ZSTD_freeDCtx)> dctx{nullptr, ZSTD_freeDCtx};
#endif
    explicit CodecState(bool encode) {
        (void)encode;
#if CL_CODEC == 1
#if !CL_DECODE_ONLY
        if (encode) {
            cctx.reset(ZSTD_createCCtx());
            require(bool(cctx), "Zstd CCtx allocation failed");
            // The pool supplies all concurrency; no backend worker threads.
            require(!ZSTD_isError(ZSTD_CCtx_setParameter(cctx.get(), ZSTD_c_nbWorkers, 0)), "Zstd worker configuration");
        } else
#endif
        {
            dctx.reset(ZSTD_createDCtx());
            require(bool(dctx), "Zstd DCtx allocation failed");
        }
#endif
    }
};

#if !CL_DECODE_ONLY
std::size_t compress_chunk(CodecState& state, const Dictionary& dict, Slice input) {
    (void)state; (void)dict; (void)input;
#if CL_CODEC == 0
    return 0;
#elif CL_CODEC == 1
    state.scratch.resize(ZSTD_compressBound(input.size));
    const auto n = dict.cdict
        ? ZSTD_compress_usingCDict(state.cctx.get(), state.scratch.data(), state.scratch.size(), input.data, input.size, dict.cdict.get())
        : ZSTD_compressCCtx(state.cctx.get(), state.scratch.data(), state.scratch.size(), input.data, input.size, CL_LEVEL);
    require(!ZSTD_isError(n), "Zstd compression failed");
    return n;
#elif CL_CODEC == 2
    const int bound = LZ4_compressBound(int(input.size));
    require(bound > 0, "LZ4 compression bound");
    state.scratch.resize(std::size_t(bound));
#if CL_LEVEL == 1
    const int n = LZ4_compress_default(reinterpret_cast<const char*>(input.data), reinterpret_cast<char*>(state.scratch.data()), int(input.size), bound);
#else
    const int n = LZ4_compress_HC(reinterpret_cast<const char*>(input.data), reinterpret_cast<char*>(state.scratch.data()), int(input.size), bound, CL_LEVEL);
#endif
    require(n > 0, "LZ4 compression failed");
    return std::size_t(n);
#elif CL_CODEC == 3
    auto n = BrotliEncoderMaxCompressedSize(input.size);
    require(n > 0, "Brotli compression bound");
    state.scratch.resize(n);
    require(BrotliEncoderCompress(CL_LEVEL, BROTLI_DEFAULT_WINDOW, BROTLI_MODE_GENERIC, input.size, input.data, &n, state.scratch.data()) == BROTLI_TRUE, "Brotli compression failed");
    return n;
#elif CL_CODEC == 4
    state.scratch.resize(lzma_stream_buffer_bound(input.size));
    std::size_t n = 0;
    require(lzma_easy_buffer_encode(CL_LEVEL, LZMA_CHECK_CRC32, nullptr, input.data, input.size, state.scratch.data(), &n, state.scratch.size()) == LZMA_OK, "XZ compression failed");
    return n;
#elif CL_CODEC == 5
    uLongf n = compressBound(uLong(input.size));
    state.scratch.resize(std::size_t(n));
    require(compress2(state.scratch.data(), &n, input.data, uLong(input.size), CL_LEVEL) == Z_OK, "zlib compression failed");
    return std::size_t(n);
#elif CL_CODEC == 6
    unsigned int n = unsigned(input.size + input.size / 100 + 601);
    state.scratch.resize(n);
    require(BZ2_bzBuffToBuffCompress(reinterpret_cast<char*>(state.scratch.data()), &n, const_cast<char*>(reinterpret_cast<const char*>(input.data)), unsigned(input.size), CL_LEVEL, 0, 30) == BZ_OK, "bzip2 compression failed");
    return n;
#endif
}
#endif  // !CL_DECODE_ONLY

void decompress_chunk(CodecState& state, const Dictionary& dict, Slice input, uint8_t* output, std::size_t raw) {
    (void)state; (void)dict; (void)input; (void)output; (void)raw;
#if CL_CODEC == 0
    throw std::runtime_error("stored backend has no compressed method");
#elif CL_CODEC == 1
    require(ZSTD_getFrameContentSize(input.data, input.size) == raw, "Zstd frame content size");
    const auto frame_size = ZSTD_findFrameCompressedSize(input.data, input.size);
    require(!ZSTD_isError(frame_size) && frame_size == input.size, "Zstd trailing/truncated frame");
    const auto n = dict.ddict
        ? ZSTD_decompress_usingDDict(state.dctx.get(), output, raw, input.data, input.size, dict.ddict.get())
        : ZSTD_decompressDCtx(state.dctx.get(), output, raw, input.data, input.size);
    require(!ZSTD_isError(n) && n == raw, "Zstd decompression failed");
#elif CL_CODEC == 2
    const auto n = LZ4_decompress_safe(reinterpret_cast<const char*>(input.data), reinterpret_cast<char*>(output), int(input.size), int(raw));
    require(n >= 0 && std::size_t(n) == raw, "LZ4 decompression failed");
#elif CL_CODEC == 3
    std::unique_ptr<BrotliDecoderState, decltype(&BrotliDecoderDestroyInstance)> decoder(BrotliDecoderCreateInstance(nullptr, nullptr, nullptr), BrotliDecoderDestroyInstance);
    require(bool(decoder), "Brotli decoder allocation failed");
    std::size_t in_left = input.size, out_left = raw, total = 0;
    const uint8_t* in = input.data;
    uint8_t* out = output;
    const auto status = BrotliDecoderDecompressStream(decoder.get(), &in_left, &in, &out_left, &out, &total);
    require(status == BROTLI_DECODER_RESULT_SUCCESS && in_left == 0 && out_left == 0 && total == raw, "Brotli decompression/trailing data");
#elif CL_CODEC == 4
    uint64_t memory = CL_XZ_DECODE_MEMORY;
    std::size_t in = 0, out = 0;
    const auto status = lzma_stream_buffer_decode(&memory, 0, nullptr, input.data, &in, input.size, output, &out, raw);
    require(status == LZMA_OK && in == input.size && out == raw, "XZ decompression/memory/trailing data");
#elif CL_CODEC == 5
    z_stream stream{};
    require(inflateInit(&stream) == Z_OK, "zlib decoder initialization");
    stream.next_in = const_cast<Bytef*>(input.data);
    stream.avail_in = uInt(input.size);
    stream.next_out = output;
    stream.avail_out = uInt(raw);
    const auto status = inflate(&stream, Z_FINISH);
    const bool ok = status == Z_STREAM_END && stream.avail_in == 0 && stream.avail_out == 0;
    const auto ended = inflateEnd(&stream);
    require(ok && ended == Z_OK, "zlib decompression/trailing data");
#elif CL_CODEC == 6
    bz_stream stream{};
    require(BZ2_bzDecompressInit(&stream, 0, 0) == BZ_OK, "bzip2 decoder initialization");
    stream.next_in = const_cast<char*>(reinterpret_cast<const char*>(input.data));
    stream.avail_in = unsigned(input.size);
    stream.next_out = reinterpret_cast<char*>(output);
    stream.avail_out = unsigned(raw);
    const auto status = BZ2_bzDecompress(&stream);
    const bool ok = status == BZ_STREAM_END && stream.avail_in == 0 && stream.avail_out == 0;
    const auto ended = BZ2_bzDecompressEnd(&stream);
    require(ok && ended == BZ_OK, "bzip2 decompression/trailing data");
#endif
}

struct ObjectInfo {
    uint32_t chunk_size = 0, count = 0;
    std::size_t raw = 0, payload = 0, payload_start = 0;
};
ObjectInfo validate_object(Slice object, const Dictionary& dict) {
    require(object.size >= kHeader && object.size <= kLimit, "object size bound");
    const uint8_t* p = object.data;
    require(std::memcmp(p, "CLP1", 4) == 0 && p[4] == 1 && p[5] == CL_CODEC && p[6] == 0 && p[7] == 0, "object magic/version/codec/flags");
    require(lab::crc32(p, 44) == get32(p + 44), "object header checksum");
    ObjectInfo info;
    info.chunk_size = get32(p + 8);
    info.count = get32(p + 12);
    const uint64_t raw = get64(p + 16), payload = get64(p + 24);
    require(info.chunk_size > 0 && info.chunk_size <= kMaxChunk && info.count <= kMaxChunks, "chunk count/size bound");
    require(raw <= kLimit && payload <= raw, "raw/payload bound");
    require(uint64_t(info.count) == raw / info.chunk_size + (raw % info.chunk_size != 0), "noncanonical chunk count");
    require(get32(p + 32) == dict.size && get32(p + 36) == dict.crc, "dictionary fingerprint mismatch");
    const uint64_t table_bytes = uint64_t(info.count) * kDescriptor;
    const uint64_t start = add(kHeader, table_bytes);
    require(add(start, payload) == object.size, "object table/payload length");
    require(lab::crc32(p + kHeader, std::size_t(table_bytes)) == get32(p + 40), "chunk table checksum");
    uint64_t rsum = 0, psum = 0;
    for (uint32_t i = 0; i < info.count; ++i) {
        const auto* d = p + kHeader + std::size_t(i) * kDescriptor;
        const uint32_t r = get32(d), n = get32(d + 4);
        const uint64_t expected = std::min(uint64_t(info.chunk_size), raw - rsum);
        require(r > 0 && r == expected && n > 0 && n <= r, "chunk length");
        require(d[9] == 0 && d[10] == 0 && d[11] == 0, "chunk reserved flags");
        if (d[8] == 0) require(n == r, "literal length");
        else require(CL_CODEC != 0 && d[8] == CL_CODEC && n < r, "compressed method/length");
        rsum = add(rsum, r);
        psum = add(psum, n);
        require(rsum <= raw && psum <= payload, "chunk cumulative lengths");
    }
    require(rsum == raw && psum == payload, "chunk totals");
    info.raw = std::size_t(raw);
    info.payload = std::size_t(payload);
    info.payload_start = std::size_t(start);
    return info;
}

#if !CL_DECODE_ONLY
uint32_t encode_count(std::size_t raw) {
    const uint64_t n = raw / CL_CHUNK_SIZE + (raw % CL_CHUNK_SIZE != 0);
    require(n <= kMaxChunks, "too many chunks");
    return uint32_t(n);
}
uint64_t encode_bound(std::size_t raw) {
    return add(add(kHeader, uint64_t(encode_count(raw)) * kDescriptor), raw);
}
Bytes encode_object(Slice input, const Dictionary& dict, cl_parallel::Workers& pool, std::vector<std::unique_ptr<CodecState>>& states) {
    const uint32_t count = encode_count(input.size);
    const std::size_t start = kHeader + std::size_t(count) * kDescriptor;
    Bytes result(static_cast<std::size_t>(encode_bound(input.size)));
    // Workers use disjoint fixed-capacity raw-size slots in ONE contiguous
    // buffer. Compression scratch is bounded to one backend bound per worker.
    pool.run(count, [&](std::size_t i, std::size_t worker) {
        const std::size_t offset = i * std::size_t(CL_CHUNK_SIZE);
        const std::size_t raw = std::min(std::size_t(CL_CHUNK_SIZE), input.size - offset);
        Slice source{input.data + offset, raw};
        auto& state = *states[worker];
        const uint32_t raw_crc = lab::crc32(source.data, source.size);
        const std::size_t compressed = compress_chunk(state, dict, source);
        const bool use_codec = CL_CODEC != 0 && compressed > 0 && compressed < raw;
        const uint8_t* bytes = use_codec ? state.scratch.data() : source.data;
        const std::size_t length = use_codec ? compressed : raw;
        auto* d = result.data() + kHeader + i * kDescriptor;
        set32(d, uint32_t(raw));
        set32(d + 4, uint32_t(length));
        d[8] = use_codec ? uint8_t(CL_CODEC) : 0;
        set32(d + 12, raw_crc);
        set32(d + 16, use_codec ? lab::crc32(bytes, length) : raw_crc);
        std::memcpy(result.data() + start + offset, bytes, length);
    });
    // Stable forward compaction cannot overwrite any not-yet-moved source slot.
    std::size_t used = 0;
    for (std::size_t i = 0; i < count; ++i) {
        const auto n = get32(result.data() + kHeader + i * kDescriptor + 4);
        const auto source = i * std::size_t(CL_CHUNK_SIZE);
        if (source != used) std::memmove(result.data() + start + used, result.data() + start + source, n);
        used += n;
    }
    result.resize(start + used);
    auto* h = result.data();
    std::memcpy(h, "CLP1", 4);
    h[4] = 1; h[5] = CL_CODEC;
    set32(h + 8, CL_CHUNK_SIZE); set32(h + 12, count);
    set64(h + 16, input.size); set64(h + 24, used);
    set32(h + 32, dict.size); set32(h + 36, dict.crc);
    set32(h + 40, lab::crc32(h + kHeader, std::size_t(count) * kDescriptor));
    set32(h + 44, lab::crc32(h, 44));
    return result;
}
#endif

Bytes decode_object(Slice input, const ObjectInfo& info, const Dictionary& dict, cl_parallel::Workers& pool, std::vector<std::unique_ptr<CodecState>>& states) {
    // All header, table, sum and aggregate-output bounds have already passed.
    std::vector<std::size_t> offsets(std::size_t(info.count) + 1);
    offsets[0] = info.payload_start;
    for (std::size_t i = 0; i < info.count; ++i)
        offsets[i + 1] = offsets[i] + get32(input.data + kHeader + i * kDescriptor + 4);
    Bytes result(info.raw);
    pool.run(info.count, [&](std::size_t i, std::size_t worker) {
        const auto* d = input.data + kHeader + i * kDescriptor;
        const std::size_t raw = get32(d), length = get32(d + 4);
        const auto* encoded = input.data + offsets[i];
        auto* output = result.data() + i * std::size_t(info.chunk_size);
        require(lab::crc32(encoded, length) == get32(d + 16), "encoded chunk checksum");
        if (d[8] == 0) std::memcpy(output, encoded, raw);
        else decompress_chunk(*states[worker], dict, {encoded, length}, output, raw);
        require(lab::crc32(output, raw) == get32(d + 12), "decoded chunk checksum");
    });
    return result;
}

Bytes index_bytes(const std::vector<OutputRecord>& records, bool archive) {
    uint64_t size = 9;
    for (const auto& record : records) size = add(size, 12 + record.name.size());
    Bytes index(static_cast<std::size_t>(size));
    std::memcpy(index.data(), archive ? "HBA1" : "HBI1", 4);
    index[4] = 1; set32(index.data() + 5, uint32_t(records.size()));
    std::size_t pos = 9;
    for (const auto& record : records) {
        set32(index.data() + pos, uint32_t(record.name.size())); pos += 4;
        std::memcpy(index.data() + pos, record.name.data(), record.name.size()); pos += record.name.size();
        set64(index.data() + pos, 0); pos += 8;
    }
    return index;
}
void check_output_directory(const fs::path& directory) {
    require(fs::is_directory(fs::symlink_status(directory)) && fs::is_empty(directory), "output directory must exist, be real and empty");
}
void write_directory(const fs::path& directory, const std::vector<OutputRecord>& records, bool archive) {
    check_output_directory(directory);
    const auto index = index_bytes(records, archive);
    // All path allocations occur before the first file is created. Exclusive
    // opens never clobber existing paths. Synchronous write failures roll back.
    std::vector<fs::path> paths;
    paths.reserve(records.size() + 1);
    for (std::size_t i = 0; i < records.size(); ++i) paths.push_back(directory / ordinal(i));
    paths.push_back(directory / "names.bin");
    std::size_t created = 0;
    int fd = -1;
    try {
        for (std::size_t i = 0; i < paths.size(); ++i) {
            fd = ::open(paths[i].c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
            if (fd < 0) throw std::system_error(errno, std::generic_category(), "output open");
            ++created;
            Slice bytes = i == records.size() ? view(index) : view(records[i].payload);
            std::size_t pos = 0;
            while (pos < bytes.size) {
                // Bounded syscalls also work when CL_MAX_BYTES exceeds SSIZE_MAX
                // on other POSIX environments (64-bit targets are required here).
                const auto n = ::write(fd, bytes.data + pos, std::min(bytes.size - pos, std::size_t(1) << 30));
                if (n < 0 && errno == EINTR) continue;
                if (n <= 0) throw std::system_error(n < 0 ? errno : EIO, std::generic_category(), "output write");
                pos += std::size_t(n);
            }
            const int close_status = ::close(fd); fd = -1;
            if (close_status != 0) throw std::system_error(errno, std::generic_category(), "output close");
        }
    } catch (...) {
        if (fd >= 0) ::close(fd);
        for (std::size_t i = 0; i < created; ++i) {
            std::error_code error;
            fs::remove(paths[i], error);
            if (error) std::cerr << "codec_error: rollback failed: " << error.message() << '\n';
        }
        throw;
    }
}
void write_stdout(const std::vector<OutputRecord>& records, bool archive) {
    uint64_t total = 9;
    for (const auto& record : records) total = add(add(total, 12 + record.name.size()), record.payload.size());
    std::array<uint8_t, 9> header{};
    std::memcpy(header.data(), archive ? "HBA1" : "HBI1", 4);
    header[4] = 1; set32(header.data() + 5, uint32_t(records.size()));
    std::cout.write(reinterpret_cast<const char*>(header.data()), std::streamsize(header.size()));
    for (const auto& record : records) {
        std::array<uint8_t, 8> number{};
        set32(number.data(), uint32_t(record.name.size()));
        std::cout.write(reinterpret_cast<const char*>(number.data()), 4);
        std::cout.write(record.name.data(), std::streamsize(record.name.size()));
        set64(number.data(), record.payload.size());
        std::cout.write(reinterpret_cast<const char*>(number.data()), 8);
        if (!record.payload.empty()) std::cout.write(reinterpret_cast<const char*>(record.payload.data()), std::streamsize(record.payload.size()));
    }
    std::cout.flush();
    require(bool(std::cout), "stdout write failure");
}

int run(int argc, char** argv) {
    require(argc >= 2, "missing operation");
    const std::string op = argv[1];
    const bool directory = op == "encode-dir" || op == "decode-dir";
    const bool encode = op == "encode-dir" || op == "encode-stream";
    require(directory || op == "encode-stream" || op == "decode-stream", "unknown operation");
#if CL_DECODE_ONLY
    require(!encode, "decoder-only executable rejects encoding");
#endif
    const int expected = directory ? 4 : 2;
    require(argc == expected || argc == expected + 1, "argument count");
    const auto requested_workers = worker_budget();
    std::ios::sync_with_stdio(false);
    std::cin.tie(nullptr);
    fs::path input_dir, output_dir;
    if (directory) {
        input_dir = argv[2]; output_dir = argv[3];
        check_output_directory(output_dir);
    }
    Bytes dictionary_bytes;
    if (argc == expected + 1) {
        require(CL_CODEC == 1, "dictionary argument supported only by Zstd");
        dictionary_bytes = read_file(argv[expected], kMaxDictionary);
    }
    const Dictionary dictionary(dictionary_bytes, encode);
    auto input = input_bundle(directory ? &input_dir : nullptr, !encode);
    std::vector<ObjectInfo> info;
    if (!encode) info.reserve(input.records.size());
    uint64_t output_bound = 9;
    std::size_t max_jobs = 1;
    // Preflight every object, including aggregate output, before output buffers,
    // contexts or threads are allocated. No output is written during processing.
    for (const auto& record : input.records) {
        output_bound = add(output_bound, 12 + record.name.size());
#if !CL_DECODE_ONLY
        if (encode) {
            output_bound = add(output_bound, encode_bound(record.payload.size));
            max_jobs = std::max(max_jobs, std::size_t(encode_count(record.payload.size)));
        } else
#endif
        {
            const auto object = validate_object(record.payload, dictionary);
            output_bound = add(output_bound, object.raw);
            max_jobs = std::max(max_jobs, std::size_t(object.count));
            info.push_back(object);
        }
    }
    const auto workers = std::min(requested_workers, max_jobs);
    std::vector<std::unique_ptr<CodecState>> states;
    states.reserve(workers);
    for (std::size_t i = 0; i < workers; ++i) states.push_back(std::make_unique<CodecState>(encode));
    cl_parallel::Workers pool(workers);
    std::vector<OutputRecord> output;
    output.reserve(input.records.size());
    for (std::size_t i = 0; i < input.records.size(); ++i) {
        const auto& record = input.records[i];
        Bytes bytes;
#if !CL_DECODE_ONLY
        if (encode) bytes = encode_object(record.payload, dictionary, pool, states);
        else
#endif
        bytes = decode_object(record.payload, info[i], dictionary, pool, states);
        output.push_back({record.name, std::move(bytes)});
    }
    if (directory) write_directory(output_dir, output, encode);
    else write_stdout(output, encode);
    return 0;
}
}  // namespace pb

int main(int argc, char** argv) {
    try { return pb::run(argc, argv); }
    catch (const std::exception& error) {
        std::cerr << "codec_error: " << error.what() << '\n';
        return 2;
    } catch (...) {
        std::cerr << "codec_error: unknown failure\n";
        return 2;
    }
}
