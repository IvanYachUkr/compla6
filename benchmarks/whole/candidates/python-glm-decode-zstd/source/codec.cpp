// pypack-bal encoder: indent transform + zstd level 22 with a 128 MiB window.
//
// Same reversible indentation transform as pypack-size, then libzstd (pinned
// standard codec) at compression level 22 with windowLog=27, so the matcher
// sees the whole 100 MB corpus at once.
// Archive payload: "PZ" + fmt(3) + flags(bit0: transform applied) + u64 LE
// original length + marker byte + zstd frame.
#include "format.h"
#include <zstd.h>
#include <cstring>

using namespace pylz;

namespace {

static bool transform(const std::vector<uint8_t>& src, std::vector<uint8_t>& dst,
                      uint8_t& marker) {
  size_t cnt[256] = {0};
  for (size_t i = 0; i < src.size(); ++i) cnt[src[i]]++;
  marker = 0;
  while (marker < 255 && cnt[marker]) marker++;
  if (cnt[marker]) return false;
  dst.clear();
  dst.reserve(src.size() + src.size() / 16 + 16);
  size_t i = 0, n = src.size();
  bool bol = true;
  while (i < n) {
    uint8_t c = src[i];
    if (bol && c == ' ') {
      size_t j = i;
      while (j < n && src[j] == ' ') j++;
      size_t run = j - i;
      if (run >= 2) {
        for (size_t k = 0; k < run; k += 256) {
          size_t take = run - k > 256 ? 256 : run - k;
          dst.push_back(marker);
          dst.push_back((uint8_t)(take - 1));
        }
        i = j;
        bol = false;
        continue;
      }
      dst.push_back(c);
      bol = false;
      i++;
      continue;
    }
    dst.push_back(c);
    bol = (c == '\n');
    i++;
  }
  return true;
}

std::vector<uint8_t> compress_bal(const std::vector<uint8_t>& in) {
  std::vector<uint8_t> out;
  std::vector<uint8_t> tr;
  uint8_t marker = 0;
  bool ok = transform(in, tr, marker);
  const std::vector<uint8_t>& data = ok ? tr : in;

  ZSTD_CCtx* c = ZSTD_createCCtx();
  ZSTD_CCtx_setParameter(c, ZSTD_c_compressionLevel, 22);
  ZSTD_CCtx_setParameter(c, ZSTD_c_windowLog, 27);
  ZSTD_CCtx_setParameter(c, ZSTD_c_checksumFlag, 1);
  std::vector<uint8_t> body(ZSTD_compressBound(data.size()));
  size_t cs = ZSTD_compress2(c, body.data(), body.size(), data.data(), data.size());
  ZSTD_freeCCtx(c);
  if (ZSTD_isError(cs)) {
    fprintf(stderr, "pypack: zstd encode failed: %s\n", ZSTD_getErrorName(cs));
    exit(2);
  }

  out.resize(HEADER_SIZE + 1 + cs);
  put_header(out.data(), 3, ok ? 1 : 0, in.size(), XXH3_64bits(in.data(), in.size()));
  out[HEADER_SIZE] = marker;
  memcpy(out.data() + HEADER_SIZE + 1, body.data(), cs);
  return out;
}

int encode_dir(const std::string& in_dir, const std::string& out_dir) {
  std::vector<uint8_t> names;
  if (!read_file(in_dir + "/names.bin", names)) return 2;
  std::vector<Record> recs;
  if (!parse_packed(names.data(), names.size(), "HBI1", recs)) return 2;
  std::vector<Record> out_recs;
  out_recs.reserve(recs.size());
  for (size_t idx = 0; idx < recs.size(); idx++) {
    std::vector<uint8_t> data;
    if (!read_file(in_dir + "/" + ordinal_name(idx), data)) return 2;
    Record r;
    r.alias = recs[idx].alias;
    r.payload = compress_bal(data);
    data.clear();
    data.shrink_to_fit();
    out_recs.push_back(std::move(r));
  }
  std::vector<Record> name_recs;
  for (const Record& r : out_recs) name_recs.push_back(Record{r.alias, {}});
  std::vector<uint8_t> names_out;
  serialize_packed("HBA1", name_recs, names_out);
  if (!write_file(out_dir + "/names.bin", names_out.data(), names_out.size())) return 2;
  for (size_t idx = 0; idx < out_recs.size(); idx++) {
    const Record& r = out_recs[idx];
    if (!write_file(out_dir + "/" + ordinal_name(idx), r.payload.data(), r.payload.size()))
      return 2;
  }
  return 0;
}

int encode_stream() {
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
  if (!parse_packed(in.data(), in.size(), "HBI1", recs)) return 2;
  std::vector<Record> out_recs;
  for (size_t idx = 0; idx < recs.size(); idx++) {
    Record r;
    r.alias = recs[idx].alias;
    r.payload = compress_bal(recs[idx].payload);
    out_recs.push_back(std::move(r));
  }
  std::vector<uint8_t> buf;
  serialize_packed("HBA1", out_recs, buf);
  if (fwrite(buf.data(), 1, buf.size(), stdout) != buf.size()) return 2;
  return 0;
}

}  // namespace

int main(int argc, char** argv) {
  if (argc == 4 && strcmp(argv[1], "encode-dir") == 0) return encode_dir(argv[2], argv[3]);
  if (argc == 2 && strcmp(argv[1], "encode-stream") == 0) return encode_stream();
  fprintf(stderr, "usage: codec encode-dir IN OUT | codec encode-stream\n");
  return 64;
}
