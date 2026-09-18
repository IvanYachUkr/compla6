#include "../../ram_api.hpp"
#define main retained_cli_main
#include "source/src/decoder.cpp"
#undef main
extern "C" void ram_run(const RamBytes& input, RamResult& result) {
uint8_t* output = nullptr; uint64_t size = 0;
    decode_payload(input.data(), input.size(), &output, &size);
    result = {output, size, output, [](void* p) { std::free(p); }};
}
