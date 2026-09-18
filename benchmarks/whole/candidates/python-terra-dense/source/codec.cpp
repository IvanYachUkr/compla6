// Terra LZ v1 encoder.  This source implements all compression operations itself.
#include <algorithm>
#include <array>
#include <atomic>
#include <cctype>
#include <cstdint>
#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <exception>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <limits>
#include <queue>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unordered_map>
#include <thread>
#include <utility>
#include <vector>

namespace fs = std::filesystem;
using Byte = std::uint8_t;
using Bytes = std::vector<Byte>;

struct Fail : std::runtime_error { using std::runtime_error::runtime_error; };
static void require(bool ok, const char* text) { if (!ok) throw Fail(text); }

static void put_le16(Bytes& out, std::uint32_t v) {
    out.push_back(Byte(v)); out.push_back(Byte(v >> 8));
}
static void put_le24(Bytes& out, std::uint32_t v) {
    out.push_back(Byte(v)); out.push_back(Byte(v >> 8)); out.push_back(Byte(v >> 16));
}
static void put_le32(Bytes& out, std::uint32_t v) {
    for (int i = 0; i != 4; ++i) out.push_back(Byte(v >> (8 * i)));
}
static void put_le64(Bytes& out, std::uint64_t v) {
    for (int i = 0; i != 8; ++i) out.push_back(Byte(v >> (8 * i)));
}
static void put_be32(Bytes& out, std::uint32_t v) {
    for (int i = 3; i >= 0; --i) out.push_back(Byte(v >> (8 * i)));
}
static void put_be64(Bytes& out, std::uint64_t v) {
    for (int i = 7; i >= 0; --i) out.push_back(Byte(v >> (8 * i)));
}

struct Reader {
    const Byte* p;
    const Byte* end;
    Reader(const Byte* a, const Byte* b) : p(a), end(b) {}
    std::size_t left() const { return std::size_t(end - p); }
    Byte u8() { require(left() >= 1, "truncated data"); return *p++; }
    std::uint32_t le16() { std::uint32_t a=u8(), b=u8(); return a | (b << 8); }
    std::uint32_t le24() { std::uint32_t a=u8(), b=u8(), c=u8(); return a | (b << 8) | (c << 16); }
    std::uint32_t le32() { std::uint32_t v=0; for (int i=0;i<4;++i) v |= std::uint32_t(u8()) << (8*i); return v; }
    std::uint64_t le64() { std::uint64_t v=0; for (int i=0;i<8;++i) v |= std::uint64_t(u8()) << (8*i); return v; }
    std::uint32_t be32() { std::uint32_t v=0; for (int i=0;i<4;++i) v = (v << 8) | u8(); return v; }
    std::uint64_t be64() { std::uint64_t v=0; for (int i=0;i<8;++i) v = (v << 8) | u8(); return v; }
    const Byte* take(std::size_t n) { require(n <= left(), "truncated data"); const Byte* q=p; p+=n; return q; }
};

static const std::array<std::uint32_t, 256>& crc_table() {
    static const std::array<std::uint32_t, 256> table = [] {
        std::array<std::uint32_t, 256> t{};
        for (std::uint32_t i=0; i<256; ++i) {
            std::uint32_t x=i;
            for (int k=0; k<8; ++k) x = (x >> 1) ^ ((x & 1) ? 0xedb88320u : 0u);
            t[i]=x;
        }
        return t;
    }();
    return table;
}
static std::uint32_t crc32(const Byte* p, std::size_t n) {
    std::uint32_t x=0xffffffffu;
    const auto& t=crc_table();
    for (std::size_t i=0; i<n; ++i) x=t[(x ^ p[i]) & 255u] ^ (x >> 8);
    return x ^ 0xffffffffu;
}

