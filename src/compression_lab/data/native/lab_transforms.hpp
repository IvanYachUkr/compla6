// SPDX-License-Identifier: MIT
// Length-preserving reversible examples. No statistics or state crosses objects.
#pragma once
#include <cstdint>
#include <stdexcept>
#include <vector>
namespace lab {
using Bytes = std::vector<uint8_t>;
enum Transform : unsigned { Identity = 0, Delta8 = 1, Shuffle4 = 2 };
inline Bytes forward(const Bytes& input, unsigned transform) {
    if (transform == Identity) return input;
    Bytes out(input.size());
    if (transform == Delta8) {
        uint8_t previous = 0;
        for (size_t i = 0; i < input.size(); ++i) {
            out[i] = uint8_t(input[i] - previous);
            previous = input[i];
        }
    } else if (transform == Shuffle4) {
        size_t words = input.size()/4;
        for (size_t lane = 0; lane < 4; ++lane)
            for (size_t i = 0; i < words; ++i) out[lane*words+i] = input[4*i+lane];
        for (size_t i = 4*words; i < input.size(); ++i) out[i] = input[i];
    } else throw std::runtime_error("unknown forward transform");
    return out;
}
inline Bytes inverse(const Bytes& input, unsigned transform) {
    if (transform == Identity) return input;
    Bytes out(input.size());
    if (transform == Delta8) {
        uint8_t previous = 0;
        for (size_t i = 0; i < input.size(); ++i) {
            previous = uint8_t(previous + input[i]);
            out[i] = previous;
        }
    } else if (transform == Shuffle4) {
        size_t words = input.size()/4;
        for (size_t lane = 0; lane < 4; ++lane)
            for (size_t i = 0; i < words; ++i) out[4*i+lane] = input[lane*words+i];
        for (size_t i = 4*words; i < input.size(); ++i) out[i] = input[i];
    } else throw std::runtime_error("unknown inverse transform");
    return out;
}
} // namespace lab
