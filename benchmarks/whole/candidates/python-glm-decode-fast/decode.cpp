#include "../../ram_api.hpp"
#define main retained_cli_main
#include "source/decoder.cpp"
#undef main
extern "C" void ram_run(const RamBytes& input, RamResult& result) {
uint64_t size = 0, checksum = 0; size_t body = 0; uint8_t flags = 0;
    ram_require(get_header(input.data(), input.size(), FMT_FAST, size, body, flags, checksum) && flags == 0,
                "Bad pypack header");
    // Preserve the original decoder's input/output padding and integrity check.
    RamBytes padded = input; padded.resize(input.size()+32, 0);
    RamBytes output(size+32);
    ram_require(decode_stream(padded.data()+body, padded.data()+input.size(), output.data(), size) &&
                XXH3_64bits(output.data(), size) == checksum, "pypack decode failed");
    output.resize(size); ram_adopt(std::move(output), result);
}
