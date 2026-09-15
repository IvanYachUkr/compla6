// Compression Lab 0.1.0 conventional native control. SPDX-License-Identifier: MIT
#include <algorithm>
#include <array>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>
#ifndef CL_CODEC
#define CL_CODEC 0
#endif
#ifndef CL_LEVEL
#define CL_LEVEL 1
#endif
#if CL_CODEC == 1
#include <zstd.h>
#elif CL_CODEC == 2
#include <lz4.h>
#elif CL_CODEC == 3
#include <brotli/encode.h>
#include <brotli/decode.h>
#elif CL_CODEC == 4
#include <lzma.h>
#endif
namespace fs=std::filesystem;
using B=std::vector<uint8_t>;
struct R{std::string name;B data;bool operator==(const R&o)const{return name==o.name&&data==o.data;}};
constexpr uint64_t LIMIT=uint64_t(8)<<30;
void need(bool ok,const char*m){if(!ok)throw std::runtime_error(m);}
void put32(B&b,uint32_t n){for(int s=24;s>=0;s-=8)b.push_back(uint8_t(n>>s));}
void put64(B&b,uint64_t n){for(int s=56;s>=0;s-=8)b.push_back(uint8_t(n>>s));}
struct Reader{const B&b;size_t p=0;void check(uint64_t n){need(p<=b.size()&&n<=b.size()-p,"truncated framing");}uint32_t u32(){check(4);uint32_t x=0;for(int i=0;i<4;i++)x=(x<<8)|b[p++];return x;}uint64_t u64(){check(8);uint64_t x=0;for(int i=0;i<8;i++)x=(x<<8)|b[p++];return x;}};
bool alias(const std::string&s){if(s.empty()||s.size()>1048576)return false;bool d=true;for(unsigned char c:s){if((c>='a'&&c<='z')||(c>='0'&&c<='9'))d=false;else if(c=='-'&&!d)d=true;else return false;}return !d;}
B read(std::istream&f){B b;std::array<char,65536>a{};while(f){f.read(a.data(),a.size());auto n=f.gcount();need(n>=0&&b.size()+uint64_t(n)<=LIMIT,"input limit");b.insert(b.end(),a.data(),a.data()+n);}need(f.eof(),"read failure");return b;}
B file(const fs::path&p){need(fs::is_regular_file(fs::symlink_status(p)),"nonregular input");std::ifstream f(p,std::ios::binary);need(bool(f),"open failure");return read(f);}
void write(const fs::path&p,const B&b){need(!fs::exists(p),"existing output");std::ofstream f(p,std::ios::binary);need(bool(f),"open output failure");f.write(reinterpret_cast<const char*>(b.data()),b.size());f.close();need(bool(f),"write failure");}
std::vector<R> unpack(const B&b,bool arc){Reader r{b};r.check(9);need(!memcmp(b.data(),arc?"HBA1":"HBI1",4),"bad magic");r.p=4;need(b[r.p++]==1,"bad version");uint32_t count=r.u32();need(count&&count<=1000000&&uint64_t(count)*13<=b.size()-r.p,"bad count");std::vector<R>v;v.reserve(count);std::string prev;for(uint32_t i=0;i<count;i++){auto n=r.u32();need(n&&n<=1048576,"bad alias length");r.check(n);std::string name(reinterpret_cast<const char*>(b.data()+r.p),n);r.p+=n;need(alias(name)&&(i==0||prev<name),"bad alias/order");prev=name;uint64_t size=r.u64();r.check(size);v.push_back({std::move(name),B(b.begin()+r.p,b.begin()+r.p+size)});r.p+=size;}need(r.p==b.size(),"trailing bytes");return v;}
B pack(const std::vector<R>&v,bool arc){need(!v.empty()&&v.size()<=1000000,"bad output count");const char*m=arc?"HBA1":"HBI1";B b(m,m+4);b.push_back(1);put32(b,v.size());std::string prev;for(const auto&r:v){need(alias(r.name)&&(prev.empty()||prev<r.name),"bad output alias");prev=r.name;need(b.size()+r.name.size()+r.data.size()+12<=LIMIT,"output limit");put32(b,r.name.size());b.insert(b.end(),r.name.begin(),r.name.end());put64(b,r.data.size());b.insert(b.end(),r.data.begin(),r.data.end());}return b;}
uint32_t update(uint32_t c,const uint8_t*p,size_t n){static const auto t=[](){std::array<uint32_t,256>a{};for(uint32_t i=0;i<256;i++){uint32_t x=i;for(int j=0;j<8;j++)x=(x>>1)^(0xedb88320U&uint32_t(-int(x&1)));a[i]=x;}return a;}();for(size_t i=0;i<n;i++)c=(c>>8)^t[(c^p[i])&255];return c;}
uint32_t crc(const B&b){return ~update(0xffffffffU,b.data(),b.size());}
B compress(const B&b,const B&dict){
#if CL_CODEC == 1
 B o(ZSTD_compressBound(b.size()));auto c=ZSTD_createCCtx();need(c,"zstd allocation");size_t n=ZSTD_compress_usingDict(c,o.data(),o.size(),b.data(),b.size(),dict.data(),dict.size(),CL_LEVEL);ZSTD_freeCCtx(c);need(!ZSTD_isError(n),"zstd encode");o.resize(n);return o;
#elif CL_CODEC == 2
 if(b.size()>LZ4_MAX_INPUT_SIZE)return b;B o(LZ4_compressBound(int(b.size())));int n=LZ4_compress_default((const char*)b.data(),(char*)o.data(),int(b.size()),int(o.size()));need(n>0,"lz4 encode");o.resize(n);return o;
#elif CL_CODEC == 3
 size_t n=BrotliEncoderMaxCompressedSize(b.size());B o(n);need(BrotliEncoderCompress(CL_LEVEL,BROTLI_DEFAULT_WINDOW,BROTLI_MODE_GENERIC,b.size(),b.data(),&n,o.data())==BROTLI_TRUE,"brotli encode");o.resize(n);return o;
#elif CL_CODEC == 4
 size_t n=0;B o(lzma_stream_buffer_bound(b.size()));need(lzma_easy_buffer_encode(CL_LEVEL,LZMA_CHECK_CRC32,nullptr,b.data(),b.size(),o.data(),&n,o.size())==LZMA_OK,"xz encode");o.resize(n);return o;
#else
 (void)dict;return b;
#endif
}
B encode(const B&b,const B&dict){B p=b.empty()?b:compress(b,dict);uint8_t codec=CL_CODEC;if(p.size()>=b.size()){p=b;codec=0;}B o={'C','L','B','1',1,codec,0,0};put64(o,b.size());put64(o,p.size());put32(o,crc(b));put32(o,0);o.insert(o.end(),p.begin(),p.end());uint32_t c=~update(update(0xffffffffU,o.data(),28),o.data()+32,o.size()-32);for(int i=0;i<4;i++)o[28+i]=uint8_t(c>>(24-8*i));return o;}
B decode(const B&b,const B&dict){need(b.size()>=32&&!memcmp(b.data(),"CLB1",4),"bad archive");need(b[4]==1&&!b[6]&&!b[7]&&(b[5]==0||b[5]==CL_CODEC),"bad archive flags");Reader r{b};r.p=8;auto raw=r.u64(),size=r.u64();auto c=r.u32(),outer=r.u32();need(raw<=LIMIT&&size==b.size()-32,"bad archive lengths");need(outer==~update(update(0xffffffffU,b.data(),28),b.data()+32,b.size()-32),"archive checksum");B o;if(b[5]==0){need(raw==size,"stored length");o.assign(b.begin()+32,b.end());}else{o.resize(raw);
#if CL_CODEC == 1
 auto ctx=ZSTD_createDCtx();need(ctx,"zstd allocation");size_t n=ZSTD_decompress_usingDict(ctx,o.data(),o.size(),b.data()+32,size,dict.data(),dict.size());ZSTD_freeDCtx(ctx);need(!ZSTD_isError(n)&&n==raw,"zstd decode");
#elif CL_CODEC == 2
 need(size<=INT32_MAX&&raw<=INT32_MAX,"lz4 bounds");int n=LZ4_decompress_safe((const char*)b.data()+32,(char*)o.data(),int(size),int(raw));need(n>=0&&uint64_t(n)==raw,"lz4 decode");
#elif CL_CODEC == 3
 size_t n=raw;need(BrotliDecoderDecompress(size,b.data()+32,&n,o.data())==BROTLI_DECODER_RESULT_SUCCESS&&n==raw,"brotli decode");
#elif CL_CODEC == 4
 uint64_t mem=1ULL<<30;size_t in=0,out=0;need(lzma_stream_buffer_decode(&mem,0,nullptr,b.data()+32,&in,size,o.data(),&out,raw)==LZMA_OK&&in==size&&out==raw,"xz decode");
#else
 throw std::runtime_error("unsupported codec");
#endif
 }need(o.size()==raw&&crc(o)==c,"raw checksum");return o;}