struct Record { std::string alias; Bytes data; };
static bool valid_alias(const std::string& s) {
    if (s.empty() || s.size() > 1048576) return false;
    bool after_dash=true;
    for (unsigned char c : s) {
        bool alnum=(c>='a'&&c<='z') || (c>='0'&&c<='9');
        if (c=='-') { if (after_dash) return false; after_dash=true; }
        else { if (!alnum) return false; after_dash=false; }
    }
    return !after_dash;
}
static std::vector<Record> parse_container(const Bytes& in, const char magic[4]) {
    Reader r(in.data(), in.data()+in.size());
    require(r.left() >= 9, "short container header");
    for (int i=0;i<4;++i) require(r.u8()==Byte(magic[i]), "wrong container magic");
    require(r.u8()==1, "unsupported container version");
    std::uint32_t n=r.be32();
    require(n>0 && n<=1000000, "invalid record count");
    std::vector<Record> rows; rows.reserve(n);
    std::string prior;
    for (std::uint32_t i=0;i<n;++i) {
        std::uint32_t name_n=r.be32();
        require(name_n>0 && name_n<=1048576, "invalid alias length");
        const Byte* name=r.take(name_n);
        std::string alias(reinterpret_cast<const char*>(name), name_n);
        require(valid_alias(alias), "invalid alias");
        require(i==0 || prior < alias, "aliases not strictly ordered");
        prior=alias;
        std::uint64_t size=r.be64();
        require(size <= r.left(), "invalid record payload length");
        const Byte* payload=r.take(std::size_t(size));
        Record row; row.alias=std::move(alias); row.data.assign(payload, payload+std::size_t(size));
        rows.push_back(std::move(row));
    }
    require(r.left()==0, "trailing container bytes");
    return rows;
}
static Bytes make_container(const char magic[4], const std::vector<Record>& rows) {
    require(!rows.empty() && rows.size()<=1000000, "invalid output record count");
    Bytes out; out.reserve(9);
    out.insert(out.end(), magic, magic+4); out.push_back(1); put_be32(out, std::uint32_t(rows.size()));
    std::string prior;
    for (std::size_t i=0;i<rows.size();++i) {
        require(valid_alias(rows[i].alias), "invalid output alias");
        require(i==0 || prior < rows[i].alias, "output aliases not ordered");
        prior=rows[i].alias;
        put_be32(out, std::uint32_t(rows[i].alias.size()));
        out.insert(out.end(), rows[i].alias.begin(), rows[i].alias.end());
        put_be64(out, rows[i].data.size());
        out.insert(out.end(), rows[i].data.begin(), rows[i].data.end());
    }
    return out;
}

static Bytes read_file(const fs::path& path) {
    std::ifstream f(path, std::ios::binary);
    require(bool(f), "cannot open input file");
    f.seekg(0, std::ios::end); std::streamoff n=f.tellg();
    require(n>=0 && std::uint64_t(n)<=2147483648ull, "input file too large");
    f.seekg(0, std::ios::beg);
    Bytes out(static_cast<std::size_t>(n));
    if (n) f.read(reinterpret_cast<char*>(out.data()), n);
    require(bool(f) || n==0, "cannot read input file");
    return out;
}
static void write_file(const fs::path& path, const Bytes& bytes) {
    std::ofstream f(path, std::ios::binary|std::ios::trunc);
    require(bool(f), "cannot create output file");
    if (!bytes.empty()) f.write(reinterpret_cast<const char*>(bytes.data()), std::streamsize(bytes.size()));
    require(bool(f), "cannot write output file");
}
static Bytes read_stdin() {
    Bytes out; std::array<char, 1<<16> buf{};
    while (std::cin) {
        std::cin.read(buf.data(), std::streamsize(buf.size()));
        std::streamsize n=std::cin.gcount();
        if (n>0) {
            require(out.size()+std::size_t(n)<=2147483648ull, "stream too large");
            out.insert(out.end(), reinterpret_cast<Byte*>(buf.data()), reinterpret_cast<Byte*>(buf.data())+n);
        }
    }
    require(std::cin.eof(), "cannot read standard input");
    return out;
}
static void write_stdout(const Bytes& bytes) {
    if (!bytes.empty()) std::cout.write(reinterpret_cast<const char*>(bytes.data()), std::streamsize(bytes.size()));
    require(bool(std::cout), "cannot write standard output");
}
static std::string numbered_name(std::size_t i) {
    char name[32]; std::snprintf(name, sizeof(name), "%08zu.bin", i); return name;
}
static bool is_empty_directory(const fs::path& path) {
    std::error_code ec;
    if (!fs::is_directory(path, ec) || ec) return false;
    return fs::directory_iterator(path, ec)==fs::directory_iterator() && !ec;
}
static std::vector<Record> read_directory(const fs::path& dir, const char magic[4]) {
    require(fs::is_directory(dir), "input is not a directory");
    Bytes names=read_file(dir/"names.bin");
    std::vector<Record> rows=parse_container(names, magic);
    for (const Record& r: rows) require(r.data.empty(), "directory names payload must be empty");
    for (std::size_t i=0;i<rows.size();++i) {
        fs::path p=dir/numbered_name(i);
        std::error_code ec;
        require(fs::is_regular_file(p, ec) && !ec, "missing numbered input file");
        rows[i].data=read_file(p);
    }
    std::size_t seen=0;
    std::error_code ec;
    for (const auto& e : fs::directory_iterator(dir, ec)) {
        require(!ec && e.is_regular_file(ec) && !ec, "unexpected nonregular input entry");
        std::string n=e.path().filename().string();
        bool allowed=n=="names.bin";
        if (!allowed) for (std::size_t i=0;i<rows.size();++i) if (n==numbered_name(i)) { allowed=true; break; }
        require(allowed, "unexpected input file"); ++seen;
    }
    require(!ec && seen==rows.size()+1, "bad input directory");
    return rows;
}
static void write_directory(const fs::path& dir, const char magic[4], const std::vector<Record>& rows) {
    require(is_empty_directory(dir), "output directory must exist and be empty");
    std::vector<Record> names=rows;
    for (Record& r : names) r.data.clear();
    write_file(dir/"names.bin", make_container(magic, names));
    for (std::size_t i=0;i<rows.size();++i) write_file(dir/numbered_name(i), rows[i].data);
}

