#include "../../ram_api.hpp"
#define main retained_cli_main
#include "source/codec.cpp"
#undef main
extern "C" void ram_run(const RamBytes& input, RamResult& result) {
ram_adopt(encode_object(input), result);
}
