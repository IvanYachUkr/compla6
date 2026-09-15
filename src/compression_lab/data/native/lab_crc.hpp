// SPDX-License-Identifier: MIT
// Reflected IEEE CRC-32, polynomial 0xedb88320. Not CRC-32C/SSE4.2.
#pragma once
#include <array>
#include <cstddef>
#include <cstdint>
namespace lab {
inline uint32_t crc_update(uint32_t state, const uint8_t* bytes, size_t length) {
    static const auto table = [] {
        std::array<std::array<uint32_t, 256>, 8> t{};
        for (uint32_t i = 0; i < 256; ++i) {
            uint32_t c = i;
            for (unsigned bit = 0; bit < 8; ++bit)
                c = (c >> 1) ^ (0xedb88320U & uint32_t(-int(c & 1U)));
            t[0][i] = c;
        }
        for (unsigned lane = 1; lane < 8; ++lane)
            for (unsigned i = 0; i < 256; ++i) {
                uint32_t c = t[lane-1][i];
                t[lane][i] = (c >> 8) ^ t[0][c & 255];
            }
        return t;
    }();
    // Explicit loads: no alignment, host-endianness or strict-aliasing assumption.
    while (length >= 8) {
        uint32_t c = state ^ uint32_t(bytes[0]) ^ (uint32_t(bytes[1]) << 8)
                     ^ (uint32_t(bytes[2]) << 16) ^ (uint32_t(bytes[3]) << 24);
        state = table[7][c & 255] ^ table[6][(c >> 8) & 255]
              ^ table[5][(c >> 16) & 255] ^ table[4][c >> 24]
              ^ table[3][bytes[4]] ^ table[2][bytes[5]]
              ^ table[1][bytes[6]] ^ table[0][bytes[7]];
        bytes += 8;
        length -= 8;
    }
    while (length--) state = (state >> 8) ^ table[0][(state ^ *bytes++) & 255];
    return state;
}
inline uint32_t crc32(const uint8_t* bytes, size_t length) {
    return ~crc_update(0xffffffffU, bytes, length);
}
} // namespace lab
