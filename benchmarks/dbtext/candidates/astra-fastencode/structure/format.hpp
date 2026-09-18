#pragma once
#include <cstdint>
#include <cstring>
#include <cstddef>
namespace structure {
static constexpr uint32_t magic=0x31525453; // STR1
static constexpr size_t HS=64;
enum : uint32_t { DECIMAL=1, ALPHABET=2, HEX=3, UUID=4, COORD=5 };
inline uint32_t get32(const uint8_t*p){uint32_t v;std::memcpy(&v,p,4);return v;}
inline uint64_t get64(const uint8_t*p){uint64_t v;std::memcpy(&v,p,8);return v;}
inline void put32(uint8_t*p,uint32_t v){std::memcpy(p,&v,4);}
inline void put64(uint8_t*p,uint64_t v){std::memcpy(p,&v,8);}
}
