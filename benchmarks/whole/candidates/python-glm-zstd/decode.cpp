#include "../../ram_api.hpp"
#define main retained_cli_main
#include "source/codec.cpp"
#undef main
extern "C" void ram_run(const RamBytes& input, RamResult& result) {
RamBytes output; decode_record(input.data(), input.size(), output); ram_adopt(std::move(output), result);
}
