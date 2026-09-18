#include "codec.h"
#include <lz4.h>
#include "structure/decoder.hpp"
#include <cstring>
#include <new>
#include <cstdint>
template<class T>static T get(const uint8_t*p){T x;memcpy(&x,p,sizeof(x));return x;}
struct State{size_t raw=0,syms=0,tokens=0,mode=0,compressed=0;structure::State* special=nullptr;const uint8_t*lens=nullptr,*dict=nullptr,*stream=nullptr;};
extern "C" void*lab_open(const uint8_t*a,size_t n){if(n>=4&&get<uint32_t>(a)==structure::magic){auto*t=structure::open(a,n);if(!t)return nullptr;auto*s=new(std::nothrow)State;if(!s){structure::close(t);return nullptr;}s->special=t;return s;}if(n<32||get<uint32_t>(a)!=0x31484242)return nullptr;auto*s=new(std::nothrow)State;if(!s)return nullptr;s->mode=get<uint32_t>(a+4);s->raw=get<uint64_t>(a+8);if(s->raw>0x10000000||s->mode>2){delete s;return nullptr;}if(s->mode==1){s->stream=a+32;s->compressed=n-32;return s;}s->syms=get<uint32_t>(a+16);s->tokens=get<uint32_t>(a+20);size_t d=(32+4*s->syms+15)&~size_t(15);if(s->syms>65536||d>n||32>n-d||2*s->tokens>n-d-32){delete s;return nullptr;}s->lens=a+32;s->dict=a+d;s->stream=a+n-2*s->tokens;return s;}
template<unsigned Width> static inline size_t range(State&s,size_t a,size_t b,uint8_t*out,size_t cap){size_t p=0;
 for(;a+4<=b&&p+4*Width<=cap;a+=4){auto t0=get<uint16_t>(s.stream+2*a),t1=get<uint16_t>(s.stream+2*a+2),t2=get<uint16_t>(s.stream+2*a+4),t3=get<uint16_t>(s.stream+2*a+6);auto d0=get<uint32_t>(s.lens+4*t0),d1=get<uint32_t>(s.lens+4*t1),d2=get<uint32_t>(s.lens+4*t2),d3=get<uint32_t>(s.lens+4*t3);memcpy(out+p,s.dict+(d0&0xffffff),Width);p+=d0>>24;memcpy(out+p,s.dict+(d1&0xffffff),Width);p+=d1>>24;memcpy(out+p,s.dict+(d2&0xffffff),Width);p+=d2>>24;memcpy(out+p,s.dict+(d3&0xffffff),Width);p+=d3>>24;}
 for(;a<b;a++){auto t=get<uint16_t>(s.stream+2*a);auto d=get<uint32_t>(s.lens+4*t);size_t l=d>>24;if(l>cap-p)return SIZE_MAX;memcpy(out+p,s.dict+(d&0xffffff),cap-p>=Width?Width:l);p+=l;}return p;}
extern "C" int64_t lab_decode(void*v,uint8_t*out,size_t cap){auto&s=*(State*)v;if(s.special)return structure::decode(s.special,out,cap);if(s.raw>cap)return -1;if(s.mode==1){int z=LZ4_decompress_safe((const char*)s.stream,(char*)out,s.compressed,cap);return z>=0&&(size_t)z==s.raw?z:-1;}size_t p=s.mode==2?range<32>(s,0,s.tokens,out,cap):range<16>(s,0,s.tokens,out,cap);return p==s.raw?p:-1;}
extern "C" int64_t lab_rows(void*,const uint64_t*,size_t,uint8_t*,size_t,uint64_t*){return -1;}
extern "C" void lab_close(void*v){auto*s=(State*)v;if(s){structure::close(s->special);delete s;}}