template <std::size_t N>
struct Huffman {
    std::array<Byte,N> lengths{};
    std::array<std::uint32_t,N> codes{};
};
struct HuffNode { std::uint64_t freq; int left, right, symbol; };
struct HuffOrder {
    const std::vector<HuffNode>* nodes;
    bool operator()(int a, int b) const {
        const HuffNode& x=(*nodes)[a]; const HuffNode& y=(*nodes)[b];
        if (x.freq != y.freq) return x.freq > y.freq;
        return a > b;
    }
};
template <std::size_t N>
static void fill_canonical(Huffman<N>& h) {
    std::array<std::uint32_t,21> count{};
    int max_len=0;
    for (Byte length : h.lengths) {
        require(length<=20, "Huffman code too long");
        if (length) { ++count[length]; max_len=std::max(max_len,int(length)); }
    }
    std::array<std::uint32_t,21> next{};
    std::uint32_t code=0;
    for (int length=1; length<=max_len; ++length) {
        code=(code+count[length-1])<<1;
        require(code+count[length] <= (1u<<length), "invalid Huffman lengths");
        next[length]=code;
    }
    for (std::size_t symbol=0; symbol<N; ++symbol)
        if (h.lengths[symbol]) h.codes[symbol]=next[h.lengths[symbol]]++;
}
template <std::size_t N>
static Huffman<N> make_huffman(const std::array<std::uint64_t,N>& freq) {
    Huffman<N> h;
    std::vector<HuffNode> nodes; nodes.reserve(2*N);
    for (std::size_t s=0;s<N;++s) if (freq[s]) nodes.push_back({freq[s],-1,-1,int(s)});
    require(!nodes.empty(), "empty Huffman alphabet");
    if (nodes.size()==1) { h.lengths[nodes[0].symbol]=1; fill_canonical(h); return h; }
    HuffOrder order{&nodes};
    std::priority_queue<int,std::vector<int>,HuffOrder> queue(order);
    for (int i=0;i<int(nodes.size());++i) queue.push(i);
    while (queue.size()>1) {
        int a=queue.top(); queue.pop(); int b=queue.top(); queue.pop();
        nodes.push_back({nodes[a].freq+nodes[b].freq,a,b,-1}); queue.push(int(nodes.size()-1));
    }
    std::function<void(int,int)> visit=[&](int node,int depth) {
        if (nodes[node].symbol>=0) { h.lengths[nodes[node].symbol]=Byte(depth); return; }
        visit(nodes[node].left,depth+1); visit(nodes[node].right,depth+1);
    };
    visit(queue.top(),0);
    int max_len=0; for (Byte length : h.lengths) max_len=std::max(max_len,int(length));
    // A bounded code avoids an unrepresentable bit accumulator on adversarial inputs.
    // The fallback width must cover every used symbol (the length alphabet has 278).
    if (max_len>20) {
        std::size_t used=0; for (std::uint64_t f : freq) if (f) ++used;
        int width=1; while ((std::size_t(1)<<width) < used) ++width;
        for (std::size_t s=0;s<N;++s) if (freq[s]) h.lengths[s]=Byte(width);
    }
    fill_canonical(h); return h;
}
struct BitWriter {
    Bytes bytes; std::uint64_t acc=0; int bits=0;
    void put(std::uint32_t code,int n) {
        require(n>0 && n<=28, "invalid bit width");
        acc=(acc<<n)|code; bits+=n;
        while (bits>=8) {
            bytes.push_back(Byte(acc>>(bits-8))); bits-=8;
            if (bits) acc&=((std::uint64_t(1)<<bits)-1); else acc=0;
        }
    }
    void finish() { if (bits) { bytes.push_back(Byte(acc<<(8-bits))); acc=0; bits=0; } }
};

