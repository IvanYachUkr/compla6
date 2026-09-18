#pragma once
#include <cstdint>
#include <cstdlib>
#include <stdexcept>
#include <utility>
#include <vector>
using RamBytes = std::vector<uint8_t>;
struct RamResult {
    const uint8_t* data = nullptr;
    size_t size = 0;
    void* owner = nullptr;
    void (*release)(void*) = nullptr;
};
inline void ram_require(bool value, const char* message) {
    if (!value) throw std::runtime_error(message);
}
inline void ram_adopt(RamBytes&& bytes, RamResult& result) {
    auto* owner = new RamBytes(std::move(bytes));
    result = {owner->data(), owner->size(), owner,
              [](void* p) { delete static_cast<RamBytes*>(p); }};
}
using RamFunction = void (*)(const RamBytes&, RamResult&);
