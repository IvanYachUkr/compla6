#include "../../ram_api.hpp"
#define main retained_cli_main
#include "source/codec.cpp"
#undef main
extern "C" void ram_run(const RamBytes& input, RamResult& result) {
std::vector<std::pair<const uint8_t*, uint32_t>> jobs;
    for (size_t pos = 0; pos < input.size(); pos += CHUNK_SIZE)
        jobs.push_back({input.data()+pos, uint32_t(std::min(size_t(CHUNK_SIZE), input.size()-pos))});
    std::vector<std::vector<uint8_t>> chunks;
    run_jobs(jobs, chunks, compress_chunk);
    RamBytes output; append_be32(output, uint32_t(chunks.size()));
    for (const auto& chunk : chunks) output.insert(output.end(), chunk.begin(), chunk.end());
    ram_adopt(std::move(output), result);
}
