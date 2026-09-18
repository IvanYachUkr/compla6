# Benchmark review

These are the measurement kernels behind the September 17–18 DBText, Python and
Yelp tables. The small Python entry points replace machine-specific paths with
arguments. C++ timers, workload selection and codec mechanisms are retained.
Recorded server results are separate from newly generated runs.

## The two DBText comparisons

These are separate code paths. For the review, choose the table first:

| Table we shared | Our code to inspect | Reading guide |
|---|---|---|
| **First: our native DBText benchmark** (MB/s, fresh/warm decoding and selected-row milliseconds) | [dbtext/benchmark.cpp](dbtext/benchmark.cpp): 90-line timer; [strings.py](../src/compression_lab/strings.py): selection, correctness and aggregation | [Original benchmark](dbtext/README.md) |
| **Later: the FSST paper benchmark replay** (million rows/s at 1/3/10/30/100%) | [fsst-paper/wrapper.cpp](fsst-paper/wrapper.cpp): our 83-line wrapper | [Paper wrapper](fsst-paper/README.md) |

The authors' unmodified `filtertest.cpp` lives separately under `upstream/fsst/`.
The paper replay does not call our original `benchmark.cpp` or `strings.py`.

## Start here

| Question | Code to read |
|---|---|
| Where do DBText timers start and stop? | [dbtext/benchmark.cpp](dbtext/benchmark.cpp), about 100 lines |
| How are columns, selected rows and exactness checked? | [strings.py](../src/compression_lab/strings.py): `rows`, `selections`, `evaluate` |
| How are column times aggregated? | [strings.py](../src/compression_lab/strings.py): `evaluate`, near `corpus=[]` |
| What do the baseline wrappers add? | [dbtext/reference.cpp](dbtext/reference.cpp), [dbtext/onpair.cpp](dbtext/onpair.cpp) |
| What was taken from the FSST paper? | Unmodified [upstream/fsst/paper/filtertest.cpp](upstream/fsst/paper/filtertest.cpp) |
| How do Astra/OnPair+ enter that benchmark? | [fsst-paper/wrapper.cpp](fsst-paper/wrapper.cpp) |
| What is timed for Python/Yelp? | [whole/ram_bench.cpp](whole/ram_bench.cpp), about 80 lines |
| Which native baseline settings were used there? | [whole/baseline.cpp](whole/baseline.cpp) |
| Where are model implementations and small RAM adapters? | [DBText candidates](dbtext/candidates), [Python/Yelp candidates](whole/candidates) |
| What does the optional WASM driver time? | [wasm/driver.mjs](wasm/driver.mjs) |
| What identifies a new run's data, machine and build? | [benchmark_run.py](../src/compression_lab/benchmark_run.py) |
| How are tables and Pareto frontiers generated? | [reporting.py](../src/compression_lab/reporting.py), [reporting guide](../docs/REPORTING.md) |

## The three protocols

| | DBText native | FSST paper selective replay | Python/Yelp native |
|---|---|---|---|
| Input | 23 original columns, 39,841,347 bytes | Same original columns | One complete ≈100 MB corpus |
| Blocking | One original column; no duplication | Authors' selective path; LZ4 uses 1,000-row blocks | Original codec's internal blocking |
| Trials | 1 discarded pass + 7 measured | 100 warmup + 100 calls per column/selectivity; 3 replays | 3 measured passes; no explicit warmup |
| Decode setup | Fresh = open + reconstruct; warm separately | Decoder already initialized | Fresh setup inside each codec call |
| Aggregation | Sum column times within each pass, then median | Geometric mean of per-column row throughput, then median of replays | Median whole-corpus time |
| Row workload | Nested random subsets, sorted IDs, seed derived from column hash | Original seed 123 and original selection/sorting | No row-access workload |
| Output buffers | Common output allocated before timer; codec internal allocations timed | Authors' target allocation outside repeated timer | Codec internal allocations timed |

All recorded comparisons use one pinned server CPU core. Input is resident before
the C++ timer starts. Reconstruction must produce the exact original bytes; an
external byte comparison is outside the timed section. No decoded-output cache is
introduced by our adapters. DBText selected-row checks also verify returned offsets.
The FSST paper path uses `DEBUG=1` to enable its byte checks. Its separate
`compare` mode that duplicates data to 8 MiB is **not used**.

