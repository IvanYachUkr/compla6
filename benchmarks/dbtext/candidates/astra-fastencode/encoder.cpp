#include "codec.h"
#include "structure/encoder.hpp"
#include <zstd.h>
#include <cstring>
#include <vector>
extern "C" int64_t lab_encode(const uint8_t*raw,size_t n,uint8_t*out,size_t cap)try{
 if(n>0x10000000||cap<16)return -1;
 std::vector<uint8_t> typed;
 if(structure::encode(raw,n,typed)){if(typed.size()>cap)return -1;memcpy(out,typed.data(),typed.size());return typed.size();}
 uint32_t magic=0x315a4654,version=1;memcpy(out,&magic,4);memcpy(out+4,&version,4);memcpy(out+8,&n,8);
 size_t written=ZSTD_compress(out+16,cap-16,raw,n,1);return ZSTD_isError(written)?-1:written+16;
}catch(...){return -1;}
