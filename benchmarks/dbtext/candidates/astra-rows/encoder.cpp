#include "codec.h"
#include "structure/encoder.hpp"
#include <algorithm>
#include <array>
#include <cstring>
#include <cstdint>
#include <vector>
#include <stdexcept>
#include <string>
#include "compressor/onpair_advanced/OnPairAdvancedCompressor.hpp"
using B=std::vector<uint8_t>;
template<class T>static T get(const uint8_t*p){T x;memcpy(&x,p,sizeof(x));return x;}
template<class T>static void put(B&b,T v){size_t p=b.size();b.resize(p+sizeof(T));memcpy(b.data()+p,&v,sizeof(v));}
namespace sgtt::compressor{std::string Compressor::getFileName(){return getName();}std::string Compressor::getInfo(){return "";}void Compressor::printInfo(){}}
struct Sym{std::array<uint8_t,16> data{};uint8_t len;};

// Store a symbol only when it is not a substring of a longer stored symbol.
// Hash only the used dictionary, never all possible substrings. Each substring
// probe confirms length and bytes, so hash collisions cannot alter the archive.
static B shared_pool(const std::vector<Sym>&used,std::vector<uint32_t>&offsets){
 const uint64_t basis=1469598103934665603ULL,prime=1099511628211ULL;
 size_t slots=1;while(slots<used.size()*2+1)slots*=2;size_t mask=slots-1;
 std::vector<uint64_t>hashes(slots);std::vector<uint32_t>table(slots,UINT32_MAX);
 std::array<std::vector<uint32_t>,17>by_length;std::array<uint32_t,17>remaining{};
 auto bucket=[&](uint64_t h){h^=h>>33;h*=0xff51afd7ed558ccdULL;h^=h>>33;return size_t(h)&mask;};
 size_t reserve=0;for(uint32_t id=0;id<used.size();++id){const auto&s=used[id];uint64_t h=basis;for(unsigned j=0;j<s.len;++j)h=(h^s.data[j])*prime;size_t p=bucket(h);while(table[p]!=UINT32_MAX)p=(p+1)&mask;table[p]=id;hashes[p]=h;by_length[s.len].push_back(id);remaining[s.len]++;reserve+=s.len;}
 B pool;pool.reserve(reserve);offsets.assign(used.size(),UINT32_MAX);
 for(int len=16;len>=1;--len)for(uint32_t id:by_length[len]){if(offsets[id]!=UINT32_MAX)continue;const auto&s=used[id];uint32_t base=pool.size();pool.insert(pool.end(),s.data.begin(),s.data.begin()+s.len);
  for(unsigned start=0;start<s.len;++start){uint64_t h=basis;for(unsigned end=start;end<s.len;++end){h=(h^s.data[end])*prime;unsigned n=end-start+1;if(!remaining[n])continue;size_t p=bucket(h);while(table[p]!=UINT32_MAX){uint32_t target=table[p];if(hashes[p]==h&&offsets[target]==UINT32_MAX&&used[target].len==n&&!memcmp(used[target].data.data(),s.data.data()+start,n)){offsets[target]=base+start;remaining[n]--;}p=(p+1)&mask;}}}
 }
 return pool;
}
extern "C" int64_t lab_encode(const uint8_t*raw,size_t n,uint8_t*out,size_t cap)try{
 B b;if(structure::encode(raw,n,b)){if(b.size()>cap)return -1;memcpy(out,b.data(),b.size());return b.size();}
 std::vector<std::string_view> rows;size_t start=0;for(size_t i=0;i<n;i++)if(raw[i]==10){rows.emplace_back((const char*)raw+start,i+1-start);start=i+1;}if(start<n)rows.emplace_back((const char*)raw+start,n-start);size_t nr=rows.size();
 using namespace sgtt::compressor;OnPairAdvancedCompressor<onpair::MaxSymbolLength::SIXTEEN> codec({true,true});codec.prepare(n);std::vector<std::byte> encoded;codec.compress(rows,encoded);
 const uint8_t*a=(const uint8_t*)encoded.data();size_t off=get<uint64_t>(a),nd=get<uint64_t>(a+8)-16;
 std::vector<uint16_t> left(256+nd),right(256+nd);std::vector<Sym> syms(256+nd);for(int i=0;i<256;i++){syms[i].data[0]=i;syms[i].len=1;}
 for(size_t i=0;i<nd;i++){uint16_t x=get<uint16_t>(a+16+4*i),y=get<uint16_t>(a+18+4*i);left[256+i]=x;right[256+i]=y;Sym s=syms[x];memcpy(s.data.data()+s.len,syms[y].data.data(),syms[y].len);s.len+=syms[y].len;syms[256+i]=s;}
 size_t nt=(encoded.size()-off-8)/2;const uint8_t*stream=a+off;std::vector<uint32_t>freq(syms.size());for(size_t i=0;i<nt;i++)freq[get<uint16_t>(stream+2*i)]++;

 std::vector<uint8_t> removed(syms.size());
 for(size_t i=syms.size();i-->256;)if(freq[i]){
   uint32_t f=freq[i];size_t x=left[i],y=right[i];
   int64_t added=2*int64_t(f);
   if(!freq[x])added+=syms[x].len+4;
   if(y!=x&&!freq[y])added+=syms[y].len+4;
   if(added<int64_t(syms[i].len+4)){
     removed[i]=1;freq[i]=0;freq[x]+=f;freq[y]+=f;
   }
 }
 std::vector<uint16_t> retokenized;retokenized.reserve(nt*2);
 auto emit=[&](auto&& self,uint16_t id)->void{if(removed[id]){self(self,left[id]);self(self,right[id]);}else retokenized.push_back(id);};
 for(size_t i=0;i<nt;++i)emit(emit,get<uint16_t>(stream+2*i));
 nt=retokenized.size();stream=reinterpret_cast<const uint8_t*>(retokenized.data());
 std::vector<uint16_t>remap(syms.size());std::vector<Sym>used;for(size_t i=0;i<syms.size();i++)if(freq[i]){remap[i]=used.size();used.push_back(syms[i]);}
 std::vector<uint32_t>index{0};size_t t=0;for(auto row:rows){size_t len=0;while(len<row.size()){len+=syms[get<uint16_t>(stream+2*t)].len;t++;}if(len!=row.size())return -1;index.push_back(t);}if(t!=nt)return -1;
 unsigned group=128;for(;;){bool ok=true;for(size_t i=0;i<index.size();i++)if(index[i]-index[i/group*group]>65535){ok=false;break;}if(ok)break;group/=2;if(!group)return -1;}
 put<uint32_t>(b,0x31575046);put<uint32_t>(b,2);put<uint64_t>(b,n);put<uint32_t>(b,nr);put<uint32_t>(b,used.size());put<uint32_t>(b,nt);put<uint32_t>(b,group);
 for(size_t i=0;i<index.size();i+=group)put<uint32_t>(b,index[i]);for(size_t i=0;i<index.size();i++)put<uint16_t>(b,index[i]-index[i/group*group]);
 std::vector<uint32_t> dict_offsets;B pool=shared_pool(used,dict_offsets);if(pool.size()>=16777216)return -1;for(size_t i=0;i<used.size();++i)put<uint32_t>(b,(uint32_t(used[i].len)<<24)|dict_offsets[i]);while(b.size()%16)b.push_back(0);b.insert(b.end(),pool.begin(),pool.end());b.insert(b.end(),16,0);for(size_t i=0;i<nt;i++)put<uint16_t>(b,remap[get<uint16_t>(stream+2*i)]);
 if(b.size()>cap)return -1;memcpy(out,b.data(),b.size());return b.size();
}catch(...){return -1;}