static constexpr std::size_t BLOCK_SIZE=8u*1024u*1024u;
static constexpr int HASH_BITS=20;
static constexpr std::size_t HASH_SIZE=std::size_t(1)<<HASH_BITS;
static constexpr std::size_t MIN_MATCH=4;
static constexpr std::size_t MAX_MATCH=65535;
static constexpr int MAX_CHAIN=64;
static constexpr int LENGTH_SYMBOLS=278;
static constexpr int END_SYMBOL=277;
static constexpr int DISTANCE_SYMBOLS=28;
static inline std::uint32_t hash4(const Byte* p) {
    std::uint32_t x=std::uint32_t(p[0]) | (std::uint32_t(p[1])<<8) |
                    (std::uint32_t(p[2])<<16) | (std::uint32_t(p[3])<<24);
    return (x*2654435761u)>>(32-HASH_BITS);
}
static int floor_log2(std::uint32_t x) {
    require(x>0, "zero logarithm"); return 31-__builtin_clz(x);
}
struct CodeExtra { int symbol; int bits; std::uint32_t value; };
static CodeExtra length_code(std::size_t length) {
    require(length>=3 && length<=65535, "invalid match length");
    std::uint32_t x=std::uint32_t(length-3);
    if (x<8) return {256+int(x),0,0};
    int k=floor_log2(x); return {264+(k-3),k,x-(std::uint32_t(1)<<k)};
}
static CodeExtra distance_code(std::size_t distance) {
    require(distance>0 && distance<(1u<<27), "invalid match distance");
    if (distance<=3) return {int(distance)-1,0,0};
    std::uint32_t x=std::uint32_t(distance); int k=floor_log2(x);
    return {3+(k-2),k,x-(std::uint32_t(1)<<k)};
}
struct Token { std::uint32_t value; std::uint32_t distance; };
struct EncodedBlock { Bytes payload; std::uint32_t crc=0; bool raw=false; std::uint32_t original=0; };

