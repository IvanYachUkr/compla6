// Terra LZ v1 decoder.  It is a separate executable and parses only archive bytes.
#include <algorithm>
#include <array>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <exception>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace fs = std::filesystem;
using Byte = std::uint8_t;
using Bytes = std::vector<Byte>;
struct Fail : std::runtime_error { using std::runtime_error::runtime_error; };
static void require(bool ok, const char* text) { if (!ok) throw Fail(text); }

struct Reader {
    const Byte* p; const Byte* end;
    Reader(const Byte* a, const Byte* b):p(a),end(b) {}
    std::size_t left() const { return std::size_t(end-p); }
    Byte u8() { require(left()>=1,"truncated data"); return *p++; }
    std::uint32_t le16() { std::uint32_t a=u8(),b=u8(); return a|(b<<8); }
    std::uint32_t le24() { std::uint32_t a=u8(),b=u8(),c=u8(); return a|(b<<8)|(c<<16); }
    std::uint32_t le32() { std::uint32_t x=0; for(int i=0;i<4;++i)x|=std::uint32_t(u8())<<(8*i); return x; }
    std::uint64_t le64() { std::uint64_t x=0; for(int i=0;i<8;++i)x|=std::uint64_t(u8())<<(8*i); return x; }
    std::uint32_t be32() { std::uint32_t x=0; for(int i=0;i<4;++i)x=(x<<8)|u8(); return x; }
    std::uint64_t be64() { std::uint64_t x=0; for(int i=0;i<8;++i)x=(x<<8)|u8(); return x; }
    const Byte* take(std::size_t n) { require(n<=left(),"truncated data"); const Byte* q=p;p+=n;return q; }
};
static void put_be32(Bytes& out,std::uint32_t x){for(int i=3;i>=0;--i)out.push_back(Byte(x>>(8*i)));}
static void put_be64(Bytes& out,std::uint64_t x){for(int i=7;i>=0;--i)out.push_back(Byte(x>>(8*i)));}
static const std::array<std::uint32_t,256>& crc_table(){
    static const std::array<std::uint32_t,256> table=[] {
        std::array<std::uint32_t,256> t{};
        for(std::uint32_t i=0;i<256;++i){std::uint32_t x=i;for(int k=0;k<8;++k)x=(x>>1)^((x&1)?0xedb88320u:0u);t[i]=x;}
        return t;
    }(); return table;
}
static std::uint32_t crc32(const Byte* p,std::size_t n){
    std::uint32_t x=0xffffffffu;const auto& t=crc_table();for(std::size_t i=0;i<n;++i)x=t[(x^p[i])&255u]^(x>>8);return x^0xffffffffu;
}

