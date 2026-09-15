# Morning primary-source handoff: mature codecs and adjacent design

**Access date:** 2026-09-07  
**Scope:** read-only source scout for a lossless, side-information-aware adaptive
compression benchmark. Release facts below are sourced from the project or
maintainer pages linked in each row. A local environment's installed version is
not inferred from this document.

## Release and API inventory

| Library | Pin and release evidence | Checksums / source location | License | Multithread semantics relevant to the benchmark |
|---|---|---|---|---|
| **Zstandard** | **v1.5.7**, published 2025-02-19, commit `f8745da`. [Official releases](https://github.com/facebook/zstd/releases); [v1.5.7 assets](https://github.com/facebook/zstd/releases/expanded_assets/v1.5.7). | Release assets include `zstd-1.5.7.tar.gz.sha256` and a signature. An independently maintained Meta source manifest records archive SHA-256 `eb33e51f49a15e023950cd7825ca74a4a2b43db8354825ac24fc1b7ee09e6fa3`: [manifest](https://github.com/facebook/proxygen/blob/main/build/fbcode_builder/manifests/zstd). | [BSD or GPLv2](https://github.com/facebook/zstd/blob/dev/LICENSE). | The CLI defaults to multiple workers (capped at four), configurable with `-T#` or `ZSTD_NBTHREADS`. For the library, the dynamic library defaults to multithread capability since v1.5.0 while a static build is single-thread by default; set `ZSTD_c_nbWorkers` and check the return code. Build targets include `lib-mt` and `lib-nomt`; POSIX builds need `-pthread`. [Library README](https://github.com/facebook/zstd/blob/dev/lib/README.md). |
| **LZ4** | **v1.10.0**, released 2024-07-22 (tag date 2024-07-21), commit `ebb370c`. [Official releases](https://github.com/lz4/lz4/releases); [v1.10.0 assets](https://github.com/lz4/lz4/releases/expanded_assets/v1.10.0). | Release assets include `lz4-1.10.0.tar.gz.sha256`. The published archive hash is `537512904744b35e232912055ccf8ec66d768639ff3abe5788d90d792ec5f48b`; verify against the release checksum asset before freezing. | The liblz4 portion is [BSD-2-Clause](https://github.com/lz4/lz4/blob/dev/lib/LICENSE); CLI/tests use GPL-2.0-or-later. | v1.10.0 is the “Multicores edition”: the CLI supports multithreaded compression and `LZ4_NBWORKERS`. The frame API does not expose a zstd-style single-stream worker setting; parallelize independent frames/contexts. A `LZ4_CDict` can be shared concurrently across threads ([frame API manual](https://github.com/lz4/lz4/blob/dev/doc/lz4frame_manual.html)). Charge each frame/header and any dictionary in total size. |
| **Brotli** | **v1.2.0**, released 2025-10-27, commit `028fb5a`. [Official release](https://github.com/google/brotli/releases/tag/v1.2.0); [assets](https://github.com/google/brotli/releases/expanded_assets/v1.2.0). | GitHub's binary assets publish SHA-256 values (for example, dynamic x64 `be6954ca080d94af1562ff84e978f9583cfd8acdfedafc3ea372a99e7d20ff69` and static x64 `3208ab82d4f08e062a25660cf38092db4e8415792aa1e579956d37740f8d8924`). The source auto-tarball has no adjacent GitHub checksum asset; do not substitute a mirror hash for the source pin. | [MIT](https://github.com/google/brotli/blob/master/LICENSE). | The public C API exposes independent encoder/decoder states and streaming, but no documented native multithread worker API. Use independent jobs/streams for parallelism and record the framing strategy. The stream format has no checksum or uncompressed length by default ([README](https://github.com/google/brotli/blob/master/README.md)), so the experiment's container must provide integrity/length metadata if needed. Avoid enabling the optional base64 mode until the upstream stack-overflow report is resolved: [issue #1509](https://github.com/google/brotli/issues/1509). |
| **XZ Utils / liblzma** | **v5.8.3**, released 2026-03-31. [Official site](https://tukaani.org/xz/); [official release](https://github.com/tukaani-project/xz/releases/tag/v5.8.3). | Official release asset `xz-5.8.3.tar.gz` has SHA-256 `3d3a1b973af218114f4f889bbaa2f4c037deaae0c8e815eec381c3d546b974a0`. | liblzma is [0BSD](https://github.com/tukaani-project/xz/blob/master/COPYING); the complete distribution contains additional LGPL/GPL-covered tools and scripts. | `lzma_stream_encoder_mt` accepts an `lzma_mt` configuration with worker count, block size, preset, and check; threaded decoding is available through `lzma_stream_decoder_mt`. Threaded operation can be bursty and memory-heavy (roughly three times block size per worker); configure and report memory limits. [Container API](https://www.tukaani.org/xz/liblzma-api/container_8h.html). v5.8.3 is preferred over 5.8.2 and older versions because the official site lists a minor security fix; do not carry forward old threaded-decoder builds without checking the relevant advisories. |
| **zlib** | **v1.3.2**, released 2026-02-17. [Official site](https://www.zlib.net/). | Official hashes: tar.gz `bb329a0a2cd0274d05519d61c667c062e06990d72e125ee2dfa8de64f0119d16`; tar.xz `d7a0654783a4da529d1bb793b7ad9c3318020af77667bcae35f95d0e42a792f3`; zip `e8bf55f3017aa181690990cb58a994e77885da140609fc8f94abe9b65d2cae28`. | [zlib License](https://www.zlib.net/zlib_license.html). | zlib is thread-safe when its dependent routines and allocators are thread-safe, but one stream must be used by one thread at a time. There is no built-in multicore stream API; use independent streams/jobs and account for one wrapper/stream per job. [FAQ](https://zlib.net/zlib_faq.html). |
| **bzip2** | **v1.0.8**, current stable release 2019-07-13. [Official downloads](https://sourceware.org/bzip2/downloads.html); [manual](https://sourceware.org/bzip2/manual/manual.html). | Source: [bzip2-1.0.8.tar.gz](https://sourceware.org/pub/bzip2/bzip2-1.0.8.tar.gz). Official SHA-512 in [sha512.sum](https://sourceware.org/pub/bzip2/sha512.sum): `083f5e675d73f3233c7930ebe20425a533feedeaaa9d8cc86831312a6581cefbe6ed0d08d2fa89be81082f2a5abdabca8b3c080bf97218a1bd59dc118a30b9f3`. | BSD-style license with attribution and a requirement to mark altered source; see the official [manual license text](https://sourceware.org/bzip2/manual/manual.html). | The high- and low-level interfaces have no global state and are thread-safe when separate stream objects are used. There is no built-in multithread API; parallelize separate streams/tasks. bzip2 has CRC self-checking, but framing and any container metadata remain part of total size. |

### Pinning interpretation

“Latest” here means the latest published release found on the official release
page on the access date. A development branch or changelog entry is not treated
as a release (for example, the zstd development changelog mentions unreleased
v1.6.0). Before a benchmark freeze, download from the linked official location,
verify the linked checksum/signature, record the exact archive bytes and build
flags, and preserve the result in the machine-readable benchmark metadata.

The installed versions observed in prior handover records are leads only:
zstd 1.5.7, LZ4 1.10.0, Brotli 1.1.0, and XZ 5.8.1. This scout did not claim a
local executable or ABI verification.

## AnyBlox: portable self-decoding data sets

Primary paper: Mateusz Gienieczko, Maximilian Kuschewski, Thomas Neumann,
Viktor Leis, and Jana Giceva, “AnyBlox: A Framework for Self-Decoding
Datasets,” *PVLDB* 18(11), pp. 4017–4031 (2025), DOI
[10.14778/3749646.3749672](https://doi.org/10.14778/3749646.3749672), [paper PDF](https://www.vldb.org/pvldb/vol18/p4017-gienieczko.pdf),
[artifact repository](https://github.com/AnyBlox/vldb-2025).

The system stores encoded data together with a portable WebAssembly decoder.
Dataset metadata includes schema, row count, estimated decoded size, and batch
information; an optional decoder URI and cryptographic checksum identify a
decoder fetched from outside the file. The decoder API accepts a batch/range
and projection and maintains decoder state. The host can split batches across
threads. Results are Arrow data, giving a practical boundary between a codec
and a query engine.

The paper's 32-core Intel Xeon Gold 6430 evaluation reports, for the full
TPC-H scale-factor-20 workload, 32-thread throughput of **978.17 million
tuples/s** for the native table, **593.18 million tuples/s** for AnyBlox
(Vortex), and **396.33 million tuples/s** for Parquet (Snappy). The table's
32-thread/1-thread speedups are respectively **20.65x**, **17.62x**, and
**24.06x**. These are paper-specific observations, not MB/s measurements and
not a claim about the current repository's codecs or hardware.

For this project, AnyBlox is architecture inspiration only. A self-decoding
container would make the decoder, URI/checksum, schema, route, dictionary, and
all framing bytes explicit side information. The paper also reports material
constraints to carry into failure analysis: standalone operation maps the whole
dataset into memory, predicate pushdown is absent, files are limited to 4 GB,
and WebAssembly adds runtime and security considerations. A compression result
must therefore charge bundled decoder bytes or an amortization rule and must
not compare a route payload alone with a conventional codec.

## Official GPT-5.6 prompting guidance for the parent harness

Canonical guidance: [Model guidance — GPT-5.6](https://developers.openai.com/api/docs/guides/model-guidance?model=gpt-5.6), with the [Builders Guide to GPT-5.6](https://openai.com/index/builders-guide-to-gpt-5-6/) dated 2026-08-13.

The current guidance recommends lean prompts, stating each instruction once,
exposing only relevant tools, tracking context, defining autonomy and approval
boundaries, and keeping concise outputs complete with required facts,
decisions, caveats, and next steps. Remove repeated instructions or tools one
group at a time and rerun the same evaluations. Set reasoning effort
intentionally: measure `xhigh` versus `max` on the target task and reserve
`max` for quality-first cases where the gain is real. The `gpt-5.6` alias routes
to `gpt-5.6-sol`; the parent should preserve the explicit Sol target required by
the harness rather than switching to Astra.

Applied to this research sprint, the useful prompt shape is: outcome first;
then repository/context and hard accounting constraints; then the evidence
format and completion checks. Require exact round trips, held-out routing,
side-information accounting, primary-source citations, and concise machine
readable outputs. Ask the agent to freeze a design/spec before test evaluation,
and compare reasoning settings on the same representative task instead of
assuming a larger setting helps.

## Implications for the next experiment

1. Treat zstd as the primary external baseline, with explicit `nbWorkers`
   measurements; include LZ4, Brotli, XZ/liblzma, zlib, and bzip2 only with
   exact build/version records.
2. Define a common independent-job protocol for libraries without a native
   multithread stream API. The protocol must specify frame boundaries, route
   identifiers, checksums/lengths, dictionary handling, and merge order.
3. Charge container bytes, dictionaries, decoder descriptions, and any custom
   code under the stated amortization rule. Keep AnyBlox-like bundled decoders
   as a separate architecture experiment.
4. Report throughput and memory alongside bits per byte, compression ratio,
   and exact round-trip status. Keep oracle routes separate from deployable
   feature routers.
