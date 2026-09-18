#include "../../ram_api.hpp"
#define main retained_cli_main
#include "source/decoder.cpp"
#undef main
extern "C" void ram_run(const RamBytes& input, RamResult& result) {
ram_require(input.size() >= 4, "Short PZ1 body");
    uint32_t count = rd32(input.data());
    const uint8_t* p = input.data()+4; const uint8_t* end = input.data()+input.size();
    uint64_t total = 0;
    for (uint32_t i = 0; i < count; ++i) {
        ram_require(end-p >= 5, "Short PZ1 chunk");
        uint32_t size = rd32(p+1); ram_require(size <= size_t(end-p-5), "Truncated PZ1 chunk");
        if (p[0] == 0) { ram_require(size >= 8, "Short stored PZ1 chunk"); total += size-8; }
        else { ram_require(p[0] == 1 && size >= 16+MAIN_NIB+A_NIB, "Bad PZ1 chunk"); total += rd32(p+5); }
        p += 5+size;
    }
    ram_require(p == end, "Trailing PZ1 bytes");
    RamBytes output(total);
    std::vector<WalkChunk> jobs; p = input.data()+4;
    for (uint32_t i = 0; i < count; ++i) {
        uint32_t size = rd32(p+1), expected = p[0] == 0 ? size-8 : rd32(p+5);
        ram_require(uint64_t(i)*CHUNK_SIZE+expected <= total, "PZ1 output bounds");
        jobs.push_back({p, p+5+size, output.data()+uint64_t(i)*CHUNK_SIZE, expected}); p += 5+size;
    }
    std::vector<uint64_t> got; run_decode_jobs(jobs, got);
    uint64_t sum = 0;
    for (size_t i = 0; i < got.size(); ++i) { ram_require(got[i] == jobs[i].expect, "PZ1 decode failed"); sum += got[i]; }
    ram_require(sum == total, "PZ1 decoded size mismatch");
    ram_adopt(std::move(output), result);
}
