# Reports from measured evidence

The same command produces a compact Markdown table, canonical CSV and optional
static PNG/PDF Pareto plots. It reads evidence; it does not run a benchmark.
Choose a **new** output directory outside `benchmarks/recorded`.

```sh
# No plotting dependency needed for Markdown and CSV.
compression-lab-report benchmarks/recorded/dbtext/native-results.csv \
  --out benchmarks/_runs/dbtext-report

# Optional plots: install once in the same Python environment.
python -m pip install '.[plots]'
compression-lab-report benchmarks/recorded/whole/trials.json \
  --out benchmarks/_runs/whole-report --plots

# New runs use their adjacent run-metadata.json automatically.
compression-lab-report benchmarks/_runs/my-run/summary.json \
  --out benchmarks/_runs/my-report --plots

# Explicit alternate views; only measured values can be selected.
compression-lab-report benchmarks/recorded/dbtext/rows.json \
  --out benchmarks/_runs/rows-warm-report --size archive --decode warm --plots
```

From a source checkout without installation, replace `compression-lab-report`
with `PYTHONPATH=src python -m compression_lab.reporting`.
`python benchmarks/plot_results.py` remains a shortcut for the recorded
Python/Yelp plots, now writing to `benchmarks/_runs/whole-report`. Its arguments
are the same, and `--no-plots` omits the matplotlib dependency.

## What is comparable

Each table and plot uses one dataset/workload, machine, protocol, run-parameter
fingerprint, decoder state, size convention and selectivity. Different contexts
receive separate panels. Metadata includes the selected CPU and timing parameters;
a CPU model name alone is insufficient to establish a matching run. Legacy files
without a receipt use a source-local identity. Shipped historical extracts say
“recorded server evidence”; their hardware fingerprint was not retained.

The default `--size auto` uses **archive** for whole-corpus and paper results,
and **charged package** for native string-column results. Archive includes
framing, dictionaries and indexes. Charged package also includes the complete
custom decoder and other charged dependencies; separately identified standard
codec libraries are excluded. Strict deployment totals stay in the CSV.
A missing package size is never treated as zero or estimated from archive size.

The default `--decode auto` uses **fresh** decoding when measured and **warm**
otherwise. Fresh includes decoder setup; warm reuses the initialized decoder.
Both measurements remain in the table and CSV when available. Whole-corpus plots
show compression factor on x and decompression **MB/s** on y; higher is better
on both axes. Row-selective native and FSST paper results have their own panels
in **million rows/s**. Their different aggregation protocols remain separate;
no conversion to MB/s is inferred. Native selected-row milliseconds are also
shown. A file lacking the original byte count gets a table with an unknown factor
and no size/speed plot.

A point is on the frontier unless another point is at least as good in both
metrics and strictly better in one. Equal points both survive. The frontier uses
full precision, not rounded table values. It is a descriptive size/speed
comparison, not a statistical significance test, an encoding-speed acceptance
gate, or full research qualification. Provisional markers remain visible. Smoke
runs are labelled and kept separate from performance measurements.

## Inputs and outputs

Supported inputs are whole-corpus `summary.json`, `trials.json` or recorded
`comparison.csv`; native strings result, comparison or `summary.json`, or
`native-results.csv`; and paper `trials.json`, `RESULT.json` or `summary.json`.
Paper summary/CSV extracts need their adjacent trials/size evidence. A run directory
selects its summary automatically. Multiple input paths can be passed together;
comparison boundaries still apply.

`report.md` is the readable table. `results.csv` retains exact byte counts, both
decoder states, units, strict totals, source path and SHA-256, result ID when one
exists, context fields and the selected derived factor/frontier. It is also a
supported input, so an existing report can be replotted without remeasurement.
PNG files use deterministic context names; `pareto.pdf` contains all panels.
The core reporter uses only the Python standard library.

The small Python API is `load_results(path)`, `prepare(rows, size='auto',
decode='auto')` and `write_report(rows, out, plots=False)`. `prepare` and
`write_report` use the same grouping and frontier calculation.
