#pragma once
#include "format.hpp"
#include <new>
#include <limits>
#include <immintrin.h>
namespace structure {
struct State{const uint8_t* archive=nullptr;const uint8_t* data=nullptr;const uint8_t* extra=nullptr;size_t archive_size=0;uint64_t raw=0,rows=0;uint32_t mode=0,a=0,b=0,c=0,d=0;uint32_t alphabet4[256];uint16_t pairs[256];uint16_t decimal[100];__m128i uuid_fixed[3];};
inline State* open(const uint8_t*archive,size_t size){
 if(!archive||size<HS||get32(archive)!=magic)return nullptr;auto*s=new(std::nothrow)State;if(!s)return nullptr;s->archive=archive;s->archive_size=size;s->mode=get32(archive+4);s->raw=get64(archive+8);s->rows=get64(archive+16);s->a=get32(archive+24);s->b=get32(archive+28);s->c=get32(archive+32);s->d=get32(archive+36);s->extra=archive+HS;s->data=s->extra;
 if(s->rows>size*8ULL||s->raw>INT64_MAX){delete s;return nullptr;}uint64_t need=HS;
 switch(s->mode){
 case DECIMAL:if(s->a>80||s->b<1||s->b>9||s->c<1||s->c>4||s->raw!=s->rows*(s->a+s->b+1ULL)){delete s;return nullptr;}need+=s->a+s->rows*s->c+8;s->data+=s->a;break;
 case ALPHABET:if(s->a<1||s->a>12||s->b<1||s->b>2||s->c<2||s->c>4||s->raw!=s->rows*(s->a+1ULL)){delete s;return nullptr;}need+=4+(s->rows*s->a*s->b+7)/8+8;s->data+=4;break;
 case HEX:if(s->a>1||s->raw<s->rows*2||s->raw>s->rows*9){delete s;return nullptr;}need+=s->rows*4+8;break;
 case UUID:if(s->raw!=s->rows*37){delete s;return nullptr;}need+=37+s->rows*11+16;s->data+=37;break;
 case COORD:if(s->a>99||s->b>98||s->raw<s->rows*5||s->raw>s->rows*41){delete s;return nullptr;}need+=s->rows*16+16;break;
 default:delete s;return nullptr;
 }if(need!=size){delete s;return nullptr;}
 if(s->mode==ALPHABET){unsigned mask=(1U<<s->b)-1;for(unsigned i=0;i<256;++i){uint32_t v=0;for(unsigned j=0;j<4;++j)v|=uint32_t(s->extra[(i>>(j*s->b))&mask])<<(8*j);s->alphabet4[i]=v;}}
 if(s->mode==HEX||s->mode==UUID){for(unsigned i=0;i<256;++i){auto h=[&](unsigned j)->uint8_t{return j<10?'0'+j:((s->mode==HEX&&s->a)?'A':'a')+j-10;};s->pairs[i]=h(i>>4)|(uint16_t(h(i&15))<<8);}}
 if(s->mode==DECIMAL||s->mode==COORD)for(unsigned i=0;i<100;++i)s->decimal[i]=uint16_t('0'+i/10)|(uint16_t('0'+i%10)<<8);
 if(s->mode==UUID){alignas(16)uint8_t fixed[37];std::memcpy(fixed,s->extra,37);const unsigned pos[]={2,3,4,5,6,7,19,20,21,22,24,25,26,27,28,29,30,31,32,33,34,35};for(unsigned p:pos)fixed[p]=0;s->uuid_fixed[0]=_mm_loadu_si128(reinterpret_cast<const __m128i*>(fixed));s->uuid_fixed[1]=_mm_loadu_si128(reinterpret_cast<const __m128i*>(fixed+16));s->uuid_fixed[2]=_mm_loadu_si128(reinterpret_cast<const __m128i*>(fixed+21));}

 return s;
}
inline void close(State*s){delete s;}
inline uint64_t hex8(const State*s,uint32_t v){return uint64_t(s->pairs[v>>24])|(uint64_t(s->pairs[(v>>16)&255])<<16)|(uint64_t(s->pairs[(v>>8)&255])<<32)|(uint64_t(s->pairs[v&255])<<48);}
inline void unpack_hex16(const uint8_t*p,uint8_t*out,bool letters){
 const __m128i v=_mm_loadu_si128(reinterpret_cast<const __m128i*>(p));const __m128i mask=_mm_set1_epi8(15);__m128i a=_mm_and_si128(v,mask),b=_mm_and_si128(_mm_srli_epi16(v,4),mask);__m128i lo=_mm_unpacklo_epi8(a,b),hi=_mm_unpackhi_epi8(a,b);const __m128i zero=_mm_set1_epi8('0');if(letters){const __m128i nine=_mm_set1_epi8(9),adj=_mm_set1_epi8(39);lo=_mm_add_epi8(_mm_add_epi8(lo,zero),_mm_and_si128(_mm_cmpgt_epi8(lo,nine),adj));hi=_mm_add_epi8(_mm_add_epi8(hi,zero),_mm_and_si128(_mm_cmpgt_epi8(hi,nine),adj));}else{lo=_mm_add_epi8(lo,zero);hi=_mm_add_epi8(hi,zero);}_mm_storeu_si128(reinterpret_cast<__m128i*>(out),lo);_mm_storeu_si128(reinterpret_cast<__m128i*>(out+16),hi);
}
template<unsigned Mode> inline size_t row_size_mode(const State*s,uint64_t id){switch(Mode){case DECIMAL:return s->a+s->b+1;case ALPHABET:return s->a+1;case HEX:{uint32_t v=get32(s->data+id*4);return (32-__builtin_clz(v|1U)+3)/4+1;}case UUID:return 37;case COORD:{uint8_t v=s->data[id*16];return v?12+(v&15)+(v>>4):5;}default:return 0;}}
template<unsigned Mode> inline size_t one_mode(const State*s,uint64_t id,uint8_t*out){
 switch(Mode){
 case DECIMAL:{uint32_t value=get32(s->data+id*s->c);if(s->c<4)value&=(1U<<(8*s->c))-1;if(s->a==12)std::memcpy(out,s->extra,12);else std::memcpy(out,s->extra,s->a);uint8_t*p=out+s->a;unsigned d=s->b;if(d==6){unsigned x=value/10000,y=(value/100)%100,z=value%100;std::memcpy(p,s->decimal+x,2);std::memcpy(p+2,s->decimal+y,2);std::memcpy(p+4,s->decimal+z,2);}else{while(d>=2){unsigned q=value/100;std::memcpy(p+d-2,s->decimal+value-q*100,2);value=q;d-=2;}if(d)p[0]='0'+value;}p[s->b]='\n';return s->a+s->b+1;}
 case ALPHABET:{if(s->a==9&&s->b==2){uint64_t off=id*18;uint32_t value=get32(s->data+off/8)>>(off%8);put32(out,s->alphabet4[value&255]);put32(out+4,s->alphabet4[(value>>8)&255]);out[8]=s->extra[(value>>16)&3];out[9]='\n';return 10;}unsigned total=s->a*s->b;uint64_t off=id*total;uint32_t value=get64(s->data+off/8)>>(off%8);unsigned p=0;unsigned mask=(1U<<(4*s->b))-1;for(;p+4<=s->a;p+=4){put32(out+p,s->alphabet4[value&mask]);value>>=4*s->b;}for(;p<s->a;++p){out[p]=s->extra[value&((1U<<s->b)-1)];value>>=s->b;}out[s->a]='\n';return s->a+1;}
 case HEX:{uint32_t value=get32(s->data+id*4);uint64_t chars=hex8(s,value);unsigned n=(32-__builtin_clz(value|1U)+3)/4;if(n==8)put64(out,chars);else{chars>>=(8-n)*8;std::memcpy(out,&chars,n);}out[n]='\n';return n+1;}
 case UUID:{const __m128i packed=_mm_loadu_si128(reinterpret_cast<const __m128i*>(s->data+id*11));const __m128i mask=_mm_set1_epi8(15),alphabet=_mm_setr_epi8('0','1','2','3','4','5','6','7','8','9','a','b','c','d','e','f');__m128i a=_mm_and_si128(packed,mask),b=_mm_and_si128(_mm_srli_epi16(packed,4),mask);__m128i lo=_mm_shuffle_epi8(alphabet,_mm_unpacklo_epi8(a,b)),hi=_mm_shuffle_epi8(alphabet,_mm_unpackhi_epi8(a,b));__m128i tail=_mm_alignr_epi8(hi,lo,6);__m128i x=_mm_or_si128(s->uuid_fixed[0],_mm_shuffle_epi8(lo,_mm_setr_epi8(-128,-128,0,1,2,3,4,5,-128,-128,-128,-128,-128,-128,-128,-128)));__m128i y=_mm_or_si128(s->uuid_fixed[1],_mm_shuffle_epi8(tail,_mm_setr_epi8(-128,-128,-128,0,1,2,3,-128,4,5,6,7,8,9,10,11)));__m128i z=_mm_or_si128(s->uuid_fixed[2],_mm_shuffle_epi8(tail,_mm_setr_epi8(2,3,-128,4,5,6,7,8,9,10,11,12,13,14,15,-128)));_mm_storeu_si128(reinterpret_cast<__m128i*>(out),x);_mm_storeu_si128(reinterpret_cast<__m128i*>(out+16),y);_mm_storeu_si128(reinterpret_cast<__m128i*>(out+21),z);return 37;}
 case COORD:{const uint8_t*p=s->data+id*16;if(!p[0]){std::memcpy(out,"NULL\n",5);return 5;}unsigned n=p[0]&15,m=p[0]>>4;alignas(16)uint8_t chars[32];unpack_hex16(p+1,chars,false);out[0]='(';std::memcpy(out+1,s->decimal+s->a,2);out[3]='.';std::memcpy(out+4,chars,n);size_t pos=4+n;out[pos++]=',';out[pos++]=' ';out[pos++]='-';std::memcpy(out+pos,s->decimal+s->b+(p[8]>>4),2);pos+=2;out[pos++]='.';std::memcpy(out+pos,chars+16,m);pos+=m;out[pos++]=')';out[pos++]='\n';return pos;}
 default:return 0;
 }
}
template<unsigned Mode> inline int64_t decode_mode(State*s,uint8_t*out,size_t capacity){
 size_t pos=0;
 if constexpr(Mode==HEX){
  uint64_t r=0;const __m128i mask=_mm_set1_epi8(15),nine=_mm_set1_epi8(9),zero=_mm_set1_epi8('0'),adjust=_mm_set1_epi8(s->a?7:39);
  for(;r+4<=s->rows;){const uint8_t*p=s->data+r*4;uint32_t a=get32(p),b=get32(p+4),c=get32(p+8),d=get32(p+12);if(((a|b|c|d)&0xf0000000U)&&a>=0x10000000U&&b>=0x10000000U&&c>=0x10000000U&&d>=0x10000000U){if(capacity-pos<36)return -1;__m128i v=_mm_loadu_si128(reinterpret_cast<const __m128i*>(p));v=_mm_or_si128(_mm_slli_epi16(v,8),_mm_srli_epi16(v,8));v=_mm_shufflelo_epi16(v,_MM_SHUFFLE(2,3,0,1));v=_mm_shufflehi_epi16(v,_MM_SHUFFLE(2,3,0,1));__m128i high=_mm_and_si128(_mm_srli_epi16(v,4),mask),low=_mm_and_si128(v,mask);__m128i x=_mm_unpacklo_epi8(high,low),y=_mm_unpackhi_epi8(high,low);x=_mm_add_epi8(_mm_add_epi8(x,zero),_mm_and_si128(_mm_cmpgt_epi8(x,nine),adjust));y=_mm_add_epi8(_mm_add_epi8(y,zero),_mm_and_si128(_mm_cmpgt_epi8(y,nine),adjust));_mm_storel_epi64(reinterpret_cast<__m128i*>(out+pos),x);out[pos+8]='\n';_mm_storel_epi64(reinterpret_cast<__m128i*>(out+pos+9),_mm_srli_si128(x,8));out[pos+17]='\n';_mm_storel_epi64(reinterpret_cast<__m128i*>(out+pos+18),y);out[pos+26]='\n';_mm_storel_epi64(reinterpret_cast<__m128i*>(out+pos+27),_mm_srli_si128(y,8));out[pos+35]='\n';pos+=36;r+=4;}else{size_t len=row_size_mode<HEX>(s,r);if(len>capacity-pos)return -1;pos+=one_mode<HEX>(s,r++,out+pos);}}
  for(;r<s->rows;++r){size_t len=row_size_mode<HEX>(s,r);if(len>capacity-pos)return -1;pos+=one_mode<HEX>(s,r,out+pos);}return pos==s->raw?int64_t(pos):-1;
 }else if constexpr(Mode==DECIMAL||Mode==ALPHABET||Mode==UUID){
  const size_t len=row_size_mode<Mode>(s,0);for(uint64_t r=0;r<s->rows;++r)one_mode<Mode>(s,r,out+r*len);return s->raw;
 }else{
  for(uint64_t r=0;r<s->rows;++r){size_t len=row_size_mode<Mode>(s,r);if(len>capacity-pos)return -1;size_t n=one_mode<Mode>(s,r,out+pos);if(n!=len)return -1;pos+=n;}return pos==s->raw?int64_t(pos):-1;
 }
}
template<unsigned Mode> inline int64_t rows_mode(State*s,const uint64_t*ids,size_t count,uint8_t*out,size_t capacity,uint64_t*offsets){
 size_t pos=0;offsets[0]=0;
 if constexpr(Mode==DECIMAL||Mode==ALPHABET||Mode==UUID){size_t len=row_size_mode<Mode>(s,0);if(count>capacity/len)return -1;for(size_t r=0;r<count;++r){if(ids[r]>=s->rows)return -1;one_mode<Mode>(s,ids[r],out+pos);pos+=len;offsets[r+1]=pos;}return pos;}
 else{for(size_t r=0;r<count;++r){if(ids[r]>=s->rows)return -1;size_t len=row_size_mode<Mode>(s,ids[r]);if(len>capacity-pos)return -1;size_t n=one_mode<Mode>(s,ids[r],out+pos);if(n!=len)return -1;pos+=n;offsets[r+1]=pos;}return pos;}
}
inline int64_t decode(State*s,uint8_t*out,size_t capacity){if(!s||(!out&&s->raw)||capacity<s->raw)return -1;
 switch(s->mode){case DECIMAL:return decode_mode<DECIMAL>(s,out,capacity);case ALPHABET:return decode_mode<ALPHABET>(s,out,capacity);case HEX:return decode_mode<HEX>(s,out,capacity);case UUID:return decode_mode<UUID>(s,out,capacity);case COORD:return decode_mode<COORD>(s,out,capacity);default:return -1;}}
inline int64_t rows(State*s,const uint64_t*ids,size_t count,uint8_t*out,size_t capacity,uint64_t*offsets){if(!s||!offsets||(count&&(!out||!ids)))return -1;
 switch(s->mode){case DECIMAL:return rows_mode<DECIMAL>(s,ids,count,out,capacity,offsets);case ALPHABET:return rows_mode<ALPHABET>(s,ids,count,out,capacity,offsets);case HEX:return rows_mode<HEX>(s,ids,count,out,capacity,offsets);case UUID:return rows_mode<UUID>(s,ids,count,out,capacity,offsets);case COORD:return rows_mode<COORD>(s,ids,count,out,capacity,offsets);default:return -1;}}
}
