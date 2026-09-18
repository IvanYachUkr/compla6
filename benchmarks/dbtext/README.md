# First comparison: our DBText benchmark

This produced the first tables: compressed/package sizes, compression MB/s,
fresh/warm full decoding MB/s, and selected-row reconstruction milliseconds.
[Recorded tables](../recorded/dbtext/README.md).

1. Read **[benchmark.cpp](benchmark.cpp)** (90 lines). This is the actual native
   timer: load the input, allocate the common output buffer, time encoding or
   decoder setup/reconstruction, repeat warm decoding, check output bounds.
2. Read **[strings.py](../../src/compression_lab/strings.py)**, especially `rows`,
   `selections` and `evaluate`. This supplies the original columns, generates
   nested sorted row subsets, verifies exact bytes/offsets, discards the first
   pass, and aggregates seven passes into the reported medians.
3. Inspect **[reference.cpp](reference.cpp)** and **[onpair.cpp](onpair.cpp)** when
   checking what our baseline interfaces add: archive framing, row indexes and
   decoder state management.

[run.py](run.py) launches this same evaluator with portable file paths;
[build.py](build.py) compiles the codecs and timer. They are supporting scripts.
The executable is still named `driver`; only its review-facing source filename
changed from `driver.cpp` to `benchmark.cpp`. Its source bytes are unchanged.

This path is independent of the later [FSST paper wrapper](../fsst-paper/README.md).
Its core benchmark has two parts, C++ timing and Python evaluation; the C++ file
alone does not define the row sampling or table aggregation.