struct Record {std::string alias;Bytes data;};
static bool valid_alias(const std::string& s){
    if(s.empty()||s.size()>1048576)return false;bool after_dash=true;
    for(unsigned char c:s){bool a=(c>='a'&&c<='z')||(c>='0'&&c<='9');if(c=='-'){if(after_dash)return false;after_dash=true;}else{if(!a)return false;after_dash=false;}}
    return !after_dash;
}
static std::vector<Record> parse_container(const Bytes& in,const char magic[4]){
    Reader r(in.data(),in.data()+in.size());require(r.left()>=9,"short container header");
    for(int i=0;i<4;++i)require(r.u8()==Byte(magic[i]),"wrong container magic");require(r.u8()==1,"unsupported container version");
    std::uint32_t n=r.be32();require(n>0&&n<=1000000,"invalid record count");std::vector<Record> rows;rows.reserve(n);std::string prior;
    for(std::uint32_t i=0;i<n;++i){std::uint32_t nn=r.be32();require(nn>0&&nn<=1048576,"invalid alias length");const Byte* q=r.take(nn);std::string a(reinterpret_cast<const char*>(q),nn);require(valid_alias(a),"invalid alias");require(i==0||prior<a,"aliases not strictly ordered");prior=a;std::uint64_t sz=r.be64();require(sz<=r.left(),"invalid record payload length");q=r.take(std::size_t(sz));rows.push_back({std::move(a),Bytes(q,q+std::size_t(sz))});}
    require(r.left()==0,"trailing container bytes");return rows;
}
static Bytes make_container(const char magic[4],const std::vector<Record>& rows){
    require(!rows.empty()&&rows.size()<=1000000,"invalid output record count");Bytes out;out.insert(out.end(),magic,magic+4);out.push_back(1);put_be32(out,std::uint32_t(rows.size()));std::string prior;
    for(std::size_t i=0;i<rows.size();++i){require(valid_alias(rows[i].alias),"invalid output alias");require(i==0||prior<rows[i].alias,"output aliases not ordered");prior=rows[i].alias;put_be32(out,std::uint32_t(rows[i].alias.size()));out.insert(out.end(),rows[i].alias.begin(),rows[i].alias.end());put_be64(out,rows[i].data.size());out.insert(out.end(),rows[i].data.begin(),rows[i].data.end());}return out;
}
static Bytes read_file(const fs::path& path){std::ifstream f(path,std::ios::binary);require(bool(f),"cannot open input file");f.seekg(0,std::ios::end);std::streamoff n=f.tellg();require(n>=0&&std::uint64_t(n)<=2147483648ull,"input file too large");f.seekg(0,std::ios::beg);Bytes out(static_cast<std::size_t>(n));if(n)f.read(reinterpret_cast<char*>(out.data()),n);require(bool(f)||n==0,"cannot read input file");return out;}
static void write_file(const fs::path& path,const Bytes& b){std::ofstream f(path,std::ios::binary|std::ios::trunc);require(bool(f),"cannot create output file");if(!b.empty())f.write(reinterpret_cast<const char*>(b.data()),std::streamsize(b.size()));require(bool(f),"cannot write output file");}
static Bytes read_stdin(){Bytes out;std::array<char,1<<16> b{};while(std::cin){std::cin.read(b.data(),std::streamsize(b.size()));std::streamsize n=std::cin.gcount();if(n>0){require(out.size()+std::size_t(n)<=2147483648ull,"stream too large");out.insert(out.end(),reinterpret_cast<Byte*>(b.data()),reinterpret_cast<Byte*>(b.data())+n);}}require(std::cin.eof(),"cannot read standard input");return out;}
static void write_stdout(const Bytes& b){if(!b.empty())std::cout.write(reinterpret_cast<const char*>(b.data()),std::streamsize(b.size()));require(bool(std::cout),"cannot write standard output");}
static std::string numbered_name(std::size_t i){char s[32];std::snprintf(s,sizeof(s),"%08zu.bin",i);return s;}
static bool is_empty_directory(const fs::path& p){std::error_code ec;if(!fs::is_directory(p,ec)||ec)return false;return fs::directory_iterator(p,ec)==fs::directory_iterator()&&!ec;}
static std::vector<Record> read_directory(const fs::path& dir,const char magic[4]){
    require(fs::is_directory(dir),"input is not a directory");std::vector<Record> rows=parse_container(read_file(dir/"names.bin"),magic);for(const Record& r:rows)require(r.data.empty(),"directory names payload must be empty");
    for(std::size_t i=0;i<rows.size();++i){fs::path p=dir/numbered_name(i);std::error_code ec;require(fs::is_regular_file(p,ec)&&!ec,"missing numbered input file");rows[i].data=read_file(p);}std::size_t seen=0;std::error_code ec;
    for(const auto& e:fs::directory_iterator(dir,ec)){require(!ec&&e.is_regular_file(ec)&&!ec,"unexpected nonregular input entry");std::string n=e.path().filename().string();bool ok=n=="names.bin";if(!ok)for(std::size_t i=0;i<rows.size();++i)if(n==numbered_name(i)){ok=true;break;}require(ok,"unexpected input file");++seen;}require(!ec&&seen==rows.size()+1,"bad input directory");return rows;
}
static void write_directory(const fs::path& dir,const char magic[4],const std::vector<Record>& rows){require(is_empty_directory(dir),"output directory must exist and be empty");std::vector<Record> names=rows;for(Record& r:names)r.data.clear();write_file(dir/"names.bin",make_container(magic,names));for(std::size_t i=0;i<rows.size();++i)write_file(dir/numbered_name(i),rows[i].data);}

