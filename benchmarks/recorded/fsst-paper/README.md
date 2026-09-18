# FSST paper selective-row benchmark replay

Hypothesis: Astra's direct-row advantage survives the FSST authors' original
selective-decoding benchmark. Falsifier: a loss to FSST or OnPair+ at any of its
five selectivities. This is a quick additional measurement; the previous native
and WebAssembly results remain unchanged.

The unchanged [filtertest.cpp at e638d4c](https://github.com/cwida/fsst/blob/e638d4cf8c26129d73c242a4127b42b975de5b63/paper/filtertest.cpp)
is included by [wrapper.cpp](../../fsst-paper/wrapper.cpp). The upstream FSST and LZ4 runners, `doTest`, random
selection, sorting, warmup, timer, correctness oracle and aggregation are unchanged.
The added runner loads the already measured Astra and OnPair+ native libraries.
Its row-ID conversion and production of output offsets are inside the timed call;
it does not cache reconstructed output. Accessed 17 September 2026.

All 23 original columns fit below the upstream selective-test 7,000,000-byte
per-file cutoff, so this path uses the full original 39,841,347 bytes. The separate
upstream `compare` mode that duplicates columns to 8 MiB is not run here.

The server uses one pinned CPU (8), the original seed 123 and sorted samples of
1/3/10/30/100 percent of rows. Each column/selectivity has 100 warmup calls and
100 timed calls. The paper's score is the geometric mean of per-column thousands
of rows per second. Three complete replays rotate method order. `DEBUG=1` enables
the authors' byte-exact output comparisons for every column and selectivity.
Initialization is outside the timer. This is a warm selective-row benchmark,
not a fresh-decoder or complete SQL-query benchmark.

Archive sizes use each runner's own format accounting, including indexes and
dictionaries; the original FSST runner also counts its 8,192-byte per-column
padding. Executable and shared-library sizes are separate from this paper metric.
The pinned source version is recorded in [SOURCE_MAP.json](../../SOURCE_MAP.json);
[trials.json](trials.json) retains the measurements. [run.py](../../fsst-paper/run.py) is the portable replay.


## Result

Astra remains ahead of FSST, OnPair+ and LZ4 at every tested selectivity in this
paper-style warm benchmark. Across three replays, Astra's lowest score exceeds
each baseline's highest score at all five selectivities. These are observed
ranges from a quick repeat, not confidence intervals or a universal guarantee.

Throughput is **million rows per second**, taking the median of the three
original geometric-mean benchmark scores. Higher is better.

| Rows selected | FSST | OnPair+ | LZ4 | Astra | Astra / FSST | Astra / OnPair+ |
|---|---:|---:|---:|---:|---:|---:|
| 1% | 60.75 | 86.79 | 1.18 | 99.08 | 1.63x | 1.14x |
| 3% | 57.52 | 77.02 | 3.49 | 101.91 | 1.77x | 1.32x |
| 10% | 56.48 | 74.66 | 10.84 | 97.45 | 1.73x | 1.31x |
| 30% | 54.73 | 76.93 | 27.31 | 93.52 | 1.71x | 1.22x |
| 100% | 51.47 | 70.12 | 60.75 | 100.90 | 1.96x | 1.44x |

| Method | Archive MB (decoder code excluded) |
|---|---:|
| FSST | 24.166 |
| OnPair+ | 22.198 |
| LZ4, 1000-row blocks | 29.904 |
| Astra rows | 17.828 |

Astra's archive is 26.23% smaller than FSST and 19.69% smaller than OnPair+. Its unchanged decoder is another 28,304 bytes. All 1,380 selected-output checks passed. No research API requests or changes to the algorithms were made.
