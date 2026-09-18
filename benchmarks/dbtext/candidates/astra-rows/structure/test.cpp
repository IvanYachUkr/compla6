#include "encoder.hpp"
#include "decoder.hpp"
#include <fstream>
#include <iterator>
#include <iostream>
#include <chrono>
int main(int argc,char**argv){for(int i=1;i<argc;++i){std::ifstream f(argv[i],std::ios::binary);std::vector<uint8_t>raw((std::istreambuf_iterator<char>(f)),{}),arc,out(raw.size());auto t=std::chrono::steady_clock::now();bool ok=structure::encode(raw.data(),raw.size(),arc);auto t2=std::chrono::steady_clock::now();if(!ok){std::cout<<argv[i]<<" unsupported\n";continue;}auto*s=structure::open(arc.data(),arc.size());if(!s){std::cerr<<"open fail\n";return 1;}auto n=structure::decode(s,out.data(),out.size());if(n!=int64_t(raw.size())||raw!=out){std::cerr<<argv[i]<<" BAD mode "<<s->mode<<" returned "<<n<<" raw "<<raw.size()<<"\n";for(size_t j=0;j<raw.size();++j)if(raw[j]!=out[j]){std::cerr<<"first "<<j<<" "<<int(raw[j])<<" "<<int(out[j])<<"\n";break;}return 1;}auto t3=std::chrono::steady_clock::now();for(int k=0;k<100;++k)structure::decode(s,out.data(),out.size());auto t4=std::chrono::steady_clock::now();std::cout<<argv[i]<<" "<<raw.size()<<" => "<<arc.size()<<" mode "<<s->mode<<" encode "<<raw.size()/std::chrono::duration<double>(t2-t).count()/1e6<<" MB/s decode "<<100*raw.size()/std::chrono::duration<double>(t4-t3).count()/1e6<<" MB/s\n";structure::close(s);}}
