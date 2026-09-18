# Python and Yelp: native RAM-to-RAM server comparison

Complete original datasets, server CPU core 8, median of three passes. Inputs and archive buffers are resident before timing. Fresh codec setup, preprocessing, archive metadata, integrity checks and internal allocations are timed. Process startup, file I/O, Lab transport copies, external correctness checks and disposal of returned output buffers are excluded. Each call reconstructs the complete original bytes. No fitted dictionaries or initialized decoder contexts are reused.

This uses the same native in-memory principle as DBText, with three passes for a quick replay rather than seven after a warmup. Each Python/Yelp corpus is one complete object, retaining each algorithm’s original internal chunking. There is no row-access workload here. Original fixed worker counts are preserved and all workers share the one pinned CPU.

All 51 displayed full-corpus roundtrips passed. Every model archive also matches its frozen CLI payload byte for byte. Model archive sizes retain the 34-byte Lab envelope; all framing, online dictionaries and indexes are included. Decoder executables are excluded consistently. MB and MB/s are decimal.

**Yelp — original 100,000,488 bytes**

| Method | Archive MB | Compress MB/s | Decompress MB/s |
|---|---:|---:|---:|
| LZ4-1 | 30.131 | 414.8 | 980.5 |
| Zstd-3 | 17.915 | 292.0 | 864.6 |
| Zstd-19 | 9.440 | 1.6 | 1127.5 |
| Astra Ultra — compact fast | 8.603 | 160.5 | 483.5 |
| GLM — columnizer + Zstd | 7.318 | 116.5 | 349.3 |
| Astra Ultra — higher compression | 8.035 | 97.7 | 559.3 |
| Sol plan → Terra — dense | 6.781 | 14.2 | 133.2 |
| GLM new — structured | 7.862 | 342.7 | 518.4 |

**Python source — original 99,999,986 bytes**

| Method | Archive MB | Compress MB/s | Decompress MB/s |
|---|---:|---:|---:|
| LZ4-1 | 46.483 | 340.2 | 955.2 |
| Zstd-3 | 28.367 | 172.1 | 725.2 |
| Zstd-19 | 20.230 | 2.0 | 738.2 |
| GLM — from scratch, fast | 27.732 | 57.3 | 175.6 |
| GLM — tuned Zstd | 24.082 | 46.2 | 506.2 |
| Terra MAX — from scratch, dense | 21.836 | 2.5 | 80.6 |
| GLM new — fast mode | 36.794 | 41.0 | 570.9 |
| GLM new — size mode† | 17.780 | 1.1 | 80.2 |
| GLM new — Zstd mode† | 18.111 | 1.2 | 238.5 |

† Provisional qualification is inherited: the newer Python size mode failed a separate local streaming gate; the newer Python Zstd mode did not complete the full local Lab checks. Both passed the prior three server corpus checks and the new RAM checks shown here. The older from-scratch Python GLM retains its recorded malformed-archive concern.

Adapters call the original codec functions; no compression mechanisms were changed. Build flags and separately compiled decoder implementations are retained. The adapters are shared libraries loaded before timing, and their builds and hashes are saved. Baselines use LZ4F and Zstd native APIs with complete checksummed frames. Native baseline frame generation can differ from CLI framing; Zstd receives the known whole-input size. The [protocol guide](../../README.md) and [input hashes](../../whole/inputs.json) describe this replay.

The earlier end-to-end CLI results are retained in the research workspace. This is a separate timing protocol, not a replacement of those measurements.
