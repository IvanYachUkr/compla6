#pragma once
#include "format.hpp"
#include <vector>
#include <algorithm>
#include <limits>
namespace structure {
inline void header(std::vector<uint8_t>& out,uint32_t mode,uint64_t raw,uint64_t rows,uint32_t a=0,uint32_t b=0,uint32_t c=0,uint32_t d=0){out.assign(HS,0);put32(out.data(),magic);put32(out.data()+4,mode);put64(out.data()+8,raw);put64(out.data()+16,rows);put32(out.data()+24,a);put32(out.data()+28,b);put32(out.data()+32,c);put32(out.data()+36,d);}
inline int hx(uint8_t c,bool upper){if(c>='0'&&c<='9')return c-'0';if(upper&&c>='A'&&c<='F')return c-'A'+10;if(!upper&&c>='a'&&c<='f')return c-'a'+10;return -1;}
inline bool encode_decimal(const uint8_t*raw,size_t size,size_t len,std::vector<uint8_t>&out){
 if(len<3||len>80||size%len)return false;
 size_t prefix=len-1;while(prefix&&raw[prefix-1]>='0'&&raw[prefix-1]<='9')--prefix;
 size_t digits=len-1-prefix;if(!prefix||digits<2||digits>9)return false;
 // Fold uniform leading decimal positions into the stored prefix.
 while(digits>1){uint8_t c=raw[prefix];bool same=true;for(size_t p=prefix;p<size;p+=len)if(raw[p]!=c){same=false;break;}if(!same)break;++prefix;--digits;}
 uint32_t max=0;size_t rows=size/len;
 for(size_t r=0;r<rows;++r){const auto*p=raw+r*len;if(std::memcmp(p,raw,prefix)||p[len-1]!='\n')return false;uint32_t v=0;for(size_t j=prefix;j<len-1;++j){if(p[j]<'0'||p[j]>'9')return false;v=v*10+p[j]-'0';}max=std::max(max,v);}
 unsigned bytes=max<256?1:max<65536?2:max<16777216?3:4;
 header(out,DECIMAL,size,rows,prefix,digits,bytes);out.insert(out.end(),raw,raw+prefix);size_t base=out.size();out.resize(base+rows*bytes+8,0);
 for(size_t r=0;r<rows;++r){const auto*p=raw+r*len;uint32_t v=0;for(size_t j=prefix;j<len-1;++j)v=v*10+p[j]-'0';for(unsigned j=0;j<bytes;++j)out[base+r*bytes+j]=v>>(8*j);}
 return out.size()<size;
}
inline bool encode_alphabet(const uint8_t*raw,size_t size,size_t len,std::vector<uint8_t>&out){
 if(len<3||len>13||size%len)return false;uint8_t seen[256]={};unsigned alph=0;for(size_t i=0;i<size;++i)if(raw[i]!='\n'&&!seen[raw[i]]){seen[raw[i]]=1;if(++alph>4)return false;}if(alph>4||alph<2)return false;
 // Accept any fixed-width four-symbol column, not just a named alphabet.
 for(size_t p=0;p<size;p+=len){if(raw[p+len-1]!='\n')return false;for(size_t j=0;j<len-1;++j)if(!seen[raw[p+j]])return false;}
 uint8_t alphabet[4]={},mapping[256]={};alph=0;for(unsigned i=0;i<256;++i)if(seen[i]){mapping[i]=alph;alphabet[alph++]=i;}unsigned bits=alph<=2?1:2,width=len-1,total=width*bits;size_t rows=size/len;
 header(out,ALPHABET,size,rows,width,bits,alph);out.insert(out.end(),alphabet,alphabet+4);size_t base=out.size();out.resize(base+(rows*total+7)/8+8,0);
 for(size_t r=0;r<rows;++r){uint64_t v=0;for(unsigned j=0;j<width;++j)v|=uint64_t(mapping[raw[r*len+j]])<<(bits*j);size_t off=r*total;uint64_t old=get64(out.data()+base+off/8);put64(out.data()+base+off/8,old|(v<<(off%8)));}return true;
}
inline bool encode_hex(const uint8_t*raw,size_t size,std::vector<uint8_t>&out){
 if(size<2)return false;bool upper=true;for(size_t j=0;j<std::min(size,size_t(4096));++j)if(raw[j]>='a'&&raw[j]<='f'){upper=false;break;}
 size_t rows=0;for(size_t p=0;p<size;){size_t start=p;uint32_t v=0;while(p<size&&raw[p]!='\n'){int d=hx(raw[p],upper);if(d<0||p-start>=8)return false;v=(v<<4)|d;++p;}if(p==size||p==start||(p-start>1&&raw[start]=='0'))return false;++p;++rows;}
 header(out,HEX,size,rows,upper);out.resize(HS+rows*4+8,0);size_t r=0;for(size_t p=0;p<size;){uint32_t v=0;while(raw[p]!='\n')v=(v<<4)|hx(raw[p++],upper);++p;put32(out.data()+HS+4*r++,v);}return out.size()<size;
}
inline bool encode_uuid(const uint8_t*raw,size_t size,std::vector<uint8_t>&out){
 if(size%37||size<37)return false;size_t rows=size/37;const unsigned positions[]={2,3,4,5,6,7,19,20,21,22,24,25,26,27,28,29,30,31,32,33,34,35};
 uint8_t variable[37]={};for(unsigned p:positions)variable[p]=1;
 for(size_t r=0;r<rows;++r){const auto*p=raw+r*37;for(unsigned j=0;j<37;++j){if(variable[j]){if(hx(p[j],false)<0)return false;}else if(p[j]!=raw[j])return false;}if(p[8]!='-'||p[13]!='-'||p[18]!='-'||p[23]!='-'||p[36]!='\n')return false;}
 header(out,UUID,size,rows);out.insert(out.end(),raw,raw+37);size_t base=out.size();out.resize(base+rows*11+16,0);for(size_t r=0;r<rows;++r)for(unsigned j=0;j<11;++j)out[base+r*11+j]=hx(raw[r*37+positions[j*2]],false)|(hx(raw[r*37+positions[j*2+1]],false)<<4);return true;
}
struct Coordinate{uint8_t n=0,m=0;const uint8_t*a=nullptr,*b=nullptr;unsigned x=0,y=0;bool null=false;};
inline bool coordinate(const uint8_t*raw,size_t size,size_t& pos,Coordinate& c){
 size_t p=pos;c={};if(size-p>=5&&!std::memcmp(raw+p,"NULL\n",5)){pos=p+5;c.null=true;return true;}if(p==size||raw[p++]!='(')return false;
 size_t begin=p;while(p<size&&raw[p]>='0'&&raw[p]<='9'){c.x=c.x*10+raw[p++]-'0';if(p-begin>2)return false;}if(p-begin!=2||p==size||raw[p++]!='.')return false;c.a=raw+p;while(p<size&&raw[p]>='0'&&raw[p]<='9'){++p;if(p-size_t(c.a-raw)>15)return false;}c.n=p-size_t(c.a-raw);if(!c.n||size-p<3||std::memcmp(raw+p,", -",3))return false;p+=3;
 begin=p;while(p<size&&raw[p]>='0'&&raw[p]<='9'){c.y=c.y*10+raw[p++]-'0';if(p-begin>2)return false;}if(p-begin!=2||p==size||raw[p++]!='.')return false;c.b=raw+p;while(p<size&&raw[p]>='0'&&raw[p]<='9'){++p;if(p-size_t(c.b-raw)>14)return false;}c.m=p-size_t(c.b-raw);if(!c.m||size-p<2||std::memcmp(raw+p,")\n",2))return false;pos=p+2;return true;
}
inline bool encode_coord(const uint8_t*raw,size_t size,std::vector<uint8_t>&out){
 size_t rows=0;unsigned x=100,ymn=100,ymx=0;for(size_t p=0;p<size;){Coordinate c;if(!coordinate(raw,size,p,c))return false;++rows;if(c.null)continue;if(x==100)x=c.x;if(c.x!=x)return false;ymn=std::min(ymn,c.y);ymx=std::max(ymx,c.y);}if(x>99||ymx-ymn>1)return false;
 header(out,COORD,size,rows,x,ymn);out.resize(HS+rows*16+16,0);size_t r=0;for(size_t p=0;p<size;++r){Coordinate c;coordinate(raw,size,p,c);auto*q=out.data()+HS+r*16;if(c.null)continue;q[0]=c.n|(c.m<<4);for(unsigned j=0;j<c.n;++j)q[1+j/2]|=(c.a[j]-'0')<<((j&1)*4);q[8]|=(c.y-ymn)<<4;for(unsigned j=0;j<c.m;++j)q[9+j/2]|=(c.b[j]-'0')<<((j&1)*4);}return out.size()<size;
}
inline bool encode(const uint8_t*raw,size_t size,std::vector<uint8_t>&out){
 if(!raw||size<4||raw[size-1]!='\n')return false;size_t len=0;while(len<size&&raw[len]!='\n'&&len<128)++len;if(len==size||len==128)return false;++len;
 if(raw[0]=='('&&encode_coord(raw,size,out))return true;
 if(len==37&&raw[8]=='-'&&raw[13]=='-'&&encode_uuid(raw,size,out))return true;
 if(len<=9&&encode_hex(raw,size,out))return true;
 if(encode_alphabet(raw,size,len,out))return true;
 if(encode_decimal(raw,size,len,out))return true;
 return false;
}
}
