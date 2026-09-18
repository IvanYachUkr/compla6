#include "../../ram_api.hpp"
#define main retained_cli_main
#include "source/decoder.cpp"
#undef main
extern "C" void ram_run(const RamBytes& input, RamResult& result) {
ram_adopt(decompress_size(input), result);
}
