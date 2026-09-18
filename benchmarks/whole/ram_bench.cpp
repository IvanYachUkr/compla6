#include "ram_api.hpp"
#include <chrono>
#include <cstring>
#include <dlfcn.h>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <string>

static RamBytes load(const char* name) {
    std::ifstream file(name, std::ios::binary | std::ios::ate);
    ram_require(bool(file), "Cannot open benchmark input");
    auto size = file.tellg();
    ram_require(size >= 0, "Cannot size benchmark input");
    RamBytes data(static_cast<size_t>(size));
    file.seekg(0);
    file.read(reinterpret_cast<char*>(data.data()), data.size());
    ram_require(bool(file), "Cannot read benchmark input");
    return data;
}
static RamFunction plugin(const char* path) {
    void* handle = dlopen(path, RTLD_NOW | RTLD_LOCAL);
    if (!handle) throw std::runtime_error(dlerror());
    auto function = reinterpret_cast<RamFunction>(dlsym(handle, "ram_run"));
    ram_require(function != nullptr, "Missing RAM adapter");
    return function;
}
static uint64_t big_endian(const uint8_t* p, size_t n) {
    uint64_t value = 0;
    while (n--) value = (value << 8) | *p++;
    return value;
}
int main(int argc, char** argv) try {
    ram_require(argc == 8, "Usage: ram_bench RAW ENCODER DECODER REFERENCE ARCHIVE TRIALS ENVELOPE_BYTES");
    const RamBytes raw = load(argv[1]);
    RamFunction encode = plugin(argv[2]), decode = plugin(argv[3]);
    RamBytes reference;
    const bool compare_reference = std::string(argv[4]) != "-";
    if (compare_reference) {
        auto framed = load(argv[4]);
        ram_require(framed.size() >= 34 && !memcmp(framed.data(), "HBA1\1", 5), "Bad reference frame");
        ram_require(big_endian(framed.data()+5, 4) == 1, "Expected one reference object");
        size_t alias = big_endian(framed.data()+9, 4), offset = 13+alias+8;
        ram_require(offset == 34 && offset <= framed.size(), "Unexpected reference envelope");
        ram_require(big_endian(framed.data()+13+alias, 8) == framed.size()-offset, "Bad reference size");
        reference.assign(framed.begin()+offset, framed.end());
    }
    const int trials = std::stoi(argv[6]);
    const size_t envelope = std::stoull(argv[7]);
    std::cout << std::setprecision(17);
    for (int trial = 1; trial <= trials; ++trial) {
        RamResult compressed, reconstructed;
        auto start = std::chrono::steady_clock::now();
        encode(raw, compressed);
        auto encoded = std::chrono::steady_clock::now();
        // The result becomes a resident decoder input outside both timers.
        RamBytes archive(compressed.data, compressed.data+compressed.size);
        if (compare_reference) ram_require(archive == reference, "Archive differs from frozen CLI implementation");
        compressed.release(compressed.owner);
        auto decode_start = std::chrono::steady_clock::now();
        decode(archive, reconstructed);
        auto decoded = std::chrono::steady_clock::now();
        ram_require(reconstructed.size == raw.size() &&
                    !std::memcmp(raw.data(), reconstructed.data, raw.size()), "Reconstruction mismatch");
        reconstructed.release(reconstructed.owner);
        if (trial == 1) {
            std::ofstream output(argv[5], std::ios::binary);
            output.write(reinterpret_cast<const char*>(archive.data()), archive.size());
            ram_require(bool(output), "Cannot save archive evidence");
            if (!compare_reference) reference = archive;
        } else ram_require(archive == reference, "Nondeterministic archive");
        std::cout << "{\"trial\":" << trial << ",\"raw_bytes\":" << raw.size()
                  << ",\"payload_bytes\":" << archive.size()
                  << ",\"archive_bytes\":" << archive.size()+envelope
                  << ",\"encode_seconds\":" << std::chrono::duration<double>(encoded-start).count()
                  << ",\"decode_seconds\":" << std::chrono::duration<double>(decoded-decode_start).count()
                  << ",\"exact\":true,\"frozen_payload_match\":" << (compare_reference ? "true" : "null")
                  << "}\n" << std::flush;
    }
    return 0;
} catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
}