DBText package size = archive (including dictionaries and indexes) + custom decoder.
Separately identified conventional codec libraries are excluded from that primary
total; strict deployment totals remain in the records. Python/Yelp plots use
archive bytes, exclude decoder code for every method, and retain the 34-byte Lab
envelope for model results. All MB are decimal. Paper-format archive sizes use
the authors' own framing/padding, so they differ from the native DBText wrappers.

## Run on Linux x86-64

Use Python 3.11+, GCC 13+, CMake, and development packages for LZ4, Zstd, LZMA and
Boost 1.83+. On Ubuntu 24.04:

```sh
sudo apt install build-essential cmake liblz4-dev libzstd-dev liblzma-dev libboost-all-dev libxxhash0
python3 benchmarks/upstream/fetch.py
python3 benchmarks/dbtext/build.py --out benchmarks/_build/dbtext
python3 benchmarks/dbtext/run.py --data-dir /path/to/dbtext \
  --build-dir benchmarks/_build/dbtext --out benchmarks/_runs/dbtext --cpu 8
python3 benchmarks/fsst-paper/run.py --data-dir /path/to/dbtext \
  --build-dir benchmarks/_build/dbtext --out benchmarks/_runs/paper --cpu 8
python3 benchmarks/whole/run.py --dataset yelp --input /path/to/yelp-business.jsonl \
  --out benchmarks/_runs/yelp --cpu 8
python3 benchmarks/whole/run.py --dataset python --input /path/to/python-source.py \
  --out benchmarks/_runs/python --cpu 8
```

Choose an available CPU on your machine; omitting `--cpu` selects the first allowed
one. Run comparisons serially on a quiet machine. Each output directory must be
new. Data is supplied separately and checked against [DBText](dbtext/columns.json)
or [Python/Yelp](whole/inputs.json) hashes. `--smoke` permits tiny inputs for wiring
checks and explicitly marks the output as a smoke run. It is not a performance result.

New runs write `run-metadata.json` alongside measurements, with input and build
hashes, machine identity and timing parameters. Runners share a host measurement
lock with native Lab evaluation. The lock serializes these tools; choose a quiet
machine for measurements because it does not control unrelated processes.

For a new dataset, use [native workspace setup](../docs/NATIVE_STRINGS.md).
It supports LF-separated columns, NUL-separated queries and bulk byte sequences
without maintaining a dataset-specific copy of the evaluator.

The Python/Yelp runner supports `--only METHOD_ID ...` and `--build-only`; IDs and
original compiler flags are in [methods.json](whole/methods.json). DBText selection
uses `--methods`. Build logs retain exact commands. Rebuilding uses your installed
standard libraries (including the static Zstd archive where originally used);
compiler/library versions and hardware can change speeds and package sizes.
The historical runs used GCC 13.3, LZ4 1.9.4 and Zstd 1.5.5.

## Results and provenance

Generate a table and optional plots without repeating measurements:

```sh
compression-lab-report benchmarks/_runs/yelp/summary.json \
  --out benchmarks/_runs/yelp-report --plots
```

The [reporting guide](../docs/REPORTING.md) lists input formats and comparison
boundaries. Different machines, protocols, decoder states and size conventions
receive separate panels.

- [DBText native tables](recorded/dbtext/README.md), [CSV](recorded/dbtext/native-results.csv)
- [FSST paper replay](recorded/fsst-paper/README.md), [trial measurements](recorded/fsst-paper/trials.json)
- [Python/Yelp RAM tables](recorded/whole/README.md), [CSV](recorded/whole/comparison.csv), [trial measurements](recorded/whole/trials.json)
- [Yelp plot](recorded/whole/yelp-pareto.png), [Python plot](recorded/whole/python-pareto.png), [PDF](recorded/whole/yelp-python-pareto.pdf)
- [Upstream attribution and pinned versions](upstream/README.md)
- [Source provenance](SOURCE_MAP.json)

The recorded files are compact extracts of retained server evidence, not a claim
that a new local build repeats the exact timings. The original Python/Yelp replay
also matched every model payload byte-for-byte against its frozen CLI archive.
The portable runner retains reconstruction checks and additionally checks the
recorded payload hash when running the original corpus. Provisional Python modes
remain labelled in the recorded tables. Pareto frontiers use archive factor and
decoding speed only, not compression speed.

The optional WASM [instructions](wasm/README.md) retain a separate exploratory
measurement; the main tables are native C++. The older file/process benchmarks
are a different protocol, implemented by [runner.py](../src/compression_lab/runner.py)
and [engine.py](../src/compression_lab/engine.py); they are not mixed into these RAM plots.
