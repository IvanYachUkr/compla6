#include "../../ram_api.hpp"
#define main retained_cli_main
#include "source/src/codec.cpp"
#undef main
extern "C" void ram_run(const RamBytes& input, RamResult& result) {
RamBytes output; build_archive(input.data(), input.size(), output); ram_adopt(std::move(output), result);
}
