// pypack-size decoder: LZMA stream decode + indentation-transform inversion.
// Independent reconstruction from the archive plus pinned liblzma only.
// See codec.cpp for the format description.
#include "format.h"
#include <lzma.h>
#include <cstring>
#include <stdexcept>

using namespace pylz;

namespace {

constexpr int DICT_LOG = 26;

static void invert(const std::vector<uint8_t>& src, std::vector<uint8_t>& dst,
                   uint8_t marker) {
  dst.clear();
  dst.reserve(src.size() + src.size() / 8 + 16);
  bool bol = true;
  for (size_t i = 0; i < src.size(); ++i) {
    uint8_t c = src[i];
    if (c == marker && i + 1 < src.size()) {
      size_t run = (size_t)src[++i] + 1;
      dst.insert(dst.end(), run, ' ');
      bol = false;
      continue;
    }
    dst.push_back(c);
    bol = (c == '\n');
  }
}

std::vector<uint8_t> decompress_size(const std::vector<uint8_t>& arc) {
  uint64_t orig_len = 0, want = 0;
  size_t body = 0;
  uint8_t flags = 0;
  if (!get_header(arc.data(), arc.size(), 2, orig_len, body, flags, want) ||
      orig_len > (uint64_t)1 << 33 || body + 1 > arc.size())
    throw std::runtime_error("bad header");
  uint8_t marker = arc[body++];

  lzma_stream s = LZMA_STREAM_INIT;
  if (lzma_alone_decoder(&s, (uint64_t)1 << (DICT_LOG + 1)) != LZMA_OK)
    throw std::runtime_error("lzma decoder init");
  std::vector<uint8_t> tr((size_t)orig_len + 64);
  s.next_in = arc.data() + body;
  s.avail_in = arc.size() - body;
  s.next_out = tr.data();
  s.avail_out = tr.size();
  if (lzma_code(&s, LZMA_FINISH) != LZMA_STREAM_END)
    throw std::runtime_error("lzma decode failed");
  tr.resize((size_t)s.total_out);
  lzma_end(&s);

  std::vector<uint8_t> out;
  if (flags & 1) {
    invert(tr, out, marker);
  } else {
    out = std::move(tr);
  }
  if (out.size() != orig_len || XXH3_64bits(out.data(), out.size()) != want)
    throw std::runtime_error("corrupt archive (checksum/length)");
  return out;
}

int decode_dir(const std::string& in_dir, const std::string& out_dir) {
  std::vector<uint8_t> names;
  if (!read_file(in_dir + "/names.bin", names)) return 2;
  std::vector<Record> recs;
  if (!parse_packed(names.data(), names.size(), "HBA1", recs)) return 2;
  for (const Record& r : recs)
    if (!r.payload.empty()) return 2;
  std::vector<Record> out_recs;
  out_recs.reserve(recs.size());
  for (size_t idx = 0; idx < recs.size(); idx++) {
    std::vector<uint8_t> arc;
    if (!read_file(in_dir + "/" + ordinal_name(idx), arc)) return 2;
    Record r;
    r.alias = recs[idx].alias;
    try {
      r.payload = decompress_size(arc);
    } catch (const std::exception& e) {
      fprintf(stderr, "pypack: corrupt archive, object %zu (%s)\n", idx, e.what());
      return 2;
    }
    out_recs.push_back(std::move(r));
  }
  std::vector<Record> name_recs;
  for (const Record& r : out_recs) name_recs.push_back(Record{r.alias, {}});
  std::vector<uint8_t> names_out;
  serialize_packed("HBI1", name_recs, names_out);
  if (!write_file(out_dir + "/names.bin", names_out.data(), names_out.size())) return 2;
  for (size_t idx = 0; idx < out_recs.size(); idx++) {
    const Record& r = out_recs[idx];
    if (!write_file(out_dir + "/" + ordinal_name(idx), r.payload.data(), r.payload.size()))
      return 2;
  }
  return 0;
}

int decode_stream_main() {
  std::vector<uint8_t> in;
  constexpr size_t CH = 1 << 20;
  size_t sz = 0;
  for (;;) {
    in.resize(sz + CH);
    size_t got = fread(in.data() + sz, 1, CH, stdin);
    sz += got;
    if (got < CH) break;
  }
  in.resize(sz);
  std::vector<Record> recs;
  if (!parse_packed(in.data(), in.size(), "HBA1", recs)) return 2;
  std::vector<Record> out_recs;
  for (size_t idx = 0; idx < recs.size(); idx++) {
    Record r;
    r.alias = recs[idx].alias;
    try {
      r.payload = decompress_size(recs[idx].payload);
    } catch (const std::exception& e) {
      fprintf(stderr, "pypack: corrupt archive, object %zu (%s)\n", idx, e.what());
      return 2;
    }
    out_recs.push_back(std::move(r));
  }
  std::vector<uint8_t> buf;
  serialize_packed("HBI1", out_recs, buf);
  if (fwrite(buf.data(), 1, buf.size(), stdout) != buf.size()) return 2;
  return 0;
}

}  // namespace

int main(int argc, char** argv) {
  if (argc == 4 && strcmp(argv[1], "decode-dir") == 0) return decode_dir(argv[2], argv[3]);
  if (argc == 2 && strcmp(argv[1], "decode-stream") == 0) return decode_stream_main();
  fprintf(stderr, "usage: decoder decode-dir IN OUT | decoder decode-stream\n");
  return 64;
}
