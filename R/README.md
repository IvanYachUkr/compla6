| Yelp experiment · modes in order | Original MB | Archive MB | Custom decoder kB | Package MB | Encode MB/s | Decode MB/s | Notes |
|---|---:|---:|---:|---:|---:|---:|---|
| LZ4-1 · server · speed | 100.00049 | 30.131 | 0.00 | 30.131 | 363.79 | 697.03 | Server; one core; stock executable excluded. |
| LZ4-1 · laptop · speed | 100.00049 | 30.131 | 0.00 | 30.131 | 497.95 | 813.65 | Laptop; one core; v1.9.4; 7 exact round trips; stock executable excluded. |
| Zstd-3 · server · balance | 100.00049 | 17.985 | 0.00 | 17.985 | 215.91 | 633.34 | Server; one core; stock executable excluded. |
| Zstd-3 · laptop · balance | 100.00049 | 17.985 | 0.00 | 17.985 | 238.88 | 685.23 | Laptop; one core; v1.5.5; 7 exact round trips; stock executable excluded. |
| Zstd-19 · server · size | 100.00049 | 9.439 | 0.00 | 9.439 | 1.48 | 811.90 | Server; one core; stock executable excluded. |
| Zstd-19 · laptop · size | 100.00049 | 9.439 | 0.00 | 9.439 | 1.38 | 603.31 | Laptop; one core; v1.5.5; 7 exact round trips; stock executable excluded. |
| Luna MAX · early Prime | 100.00049 | 10.961 | 124.37 | 11.721 | 30.53 | 131.56 | Lossless bug: ordinary whitespace/newlines and nested values changed. |
| Sol MAX · early checkpoint | 100.00049 | 10.477 | 89.86 | 10.567 | 9.01 | 83.67 | Lossless bug: wrong-input replay; interrupted run. |
| Sol MAX · Prime (selected / dense) | 100.00049 | 8.045 / 7.162 | 59.70 / 63.80 | 8.105 / 7.226 | 127.81 / 14.45 | 150.00 / 66.00 | Edge round-trip bugs: coordinates/attribute counts. |
| GLM MAX · scratch (fast / dense) | 100.00049 | 19.421 / 13.971 | 21.72 / 47.94 | 19.443 / 14.020 | 116.38 / 49.74 | 308.54 / 109.53 | No defect found in saved review. |
| Luna MAX · Prime (fast / dense) | 100.00049 | 64.029 / 13.591 | 55.91 / 55.91 | 64.085 / 13.647 | 136.62 / 4.86 | 55.19 / 118.36 | Safety bug: malformed Huffman model causes out-of-bounds access. |
| Terra MAX · Prime (fast / dense) | 100.00049 | 54.908 / 6.822 | 90.21 / 123.20 | 54.999 / 6.946 | 196.38 / 42.23 | 239.83 / 44.49 | Edge round-trip bugs; dense also has undefined C++ evaluation order. |
| Luna MAX · Codex (fast / dense) | 100.00049 | 79.552 / 11.983 | 108.36 / 122.03 | 79.660 / 12.105 | 129.17 / 46.98 | 139.61 / 71.90 | Fast: no defect found. Dense: name/business-ID failures. |
| Terra MAX · Codex (fast / dense) | 100.00049 | 18.217 / 7.321 | 66.65 / 146.90 | 18.283 / 7.468 | 108.94 / 7.55 | 306.51 / 169.99 | Round-trip/input bugs: long matches, dictionary lengths, stars. |
| Astra ULTRA + agents (compact / smaller) | 100.00049 | 8.603 / 8.035 | 80.70 / 72.12 | 8.684 / 8.107 | 281.73 / 114.44 | 383.73 / 345.79 | No defect found; both shown modes clear 100 MB/s. |
| Sol MAX + ≤3 Luna (compact / dense) | 100.00049 | 10.900 / 10.110 | 55.62 / 55.62 | 10.956 / 10.166 | 110.25 / 24.87 | 108.81 / 110.05 | Edge round-trip bugs in both schema modes; generic fastest mode is separate. |
| Sol plan → Luna (fast / dense) | 100.00049 | 36.268 / 14.033 | 54.58 / 86.48 | 36.323 / 14.120 | 139.79 / 46.38 | 115.22 / 117.90 | Fast: no defect found. Dense: trailing category lost. Plan partly explored. |
| Sol plan → Terra (fast / dense) | 100.00049 | 15.003 / 6.781 | 84.19 / 112.86 | 15.087 / 6.894 | 103.40 / 22.74 | 115.63 / 121.18 | Fast: 8 KiB input produces undecodable archive. Dense: limited owner review. |
| Astra MAX + 1 Luna (compact / maximum) | 100.00049 | 10.503 / 8.281 | 117.05 / 117.05 | 10.621 / 8.398 | 112.53 / 11.93 | 334.64 / 66.93 | Recorded gates pass; no actionable defect found in bounded review. |
| GPT-6 Pro · cloud | 100.00049 | 5.645 | 92.84 | 5.738 | 0.65 | 29.86 | Corrupt input: partial output before rejection. Includes preparation time. |
| Earlier GLM + Zstd · 9–10 Sep | 100.00049 | 7.318 | 69.70 | 7.389 | 138.81 | 241.78 | Not from scratch; existing Zstd component excluded. |
| New GLM MAX · Lab 0.6.4 (fast / dense) | 100.00049 | 15.741 / 10.448 | 51.43 / 51.43 | 15.792 / 10.499 | 109.81 / 1.26 | 140.40 / 163.07 | Both quality gates pass; no codec defect found in bounded review. Dense below speed floor. Stock codecs measured during research; memory interruption recovered; handoff arithmetic corrected. |
