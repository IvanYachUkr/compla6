# Native RAM workloads

`strings-v1` evaluates separate native encoder and decoder libraries through
[`codec.h`](../src/compression_lab/data/strings/codec.h). Each call receives one
complete input in memory. The same ABI supports three explicit framing choices:

| `row_framing` | Input interpretation | Variants |
| --- | --- | --- |
| `lf` (default) | LF ends a row and remains part of it | `bulk`, `rows` |
| `nul` | NUL ends a row and remains part of it; embedded LF is ordinary data | `bulk`, `rows` |
| `none` | One opaque byte sequence per input file | `bulk` |

Both delimited modes preserve empty rows and a final unterminated row. An empty
file has zero rows. Old `strings.json` files without `row_framing` retain their LF
interpretation and original digest. Existing results and published benchmark
sources are unchanged.

## Owner setup

Use one installed Lab version to provision each new workspace. Supply every
commissioned input file:

```sh
compression-lab native-init --workspace runs/sqlstorm --dataset sqlstorm \
  --input /data/sqlstorm/title.bin /data/sqlstorm/body.bin \
  --row-framing nul --implementation from_scratch --cpu 6
```

For DBText, supply its complete columns with `--row-framing lf`. For complete
byte files use `--row-framing none`. Choose a CPU available in the process's
affinity mask. Defaults are LF framing and a from-scratch implementation.

The command builds the supplied timer, pins the inputs, timer source/header,
executable and compiler, and writes `strings.json`, `commission.json`, generated
`INSTRUCTIONS.md`, `references.json`, `runtime/build.json` and a host receipt in
`run-metadata.json`. It supplies `workbench/codec.h` and
`workbench/manifest-template.json`. It refuses to replace a nonempty workspace.
The timer build is data-independent and its elapsed time is recorded separately.

An explicitly open implementation can declare separately installed codec
libraries with repeated `--standard-library SONAME=/absolute/library/path`.
Their exact hashes are pinned; only those commissioned standard libraries can
be excluded from package accounting. Custom decoder libraries and statically
linked codec code remain charged. The strict deployment total is also reported.

Native commissions optimize charged package size and fresh decoding time, with
compression speed competitive with matched baselines.

## Candidate evaluation

Create an encoder, independent decoder and `ALGORITHM.md` under the workbench,
using the source manifest template. Record a hypothesis before substantive
algorithm changes, and put its returned ID in that manifest:

```sh
compression-lab native record-hypothesis --workspace runs/sqlstorm \
  --statement 'Describe the proposed mechanism' \
  --expected-benefit 'State its expected size or speed benefit' \
  --falsifier 'State the measurement that would reject it'
compression-lab native evaluate --workspace runs/sqlstorm \
  --candidate runs/sqlstorm/workbench/agent/my-codec/candidate.json --quick
compression-lab native submit --workspace runs/sqlstorm \
  --candidate runs/sqlstorm/workbench/agent/my-codec/candidate.json
compression-lab native status --workspace runs/sqlstorm --job JOB_ID
compression-lab native compare --workspace runs/sqlstorm --results RESULT_ID
compression-lab native export --workspace runs/sqlstorm --result RESULT_ID
```

`evaluate` blocks; `submit` returns an asynchronous job ID. A quick evaluation is
a development check and cannot qualify or export. Full native qualification
snapshots the declared source, rebuilds it reproducibly, runs valid-corpus UBSan
diagnostics, and checks independent exact decoding and every requested row.
Source builds receive no dataset mount: automatic fitting and preprocessing
belong inside `lab_encode`. All required reconstruction information belongs in
the archive or charged decoder. Row-access mechanism and scientific claims
still require source review; native qualification does not claim the separate
file-workflow corruption suite was run.

For agent access, start `compression-lab-native-strings --workspace runs/sqlstorm
--workbench runs/sqlstorm/workbench --port PORT` with `COMPRESSION_LAB_TOKEN` set
in the environment. Its generated instructions describe the MCP workflow.
Completion requires the commissioned variants; byte-only runs can finish with
their bulk result. Optional remote benchmarking still needs an owner-configured
`server.json` and worker. Requests carry the exact configured corpus hashes and
framing. The worker must use that same commissioned corpus and framing.

## Measurement boundaries and portable replay

The unchanged C++ driver reads the input and allocates output buffers before
timing. Encoding measures `lab_encode`; fresh decoding includes `lab_open` and
complete reconstruction. Warm decoding is a second operation on the same state.
Cleanup is recorded separately. A full run uses one warmup and seven measured
trials, one pinned CPU core, a 2 GiB process memory limit and a host measurement
lease. Every pass checks exact output. Corpus timing sums matching per-column
trials before taking the median. Row access uses fixed nested random subsets at
1%, 3%, 10%, 30% and 100%, with sorted IDs and exact byte/offset checks. It is
reported separately from full decoding. The driver limits each output buffer to
512 MiB; this interface is for bounded columns or byte files.

The [DBText replay scripts](../benchmarks/dbtext/README.md) keep their original LF
defaults and byte-identical published timer/adapters. `build.py --methods lz4
zstd1` can build only selected methods. Builds validate pinned published and
upstream sources before compilation and save the actual compiler/source pins.
For a new NUL workload, the same scripts use the package's configurable adapters:

```sh
python benchmarks/dbtext/build.py --out builds/sqlstorm \
  --row-framing nul --methods lz4 zstd1 fsst onpairplus
python benchmarks/dbtext/run.py --data-dir /data/sqlstorm \
  --columns /data/sqlstorm/columns.json --build-dir builds/sqlstorm \
  --out results/sqlstorm --row-framing nul --cpu 6
```

`columns.json` is an array of `{ "name": "title.bin", "bytes": 123,
"sha256": "..." }` records; all supplied sizes and hashes are checked for full
runs. The build and evaluation framing must match. Recorded candidate sources
remain restricted to their original LF protocol; the reusable FSST/OnPair+
adapters accept `LAB_ROW_DELIMITER=0` for NUL. Use
`python benchmarks/upstream/fetch.py` once when OnPair+ sources are needed.
Portable replay is for the reviewed supplied codecs; native source qualification
uses the Lab's required isolated process runner. `--smoke` uses one column and
one measured trial and is explicitly labeled as a smoke run in the receipt.
