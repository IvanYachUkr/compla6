#include "encode.hpp"
#include <cstdio>
#include <random>
#include <string>

using sw::Bytes;

template <class F> void rejects(F f) {
  bool rejected = false;
  try { f(); } catch (const std::runtime_error &) { rejected = true; }
  sw::require(rejected, "malformed fixture accepted");
}

void set32(Bytes &b, size_t at, uint32_t value) {
  sw::require(at + 4 <= b.size(), "invalid test offset");
  for (unsigned i = 0; i < 4; ++i) b[at + i] = uint8_t(value >> (8 * i));
}

void outer_header_bounds(const Bytes &archive) {
  for (auto change : {std::pair<size_t, uint32_t>{4, uint32_t(sw::BLOCK + 1)},
                     {8, 1}, {12, 0}, {12, 2}, {17, 0},
                     {21, uint32_t(sw::BLOCK + 6)}}) {
    Bytes bad = archive;
    set32(bad, change.first, change.second);
    rejects([&] { sw::decode(bad); });
  }
  Bytes oversized_header;
  sw::put32(oversized_header, 0x335a4c43);
  sw::put64(oversized_header, sw::BLOCK + 1);
  sw::put32(oversized_header, 1);
  rejects([&] { sw::decode(oversized_header); });
}

void single(const Bytes &input, uint8_t mode) {
  Bytes archive = sw::stripesencode(input);
  sw::require(sw::load32(archive.data()) == 1 && archive[12] == mode,
              "stripe selection changed");
  sw::require(sw::stripesdecode(archive.data(), archive.size(), input.size()) == input,
              "stripe round trip mismatch");
  for (size_t n = 0; n < archive.size(); ++n)
    rejects([&] { sw::stripesdecode(archive.data(), n, input.size()); });
  for (auto change : {std::pair<size_t, uint32_t>{0, 0}, {0, 1026},
                     {4, 0}, {4, uint32_t(input.size() + 1)},
                     {8, uint32_t(archive.size())}}) {
    Bytes bad = archive;
    set32(bad, change.first, change.second);
    rejects([&] { sw::stripesdecode(bad.data(), bad.size(), input.size()); });
  }
  Bytes bad = archive;
  bad[12] = 3;
  rejects([&] { sw::stripesdecode(bad.data(), bad.size(), input.size()); });
  bad = archive;
  bad.push_back(0);
  rejects([&] { sw::stripesdecode(bad.data(), bad.size(), input.size()); });
  rejects([&] { sw::stripesdecode(archive.data(), archive.size(), input.size() + 1); });
}

int main() {
  try {
    std::mt19937 random(72581);
    Bytes uniform(2048), binary(8192), repeated(8192);
    for (auto &c : uniform) c = uint8_t(random());
    for (auto &c : binary) c = uint8_t(random() & 1);
    for (size_t i = 0; i < repeated.size(); ++i) repeated[i] = uint8_t(i % 93 + 32);
    single(uniform, 0);
    single(binary, 2);
    single(repeated, 1);

    std::string rows;
    for (unsigned i = 0; i < 500; ++i)
      rows += "{\"object\":{\"a\":true,\"b\":false},\"list\":\"alpha, beta, alpha\","
              "\"scalar\":17,\"other\":\"" + std::to_string(i) + "\"}\n";
    Bytes structured(rows.begin(), rows.end());
    Bytes prep = columnar::forward(structured);
    sw::require(prep[4] == 2 && sw::column_stripes(prep).size() == 5,
                "structured fixture did not split columns");
    Bytes stripes = sw::stripesencode(prep);
    sw::require(sw::stripesdecode(stripes.data(), stripes.size(), prep.size()) == prep,
                "column stripe concatenation mismatch");
    // Stripe workers write disjoint ranges; scheduling must not affect bytes.
    sw::require(setenv("COMPRESSION_LAB_THREADS", "1", 1) == 0, "setenv failed");
    Bytes serial = sw::encode(structured);
    sw::require(setenv("COMPRESSION_LAB_THREADS", "4", 1) == 0, "setenv failed");
    sw::require(sw::encode(structured) == serial, "worker-dependent archive");
    sw::require(sw::decode(serial) == structured, "parallel decode mismatch");
    outer_header_bounds(serial);

    // Exercise the maximum stripe count and reject inconsistent aggregate
    // lengths before allocating any concatenated transform buffer.
    Bytes many;
    sw::put32(many, 1025);
    for (uint32_t i = 0; i < 1025; ++i) {
      sw::put32(many, 1);
      sw::put32(many, 1);
      many.push_back(0);
      many.push_back(uint8_t(i));
    }
    Bytes expected(1025);
    for (size_t i = 0; i < expected.size(); ++i) expected[i] = uint8_t(i);
    sw::require(sw::stripesdecode(many.data(), many.size(), expected.size()) == expected,
                "maximum stripe count mismatch");
    rejects([&] { sw::stripesdecode(many.data(), many.size(), sw::BLOCK); });
    set32(many, 4, uint32_t(sw::BLOCK + 1));
    rejects([&] { sw::stripesdecode(many.data(), many.size(), sw::BLOCK); });
    for (Bytes input : {Bytes{}, Bytes{0}, uniform, binary, repeated, structured}) {
      Bytes archive = sw::encode(input);
      sw::require(sw::decode(archive) == input, "codec round trip mismatch");
      sw::require(sw::encode(input) == archive, "nondeterministic encoding");
      for (size_t at = 0; at < archive.size(); ++at) {
        Bytes bad = archive;
        bad[at] ^= uint8_t(1u << (at & 7));
        try {
          sw::require(sw::decode(bad) == input, "corruption silently changed output");
        } catch (const std::runtime_error &error) {
          if (std::string(error.what()) == "corruption silently changed output") throw;
        }
      }
    }
    std::puts("globalstripe focused checks passed: stripe modes, global bounds, worker determinism, corruption");
    return 0;
  } catch (const std::exception &error) {
    std::fprintf(stderr, "check: %s\n", error.what());
    return 1;
  }
}