std::string ordinal(size_t i){char b[32];std::snprintf(b,sizeof(b),"%08zu.bin",i);return b;}
std::vector<R> read_dir(const fs::path&p,bool arc){auto v=unpack(file(p/"names.bin"),arc);size_t n=0;for(const auto&e:fs::directory_iterator(p)){need(fs::is_regular_file(e.symlink_status()),"nonregular directory entry");n++;}need(n==v.size()+1,"directory extra/missing file");for(size_t i=0;i<v.size();i++){need(v[i].data.empty(),"nonempty index payload");v[i].data=file(p/ordinal(i));}return v;}
void write_dir(const fs::path&p,const std::vector<R>&v,bool arc){need(fs::is_directory(p)&&fs::is_empty(p),"nonempty output directory");std::vector<R>names;for(const auto&r:v)names.push_back({r.name,{}});auto index=pack(names,arc);for(size_t i=0;i<v.size();i++)write(p/ordinal(i),v[i].data);write(p/"names.bin",index);}
int main(int argc,char**argv){try{need(argc>=2,"missing operation");std::string op=argv[1];bool dir=op=="encode-dir"||op=="decode-dir",enc=op=="encode-dir"||op=="encode-stream";need(dir||op=="encode-stream"||op=="decode-stream","unknown operation");int n=dir?4:2;need(argc==n||argc==n+1,"argument count");B dict;if(argc==n+1)dict=file(argv[n]);std::ios::sync_with_stdio(false);std::cin.tie(nullptr);auto v=dir?read_dir(argv[2],!enc):unpack(read(std::cin),!enc);for(auto&r:v)r.data=enc?encode(r.data,dict):decode(r.data,dict);if(dir)write_dir(argv[3],v,enc);else{auto b=pack(v,enc);std::cout.write((const char*)b.data(),b.size());std::cout.flush();need(bool(std::cout),"stdout failure");}return 0;}catch(const std::exception&e){std::cerr<<"codec_error: "<<e.what()<<'\n';return 2;}}
