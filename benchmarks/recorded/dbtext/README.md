GPT-6 Astra Ultra — final dbtext comparison  
Original: 39,841,347 bytes (39.841 MB), 23 original columns, 1,492,936 rows.  
Native server: one CPU/thread, RAM-to-RAM, median of 7 corpus passes after a warmup.  
One complete column per block; no duplicated input. All speeds below are MB/s.  
Fresh decode includes decoder/dictionary setup; warm reuses the initialized decoder.  
All full and selected-row reconstruction checks passed.  

**Bulk compression**

| Method | Payload MB | Decoder kB | Package MB | Decode fresh | Decode warm | Compress |
|---|---:|---:|---:|---:|---:|---:|
| LZ4 | 24.607 | 16.496 | 24.623 | 2952 | 3108 | 505.7 |
| Zstd1 | 15.383 | 16.536 | 15.400 | 1084 | 1202 | 319.9 |
| Astra fast decode | 15.134 | 24.328 | 15.158 | 4136 | 4361 | 22.6 |
| Astra fast encode | 14.996 | 24.304 | 15.020 | 1400 | 1526 | 294.5 |

**With individual-row access**

| Method | Payload MB | Decoder kB | Package MB | Decode fresh | Decode warm | Compress |
|---|---:|---:|---:|---:|---:|---:|
| FSST | 24.941 | 16.672 | 24.958 | 2815 | 3037 | 173.1 |
| OnPair+ | 22.198 | 16.784 | 22.215 | 1524 | 4880 | 38.2 |
| Astra row access | 17.828 | 28.304 | 17.856 | 4333 | 4699 | 43.9 |

**Selected-row reconstruction: fresh / warm milliseconds (lower is better)**

| Rows selected | FSST | OnPair+ | Astra |
|---|---:|---:|---:|
| 1% | 1.353 / 0.700 | 18.670 / 0.530 | 0.562 / 0.445 |
| 3% | 2.174 / 1.486 | 19.263 / 1.093 | 1.048 / 0.870 |
| 10% | 4.603 / 3.768 | 20.463 / 2.577 | 2.524 / 2.283 |
| 30% | 11.605 / 10.363 | 24.826 / 6.864 | 6.662 / 6.293 |
| 100% | 34.482 / 33.093 | 36.571 / 21.273 | 17.121 / 16.367 |

Native package = payload (including dictionaries/indexes) + complete custom decoder; separately pinned conventional codec libraries are assumed installed and excluded. MB/kB are decimal. Strict totals are retained in native-results.csv.
The bulk baseline rows are from the fast-decode comparison. Baselines were rerun for fast-encode: LZ4 compress/fresh/warm = 495.6/2955/3034 MB/s; Zstd1 = 319.2/1098/1207 MB/s, with identical sizes.
Row subsets are fixed nested random samples, submitted in sorted row order. These measure selected-row reconstruction throughput, not random-order point lookup or SQL queries.
Results are corpus aggregates; individual columns can lose. Astra bulk trades compression speed for decoding speed; OnPair+ still slightly wins warm full-column decoding.