struct BitReader {
    const Byte* p; const Byte* end; std::uint64_t acc=0; int bits=0;
    BitReader(const Byte* first,const Byte* last):p(first),end(last) {}
    bool fill(int n) {
        while (bits<n && p<end) { acc=(acc<<8)|*p++; bits+=8; }
        return bits>=n;
    }
    bool peek(int n,std::uint32_t& value) {
        if (!fill(n)) return false;
        value=std::uint32_t(acc>>(bits-n));
        if (n<32) value&=((std::uint32_t(1)<<n)-1);
        return true;
    }
    void drop(int n) {
        require(n>=0 && n<=bits,"invalid bit drop"); bits-=n;
        if (bits) acc&=((std::uint64_t(1)<<bits)-1); else acc=0;
    }
    bool get(int n,std::uint32_t& value) {
        if (!peek(n,value)) return false; drop(n); return true;
    }
    bool only_zero_padding() const {
        if (acc) return false;
        for (const Byte* q=p;q<end;++q) if (*q) return false;
        return true;
    }
};
struct HuffTree {
    struct Node { int child[2]; int symbol; Node():child{-1,-1},symbol(-1) {} };
    std::vector<Node> nodes;
    std::vector<std::uint32_t> fast;
};
static HuffTree build_tree(const Byte* lengths,std::size_t count_symbols) {
    std::array<std::uint32_t,21> count{},next{};
    int max_length=0;
    for (std::size_t symbol=0;symbol<count_symbols;++symbol) {
        require(lengths[symbol]<=20,"Huffman code too long");
        if (lengths[symbol]) { ++count[lengths[symbol]]; max_length=std::max(max_length,int(lengths[symbol])); }
    }
    std::uint32_t code=0;
    for (int length=1;length<=max_length;++length) {
        code=(code+count[length-1])<<1;
        require(code+count[length] <= (1u<<length),"invalid Huffman lengths");
        next[length]=code;
    }
    constexpr int FAST_BITS=12;
    HuffTree tree; tree.nodes.emplace_back(); tree.fast.assign(std::size_t(1)<<FAST_BITS,0);
    for (std::size_t symbol=0;symbol<count_symbols;++symbol) if (lengths[symbol]) {
        std::uint32_t bits=next[lengths[symbol]]++;
        if (lengths[symbol]<=FAST_BITS) {
            std::uint32_t first=bits<<(FAST_BITS-lengths[symbol]);
            std::uint32_t entry=(std::uint32_t(symbol+1)<<5)|lengths[symbol];
            for (std::uint32_t suffix=0;suffix<(std::uint32_t(1)<<(FAST_BITS-lengths[symbol]));++suffix) tree.fast[first+suffix]=entry;
        }
        int node=0;
        for (int shift=int(lengths[symbol])-1;shift>=0;--shift) {
            require(tree.nodes[node].symbol<0,"invalid Huffman prefix");
            int bit=(bits>>shift)&1; int child=tree.nodes[node].child[bit];
            if (child<0) { child=int(tree.nodes.size()); tree.nodes[node].child[bit]=child; tree.nodes.emplace_back(); }
            node=child;
        }
        require(tree.nodes[node].symbol<0 && tree.nodes[node].child[0]<0 && tree.nodes[node].child[1]<0,"duplicate Huffman code");
        tree.nodes[node].symbol=int(symbol);
    }
    return tree;
}
static int decode_symbol(BitReader& bits,const HuffTree& tree) {
    constexpr int FAST_BITS=12;
    std::uint32_t prefix=0;
    if (bits.peek(FAST_BITS,prefix)) {
        std::uint32_t entry=tree.fast[prefix];
        if (entry) { bits.drop(int(entry&31)); return int(entry>>5)-1; }
    }
    int node=0;
    for (int depth=0;depth<20;++depth) {
        std::uint32_t bit=0; require(bits.get(1,bit),"truncated token bitstream");
        node=tree.nodes[node].child[bit]; require(node>=0,"invalid token code");
        if (tree.nodes[node].symbol>=0) return tree.nodes[node].symbol;
    }
    throw Fail("overlong token code");
}
static constexpr std::size_t BLOCK_SIZE=8u*1024u*1024u;
static constexpr int LENGTH_SYMBOLS=278;
static constexpr int END_SYMBOL=277;
static constexpr int DISTANCE_SYMBOLS=28;
static std::size_t decode_length(int symbol,BitReader& bits) {
    if (symbol>=256 && symbol<=263) return std::size_t(3+symbol-256);
    require(symbol>=264 && symbol<=276,"invalid length symbol");
    int width=3+(symbol-264); std::uint32_t extra=0; require(bits.get(width,extra),"truncated length extra bits");
    std::size_t length=std::size_t(3)+(std::size_t(1)<<width)+extra;
    require(length<=65535,"invalid match length"); return length;
}
static std::size_t decode_distance(int symbol,BitReader& bits) {
    if (symbol>=0 && symbol<=2) return std::size_t(symbol+1);
    require(symbol>=3 && symbol<DISTANCE_SYMBOLS,"invalid distance symbol");
    int width=2+(symbol-3); std::uint32_t extra=0; require(bits.get(width,extra),"truncated distance extra bits");
    std::size_t distance=(std::size_t(1)<<width)+extra;
    require(distance<(std::size_t(1)<<27),"invalid match distance"); return distance;
}
static void decode_compressed_block(const Byte* payload,std::size_t payload_n,Bytes& output,std::size_t base,std::size_t out_n) {
    Reader r(payload,payload+payload_n); const Byte* length_lengths=r.take(LENGTH_SYMBOLS); const Byte* distance_lengths=r.take(DISTANCE_SYMBOLS);
    std::uint32_t bit_n=r.le32(); const Byte* stream=r.take(bit_n); require(r.left()==0,"trailing compressed block bytes");
    HuffTree length_tree=build_tree(length_lengths,LENGTH_SYMBOLS); HuffTree distance_tree=build_tree(distance_lengths,DISTANCE_SYMBOLS);
    BitReader bits(stream,stream+bit_n); std::size_t at=0;
    while (true) {
        int symbol=decode_symbol(bits,length_tree);
        if (symbol<256) { require(at<out_n,"literal exceeds block"); output[base+at++]=Byte(symbol); continue; }
        if (symbol==END_SYMBOL) break;
        std::size_t length=decode_length(symbol,bits); int distance_symbol=decode_symbol(bits,distance_tree); std::size_t distance=decode_distance(distance_symbol,bits);
        require(length<=out_n-at && distance>0 && distance<=base+at,"invalid match");
        if (distance>=length) std::memcpy(output.data()+base+at,output.data()+base+at-distance,length);
        else for (std::size_t i=0;i<length;++i) output[base+at+i]=output[base+at-distance+i];
        at+=length;
    }
    require(at==out_n,"early end token"); require(bits.only_zero_padding(),"nonzero token padding");
}
static Bytes decompress_core(const Bytes& archive) {
    Reader r(archive.data(),archive.data()+archive.size()); require(r.left()>=28,"short archive header"); const char expected[4]={'T','L','Z','Y'};
    for(int i=0;i<4;++i)require(r.u8()==Byte(expected[i]),"wrong archive magic");require(r.u8()==1,"unsupported archive version");require(r.u8()==0,"unsupported archive flags");require(r.le16()==0,"nonzero archive reserved field");
    std::uint64_t total64=r.le64();require(total64<=2147483648ull,"archive output too large");std::size_t total=std::size_t(total64);std::uint32_t block_size=r.le32(),block_count=r.le32(),whole_crc=r.le32();require(block_size==BLOCK_SIZE,"unsupported archive block size");std::size_t expected_count=(total+BLOCK_SIZE-1)/BLOCK_SIZE;require(block_count==expected_count,"archive block count mismatch");Bytes out(total);std::size_t at=0;
    for(std::uint32_t b=0;b<block_count;++b){std::uint32_t raw_n=r.le32(),block_crc=r.le32(),stored_n=r.le32();Byte kind=r.u8();std::size_t expected_n=std::min<std::size_t>(BLOCK_SIZE,total-at);require(raw_n==expected_n&&stored_n<=r.left(),"invalid block framing");const Byte* payload=r.take(stored_n);if(kind==0)decode_compressed_block(payload,stored_n,out,at,raw_n);else if(kind==1){require(stored_n==raw_n,"raw block size mismatch");if(raw_n)std::memcpy(out.data()+at,payload,raw_n);}else throw Fail("unknown block kind");require(crc32(out.data()+at,raw_n)==block_crc,"block checksum mismatch");at+=raw_n;}
    require(at==total&&r.left()==0,"trailing archive bytes");require(crc32(out.data(),out.size())==whole_crc,"archive checksum mismatch");return out;
}
static bool word_start(Byte c) {
    return (c>='A'&&c<='Z') || (c>='a'&&c<='z') || c=='_';
}
static bool word_continue(Byte c) {
    return word_start(c) || (c>='0'&&c<='9');
}
static Bytes decompress_payload(const Bytes& archive) {
    Reader r(archive.data(),archive.data()+archive.size()); require(r.left()>=22,"short token archive header");
    const char expected[4]={'T','L','Z','Z'}; for(int i=0;i<4;++i)require(r.u8()==Byte(expected[i]),"wrong token archive magic");
    require(r.u8()==1,"unsupported token archive version");require(r.u8()==0,"unsupported token archive flags");require(r.le16()==0,"nonzero token archive reserved field");
    std::uint64_t original64=r.le64();require(original64<=2147483648ull,"archive output too large");std::size_t original=std::size_t(original64);std::uint32_t expected_crc=r.le32();std::uint32_t count=r.le16();require(count<=382,"invalid dictionary count");
    std::vector<Bytes> words;words.reserve(count);
    for(std::uint32_t i=0;i<count;++i){std::uint32_t n=r.le16();require(n>=3&&n<=65535,"invalid dictionary word length");const Byte* p=r.take(n);for(std::uint32_t j=0;j<n;++j)require(word_continue(p[j]),"invalid dictionary word");require(word_start(p[0]),"invalid dictionary word");Bytes word(p,p+n);for(const Bytes& old:words)require(old!=word,"duplicate dictionary word");words.push_back(std::move(word));}
    std::uint32_t core_n=r.le32();require(core_n==r.left(),"invalid token core length");const Byte* core=r.take(core_n); Bytes transformed=decompress_core(Bytes(core,core+core_n));
    Bytes out;out.reserve(original);
    for(std::size_t p=0;p<transformed.size();++p){Byte c=transformed[p];if(c<0x80){require(out.size()<original,"expanded output too large");out.push_back(c);continue;}if(c<=0xfd){std::size_t code=std::size_t(c)-0x80;require(code<words.size()&&code<126&&out.size()+words[code].size()<=original,"invalid token reference");out.insert(out.end(),words[code].begin(),words[code].end());continue;}if(c==0xfe){require(++p<transformed.size(),"truncated extended token");std::size_t code=126+transformed[p];require(code<words.size()&&out.size()+words[code].size()<=original,"invalid extended token");out.insert(out.end(),words[code].begin(),words[code].end());continue;}require(++p<transformed.size(),"truncated token escape");Byte raw=transformed[p];require(raw>=0x80&&out.size()<original,"invalid token escape");out.push_back(raw);}
    require(out.size()==original&&crc32(out.data(),out.size())==expected_crc,"token archive checksum mismatch");return out;
}
static std::vector<Record> decode_records(std::vector<Record> rows){for(Record& r:rows)r.data=decompress_payload(r.data);return rows;}
static int run_stream(){std::vector<Record> rows=parse_container(read_stdin(),"HBA1");write_stdout(make_container("HBI1",decode_records(std::move(rows))));return 0;}
static int run_dir(const char* in,const char* out){std::vector<Record> rows=read_directory(in,"HBA1");write_directory(out,"HBI1",decode_records(std::move(rows)));return 0;}
int main(int argc,char** argv){try{if(argc==2&&std::string(argv[1])=="decode-stream")return run_stream();if(argc==4&&std::string(argv[1])=="decode-dir")return run_dir(argv[2],argv[3]);throw Fail("usage: decoder decode-stream | decoder decode-dir INPUT_DIR OUTPUT_DIR");}catch(const std::exception& e){std::cerr<<"decoder: "<<e.what()<<"\n";return 2;}}