// Each entry links to the nearest earlier occurrence of its four-byte context.
// It is built before the parallel parse, so every worker can reference only bytes
// that a sequential decoder has already reconstructed.
struct PreviousIndex {
    std::vector<int> previous;
    explicit PreviousIndex(const Bytes& input) : previous(input.size(),-1) {
        std::vector<int> head(HASH_SIZE,-1);
        for (std::size_t pos=0;pos+MIN_MATCH<=input.size();++pos) {
            std::uint32_t h=hash4(input.data()+pos);
            previous[pos]=head[h]; head[h]=int(pos);
        }
    }
};
static void find_match(const Byte* input,std::size_t pos,std::size_t stop,const std::vector<int>& previous,std::size_t& best,std::size_t& best_distance) {
    best=0; best_distance=0;
    if (pos+MIN_MATCH>stop) return;
    int candidate=previous[pos];
    for (int probe=0;candidate>=0 && probe<MAX_CHAIN;++probe) {
        std::size_t source=std::size_t(candidate),length=0,limit=std::min(MAX_MATCH,stop-pos);
        while (length<limit && input[source+length]==input[pos+length]) ++length;
        if (length>best) { best=length; best_distance=pos-source; if (best==limit) break; }
        candidate=previous[source];
    }
    if (best<MIN_MATCH) best=0;
}
static EncodedBlock compress_block(const Byte* input,std::size_t total,std::size_t start,std::size_t n,const std::vector<int>& previous) {
    require(n<=BLOCK_SIZE && start+n<=total, "bad block size");
    const std::size_t stop=start+n;
    std::vector<Token> tokens; tokens.reserve(n/4);
    std::size_t pos=start,literal_start=start;
    auto emit_pending=[&](std::size_t end) { for (std::size_t q=literal_start;q<end;++q) tokens.push_back({input[q],0}); };
    while (pos<stop) {
        std::size_t best=0,best_distance=0; find_match(input,pos,stop,previous,best,best_distance);
        if (best && best<MAX_MATCH && pos+1<stop) {
            std::size_t next=0,next_distance=0; find_match(input,pos+1,stop,previous,next,next_distance);
            // Spending one literal is useful only when the delayed match gains at least two bytes.
            if (next>best+1) { ++pos; continue; }
        }
        if (best) { emit_pending(pos); tokens.push_back({std::uint32_t(best),std::uint32_t(best_distance)}); pos+=best; literal_start=pos; }
        else ++pos;
    }
    emit_pending(stop);
    std::array<std::uint64_t,LENGTH_SYMBOLS> length_freq{};
    std::array<std::uint64_t,DISTANCE_SYMBOLS> distance_freq{};
    for (const Token& t:tokens) {
        if (!t.distance) ++length_freq[t.value];
        else { CodeExtra l=length_code(t.value),d=distance_code(t.distance);++length_freq[l.symbol];++distance_freq[d.symbol]; }
    }
    ++length_freq[END_SYMBOL];
    Huffman<LENGTH_SYMBOLS> lengths=make_huffman(length_freq);
    Huffman<DISTANCE_SYMBOLS> distances; bool have_distance=false;
    for (std::uint64_t f:distance_freq) if (f) {have_distance=true;break;}
    if (have_distance) distances=make_huffman(distance_freq);
    BitWriter bits; bits.bytes.reserve(n/4);
    for (const Token& t:tokens) {
        if (!t.distance) bits.put(lengths.codes[t.value],lengths.lengths[t.value]);
        else { CodeExtra l=length_code(t.value),d=distance_code(t.distance);bits.put(lengths.codes[l.symbol],lengths.lengths[l.symbol]);if(l.bits)bits.put(l.value,l.bits);bits.put(distances.codes[d.symbol],distances.lengths[d.symbol]);if(d.bits)bits.put(d.value,d.bits); }
    }
    bits.put(lengths.codes[END_SYMBOL],lengths.lengths[END_SYMBOL]); bits.finish();
    Bytes packed; packed.reserve(LENGTH_SYMBOLS+DISTANCE_SYMBOLS+4+bits.bytes.size());
    packed.insert(packed.end(),lengths.lengths.begin(),lengths.lengths.end());
    packed.insert(packed.end(),distances.lengths.begin(),distances.lengths.end());
    put_le32(packed,std::uint32_t(bits.bytes.size())); packed.insert(packed.end(),bits.bytes.begin(),bits.bytes.end());
    EncodedBlock out;out.crc=crc32(input+start,n);out.original=std::uint32_t(n);
    if(packed.size()<n)out.payload=std::move(packed);else{out.raw=true;out.payload.assign(input+start,input+stop);}return out;
}
static int configured_threads() {
    const char* s=std::getenv("COMPRESSION_LAB_THREADS"); if(!s||!*s)return 4;
    char* end=nullptr;long v=std::strtol(s,&end,10);if(end==s||*end||v<1)return 1;return int(std::min<long>(4,v));
}
static Bytes compress_core(const Bytes& input) {
    require(input.size()<=2147483648ull,"input too large");
    PreviousIndex index(input);
    std::size_t count=(input.size()+BLOCK_SIZE-1)/BLOCK_SIZE;
    std::vector<EncodedBlock> blocks(count);std::atomic<std::size_t> next{0};
    auto worker=[&](){while(true){std::size_t b=next.fetch_add(1);if(b>=count)break;std::size_t start=b*BLOCK_SIZE,size=std::min(BLOCK_SIZE,input.size()-start);blocks[b]=compress_block(input.data(),input.size(),start,size,index.previous);}};
    int workers=std::min<int>(configured_threads(),std::max<std::size_t>(1,count));std::vector<std::thread> other;other.reserve(workers-1);for(int i=1;i<workers;++i)other.emplace_back(worker);worker();for(std::thread& t:other)t.join();
    Bytes out;out.reserve(input.size()/4);out.insert(out.end(),{'T','L','Z','Y'});out.push_back(1);out.push_back(0);put_le16(out,0);put_le64(out,input.size());put_le32(out,std::uint32_t(BLOCK_SIZE));put_le32(out,std::uint32_t(count));put_le32(out,crc32(input.data(),input.size()));
    for(const EncodedBlock& b:blocks){put_le32(out,b.original);put_le32(out,b.crc);put_le32(out,std::uint32_t(b.payload.size()));out.push_back(b.raw?1:0);out.insert(out.end(),b.payload.begin(),b.payload.end());}return out;
}
// Codes 0x80..0xfd are 126 direct words.  0xfe followed by a byte
// selects one of 256 extended words, while 0xff escapes an original high byte.
static bool word_start(Byte c) {
    return (c>='A'&&c<='Z') || (c>='a'&&c<='z') || c=='_';
}
static bool word_continue(Byte c) {
    return word_start(c) || (c>='0'&&c<='9');
}
struct ViewHash {
    std::size_t operator()(std::string_view s) const noexcept {
        std::uint64_t h=1469598103934665603ull;
        for (unsigned char c:s) h=(h^c)*1099511628211ull;
        return std::size_t(h);
    }
};
struct ReverseNode {
    std::array<int,128> child;
    int code;
    ReverseNode() : code(-1) { child.fill(-1); }
};
struct WordDictionary {
    std::vector<std::string_view> words;
    std::unordered_map<std::string_view,std::uint16_t,ViewHash> ids;
    std::vector<ReverseNode> suffixes;
};
static WordDictionary make_dictionary(const Bytes& input) {
    std::unordered_map<std::string_view,std::uint32_t,ViewHash> counts;
    counts.reserve(1u<<20);
    for (std::size_t p=0;p<input.size();) {
        if (!word_start(input[p])) { ++p; continue; }
        std::size_t q=p+1; while (q<input.size() && word_continue(input[q])) ++q;
        ++counts[std::string_view(reinterpret_cast<const char*>(input.data()+p),q-p)]; p=q;
    }
    struct Choice { std::string_view word; std::uint64_t two_gain; std::uint32_t count; };
    std::vector<Choice> choices; choices.reserve(counts.size());
    for (const auto& pair:counts) {
        std::size_t n=pair.first.size(); if (n<3 || n>65535) continue;
        std::uint64_t gain=std::uint64_t(pair.second)*(n-2);
        if (gain>n+2) choices.push_back({pair.first,gain-(n+2),pair.second});
    }
    std::sort(choices.begin(),choices.end(),[](const Choice& a,const Choice& b) {
        if (a.two_gain!=b.two_gain) return a.two_gain>b.two_gain;
        if (a.count!=b.count) return a.count>b.count;
        return a.word<b.word;
    });
    if (choices.size()>382) choices.resize(382);
    std::sort(choices.begin(),choices.end(),[](const Choice& a,const Choice& b) {
        if (a.count!=b.count) return a.count>b.count;
        if (a.two_gain!=b.two_gain) return a.two_gain>b.two_gain;
        return a.word<b.word;
    });
    std::size_t direct=std::min<std::size_t>(126,choices.size());
    std::sort(choices.begin()+direct,choices.end(),[](const Choice& a,const Choice& b) {
        if (a.two_gain!=b.two_gain) return a.two_gain>b.two_gain;
        if (a.count!=b.count) return a.count>b.count;
        return a.word<b.word;
    });
    WordDictionary dictionary; dictionary.words.reserve(choices.size()); dictionary.ids.reserve(choices.size()*2+1);
    std::size_t trie_nodes=1;
    for (std::size_t i=0;i<choices.size();++i) { dictionary.words.push_back(choices[i].word); dictionary.ids.emplace(choices[i].word,std::uint16_t(i)); trie_nodes+=choices[i].word.size(); }
    dictionary.suffixes.reserve(trie_nodes); dictionary.suffixes.emplace_back();
    for (std::size_t code=0;code<dictionary.words.size();++code) {
        int node=0; std::string_view word=dictionary.words[code];
        for (std::size_t j=word.size();j>0;--j) {
            Byte c=Byte(word[j-1]); int child=dictionary.suffixes[node].child[c];
            if (child<0) { child=int(dictionary.suffixes.size()); dictionary.suffixes[node].child[c]=child; dictionary.suffixes.emplace_back(); }
            node=child;
        }
        dictionary.suffixes[node].code=int(code);
    }
    return dictionary;
}
static Bytes transform_words(const Bytes& input,const WordDictionary& dictionary) {
    Bytes out; out.reserve(input.size());
    auto emit_code=[&](std::uint16_t code) {
        if (code<126) out.push_back(Byte(0x80u+code));
        else { out.push_back(0xfe); out.push_back(Byte(code-126)); }
    };
    for (std::size_t p=0;p<input.size();) {
        if (word_start(input[p])) {
            std::size_t q=p+1; while (q<input.size() && word_continue(input[q])) ++q;
            std::string_view word(reinterpret_cast<const char*>(input.data()+p),q-p);
            auto found=dictionary.ids.find(word);
            if (found!=dictionary.ids.end()) { emit_code(found->second); p=q; continue; }
            int node=0,suffix_code=-1; std::size_t suffix_length=0;
            for (std::size_t length=1;length<=q-p;++length) {
                int child=dictionary.suffixes[node].child[input[q-length]];
                if (child<0) break; node=child;
                if (dictionary.suffixes[node].code>=0) { suffix_code=dictionary.suffixes[node].code; suffix_length=length; }
            }
            if (suffix_code>=0) { out.insert(out.end(),input.data()+p,input.data()+q-suffix_length); emit_code(std::uint16_t(suffix_code)); }
            else out.insert(out.end(),input.data()+p,input.data()+q);
            p=q; require(out.size()<=2147483648ull,"transformed input too large"); continue;
        }
        Byte c=input[p++]; if (c<0x80) out.push_back(c); else { out.push_back(0xff); out.push_back(c); }
        require(out.size()<=2147483648ull,"transformed input too large");
    }
    return out;
}
static Bytes compress_payload(const Bytes& input) {
    WordDictionary dictionary=make_dictionary(input);
    Bytes transformed=transform_words(input,dictionary);
    Bytes core=compress_core(transformed);
    require(core.size()<=std::numeric_limits<std::uint32_t>::max(),"archive too large");
    Bytes out; out.reserve(22+dictionary.words.size()*12+core.size());
    out.insert(out.end(),{'T','L','Z','Z'}); out.push_back(1); out.push_back(0); put_le16(out,0);
    put_le64(out,input.size()); put_le32(out,crc32(input.data(),input.size())); put_le16(out,std::uint32_t(dictionary.words.size()));
    for (std::string_view word:dictionary.words) {
        put_le16(out,std::uint32_t(word.size())); out.insert(out.end(),word.begin(),word.end());
    }
    put_le32(out,std::uint32_t(core.size())); out.insert(out.end(),core.begin(),core.end());
    return out;
}
static std::vector<Record> encode_records(std::vector<Record> in) {
    for (Record& r:in) r.data=compress_payload(r.data);
    return in;
}
static int run_stream() {
    std::vector<Record> rows=parse_container(read_stdin(),"HBI1");
    write_stdout(make_container("HBA1",encode_records(std::move(rows)))); return 0;
}
static int run_dir(const char* in, const char* out) {
    std::vector<Record> rows=read_directory(in,"HBI1");
    write_directory(out,"HBA1",encode_records(std::move(rows))); return 0;
}
int main(int argc, char** argv) {
    try {
        if (argc==2 && std::string(argv[1])=="encode-stream") return run_stream();
        if (argc==4 && std::string(argv[1])=="encode-dir") return run_dir(argv[2],argv[3]);
        throw Fail("usage: codec encode-stream | codec encode-dir INPUT_DIR OUTPUT_DIR");
    } catch (const std::exception& e) { std::cerr << "codec: " << e.what() << "\n"; return 2; }
}
