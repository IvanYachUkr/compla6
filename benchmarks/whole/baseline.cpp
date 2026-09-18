#include "ram_api.hpp"
#include <lz4frame.h>
#include <zstd.h>
#include <cstring>
#include <string>

extern "C" void ram_run(const RamBytes& input, RamResult& result) {
    const std::string method = std::getenv("RAM_METHOD");
    const size_t original = std::stoull(std::getenv("RAM_ORIGINAL_BYTES"));
    RamBytes output;
#ifdef RAM_ENCODE
    if (method == "lz4-1") {
        LZ4F_preferences_t preferences{};
        preferences.frameInfo.blockSizeID = LZ4F_max4MB;
        preferences.frameInfo.blockMode = LZ4F_blockIndependent;
        preferences.frameInfo.contentChecksumFlag = LZ4F_contentChecksumEnabled;
        preferences.compressionLevel = 0;
        output.resize(LZ4F_compressFrameBound(input.size(), &preferences));
        size_t size = LZ4F_compressFrame(output.data(), output.size(), input.data(), input.size(), &preferences);
        ram_require(!LZ4F_isError(size), "LZ4 frame encoding failed");
        output.resize(size);
    } else {
        ZSTD_CCtx* context = ZSTD_createCCtx();
        ram_require(context != nullptr, "Zstd encoder allocation failed");
        ram_require(!ZSTD_isError(ZSTD_CCtx_setParameter(context, ZSTD_c_compressionLevel,
            method == "zstd-3" ? 3 : 19)), "Zstd level setup failed");
        ram_require(!ZSTD_isError(ZSTD_CCtx_setParameter(context, ZSTD_c_checksumFlag, 1)), "Zstd checksum setup failed");
        output.resize(ZSTD_compressBound(input.size()));
        size_t size = ZSTD_compress2(context, output.data(), output.size(), input.data(), input.size());
        ram_require(!ZSTD_isError(size), "Zstd encoding failed");
        ZSTD_freeCCtx(context);
        output.resize(size);
    }
#else
    output.resize(original);
    if (method == "lz4-1") {
        LZ4F_dctx* context = nullptr;
        ram_require(!LZ4F_isError(LZ4F_createDecompressionContext(&context, LZ4F_VERSION)), "LZ4 context setup failed");
        size_t source_pos = 0, target_pos = 0, remaining = 1;
        while (remaining) {
            size_t source_size = input.size()-source_pos, target_size = output.size()-target_pos;
            remaining = LZ4F_decompress(context, output.data()+target_pos, &target_size,
                                        input.data()+source_pos, &source_size, nullptr);
            ram_require(!LZ4F_isError(remaining) && (source_size || target_size || !remaining), "LZ4 decoding failed");
            source_pos += source_size; target_pos += target_size;
        }
        LZ4F_freeDecompressionContext(context);
        ram_require(source_pos == input.size() && target_pos == original, "LZ4 decoded size mismatch");
    } else {
        ZSTD_DCtx* context = ZSTD_createDCtx();
        ram_require(context != nullptr, "Zstd decoder allocation failed");
        size_t size = ZSTD_decompressDCtx(context, output.data(), output.size(), input.data(), input.size());
        ram_require(!ZSTD_isError(size) && size == original, "Zstd decoding failed");
        ZSTD_freeDCtx(context);
    }
#endif
    ram_adopt(std::move(output), result);
}
