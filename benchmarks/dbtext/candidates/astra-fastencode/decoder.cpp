#include "codec.h"
#include "structure/decoder.hpp"
#include <zstd.h>
#include <cstring>
#include <new>
struct State{const uint8_t*data=nullptr;size_t bytes=0,raw=0;structure::State*typed=nullptr;};
extern "C" void*lab_open(const uint8_t*a,size_t n){
 if(n<4)return nullptr;uint32_t magic;memcpy(&magic,a,4);auto*s=new(std::nothrow)State;if(!s)return nullptr;
 if(magic==structure::magic){s->typed=structure::open(a,n);if(!s->typed){delete s;return nullptr;}return s;}
 uint32_t version=0;if(n>=16)memcpy(&version,a+4,4);if(n<16||magic!=0x315a4654||version!=1){delete s;return nullptr;}
 memcpy(&s->raw,a+8,8);if(s->raw>0x10000000){delete s;return nullptr;}s->data=a+16;s->bytes=n-16;return s;
}
extern "C" int64_t lab_decode(void*v,uint8_t*out,size_t cap){
 auto*s=(State*)v;if(s->typed)return structure::decode(s->typed,out,cap);if(s->raw>cap)return -1;
 size_t written=ZSTD_decompress(out,cap,s->data,s->bytes);return ZSTD_isError(written)||written!=s->raw?-1:written;
}
extern "C" int64_t lab_rows(void*,const uint64_t*,size_t,uint8_t*,size_t,uint64_t*){return -1;}
extern "C" void lab_close(void*v){auto*s=(State*)v;if(s){structure::close(s->typed);delete s;}}
