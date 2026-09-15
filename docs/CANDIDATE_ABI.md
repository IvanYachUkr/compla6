# Native candidate ABI

Use `brief` for this run's dataset mode, permitted implementation, primary size
policy, resources and completion method. New runs default to whole-dataset research.

## Complete codec-free v2 manifest

`manifest_template(name)` returns a complete C++ v2 manifest with the actual
compiler path/hash, no algorithm implementation and no dependency. The CLI is
`compression-lab manifest-template --name my-codec`. Implement `codec.cpp` and
`decoder.cpp`, or adjust the source/output paths to match your design. No baseline
creation is needed. This example replaces only the toolchain values with labels:

```json
{
  "schema_version": 2,
  "candidate_id": "my-codec",
  "language": "c++",
  "input_domain": "opaque_bytes",
  "deterministic": true,
  "threads": 1,
  "source_paths": [
    "codec.cpp",
    "decoder.cpp"
  ],
  "artifact_paths": [],
  "runtime_paths": [],
  "build_commands": [
    [
      "g++",
      "-std=c++17",
      "-O3",
      "-DNDEBUG",
      "-ffile-prefix-map=/source=source",
      "-ffile-prefix-map=/output=build",
      "{source}/codec.cpp",
      "-o",
      "{build}/codec"
    ],
    [
      "g++",
      "-std=c++17",
      "-O3",
      "-DNDEBUG",
      "-ffile-prefix-map=/source=source",
      "-ffile-prefix-map=/output=build",
      "{source}/decoder.cpp",
      "-o",
      "{build}/decoder"
    ]
  ],
  "sanitizer_build_commands": [
    [
      "g++",
      "-std=c++17",
      "-O1",
      "-g",
      "-fsanitize=address,undefined",
      "-fno-omit-frame-pointer",
      "-no-pie",
      "-ffile-prefix-map=/source=source",
      "-ffile-prefix-map=/output=build",
      "{source}/codec.cpp",
      "-o",
      "{build}/codec"
    ],
    [
      "g++",
      "-std=c++17",
      "-O1",
      "-g",
      "-fsanitize=address,undefined",
      "-fno-omit-frame-pointer",
      "-no-pie",
      "-ffile-prefix-map=/source=source",
      "-ffile-prefix-map=/output=build",
      "{source}/decoder.cpp",
      "-o",
      "{build}/decoder"
    ]
  ],
  "build_output_paths": [
    "codec",
    "decoder"
  ],
  "executable": "codec",
  "commands": {
    "encode_dir": [
      "{runtime}/codec",
      "encode-dir",
      "{input_dir}",
      "{output_dir}"
    ],
    "decode_dir": [
      "{runtime}/decoder",
      "decode-dir",
      "{input_dir}",
      "{output_dir}"
    ],
    "encode_stream": [
      "{runtime}/codec",
      "encode-stream"
    ],
    "decode_stream": [
      "{runtime}/decoder",
      "decode-stream"
    ]
  },
  "decoder": {
    "executable": "decoder",
    "build_output_paths": [
      "decoder"
    ],
    "artifact_paths": [],
    "runtime_paths": []
  },
  "diagnostics": {
    "kind": "cxx-asan-ubsan-v1"
  },
  "toolchain": {
    "g++": {
      "path": "/actual/compiler/from/manifest_template",
      "sha256": "actual-sha256-from-manifest_template"
    }
  },
  "dependencies": [],
  "training": {
    "kind": "data-independent"
  }
}
```

Add `hypothesis: {"experiment_id": "h-ID-from-record_hypothesis"}` when the card
requires it. Record the hypothesis before a substantive implementation change.
If fitting artifacts, list all of them in `artifact_paths`, plus
`decoder.artifact_paths` when decoding requires them. Declare
`training: {"kind": "whole-dataset", "object_sha256": ["actual-corpus-sha256"]}`;
split runs instead use `train-only` and training-object hashes. Data-independent
implementations use the example's `training.kind: data-independent`.

## Offline preparation

Cards selecting `offline-plus-online-v1` allow slow fitting, preprocessing and
data-dependent code generation. Fitted candidates must add an `offline` role to
their v2 manifest, build its native executable in both normal and diagnostic
builds, and declare every produced file in the top-level `artifact_paths`:

```json
"offline": {
  "executable": "prepare",
  "command": ["{runtime}/prepare", "{input_dir}", "{output_dir}"],
  "artifact_paths": ["dictionary.bin"],
  "rebuild": false
}
```

Add `prepare` to `build_output_paths`. The offline artifact list must exactly
match the full artifact list. Keep artifacts required for reconstruction in
`decoder.artifact_paths`; preparation executables need not belong to the decoder.
Use `rebuild: true` when generated headers, tables or code require compilation
after fitting. All declared build commands are then replayed and their measured
time is included in the offline trial. Pure software compilation independent of
the dataset is recorded separately.

The input directory contains raw `00000000.bin`, `00000001.bin`, etc., ordered by
canonical SHA-256 and alias, plus `manifest.json` with `schema_version: 1` and an
`objects` array of `path`, `alias`, `canonical_sha256`, `canonical_bytes` and `split`.
The complete corpus is supplied in whole-dataset mode; split mode supplies only
training objects. The read-only preparation runtime excludes fitted artifacts
and other compiled executables. Write exactly the declared artifact files into
the empty output directory; standard output must stay empty.

The lab checks fresh artifact bytes against the registered inventory and runs
online encoding with those fresh outputs. If rebuilding is required, executable
bytes and dependency identities must also match registration. Full evaluation
checks preparation under the candidate's diagnostic build, then repeats fitting
for each of seven measured encode trials. The default offline allowance is one
hour per preparation, including required rebuilding; memory, CPU and the remaining
overall evaluation budget still apply. Use `limits.offline_timeout_seconds` to
commission a shorter allowance. Required compilation uses one CPU from the selected
profile under the existing compiler sandbox, with its resources recorded separately.

Declare all dataset-dependent work, including representations prepared before
online encoding. Source review is still required to establish that the declared
pipeline covers every such operation; manifest assertions alone cannot prove it.
Legacy timing cards keep their original contract and do not accept this role.

Every decoder path must also belong to the corresponding full manifest list.
A combined executable is supported, but its complete bytes count as decoder code.
Every decode gate mounts only that independently staged runtime. Runtime commands
are argument arrays. Builds use `{source}` and `{build}`; runtime commands use
`{runtime}`, `{input_dir}` and `{output_dir}`. No shell or model participates in
encoding or decoding. The runtime is native Linux x86-64 ELF.

Registration snapshots before building. The declared source/artifact/runtime
inventory must be exact: missing or undeclared files, symlinks, hardlinks and
undeclared build outputs are rejected. Full evaluation rebuilds and requires
identical executable bytes and dependency identities. Every nonplatform native
dependency has a pinned `soname`, `build_path`, `sha256`, `version` and `license`.
From-scratch mode permits standard language/runtime facilities only.

## Resources and diagnostics

`threads` declares maximum execution threads including the main thread, from
1 to 64. Honor `COMPRESSION_LAB_THREADS` and the aggregate memory limit. The
engine sets affinity, denies subprocesses/network access, and records observed
native tasks, CPU and RSS. One-worker reference profiles cannot replace the
commissioned primary profile. Use measured scaling rather than extrapolation.

C/C++ full diagnostics require explicit ASan/UBSan flags and verified runtime
linkage. Stable Rust uses `rust-checked-v1`, direct `rustc` recipes and explicit
`-Cdebug-assertions=yes` and `-Coverflow-checks=yes`. Obtain the compiler identity
with `candidate.rust_identity()` under the evaluator's configured
`COMPRESSION_LAB_RUST_TOOLCHAIN`. Rust checks assertions/overflow, not unsafe memory,
FFI or races. Cargo build scripts and nightly sanitizer certification are not
supplied. Precompiled libraries are not rebuilt with sanitizers.

## Logical stream and directory formats

Both transports use the supplied strict HBI1/HBA1 parsers:

* Header: four-byte magic HBI1 (original) or HBA1 (archives), one-byte version 1,
  four-byte big-endian positive record count, at most 1,000,000.
* Each record: four-byte big-endian alias length, exact ASCII alias bytes,
  eight-byte big-endian payload length, exact payload bytes.
* Aliases match `[a-z0-9]+(?:-[a-z0-9]+)*`, are unique and strictly increasing,
  and may contain between 1 and 1,048,576 bytes. Trailing bytes are invalid.

Directory input has a `names.bin` file in the corresponding packed format with
empty record payloads, plus `00000000.bin`, `00000001.bin`, etc. containing the
actual payloads in index order. Output uses the same layout with the other
magic. An ordinal layout preserves a one-megabyte logical name without pretending
that a filesystem supports a one-megabyte filename. Unexpected files, symlinks,
missing files, nonempty index payloads and nonempty output directories are invalid.
Every original object must independently decode with its archive and the declared
fixed package; neither other objects, filename lookup nor encoder state is allowed.
The engine checks separately encoded single objects, decoder subsets, repeat runs,
reordered data under new aliases, names and directory/stream equality.

The HCB1 adapter verifies the canonical bundle before submission. Contents may
be empty, non-UTF-8, NUL-containing or syntactically strange text. Only the HCB
container must be canonical. A byte candidate may encode HCB objects opaquely.
Literal fallback is encouraged; excluding inconvenient valid contents is not.

## Size and timing

Archive accounting includes aliases, record lengths and batch framing. Each
reported partition includes its nine-byte batch header. Required decoder artifacts,
config, notices and code are charged once by their physical runtime inventory.
The strict score V+B+A+D is complete archive bytes, decoder code, required decoder
artifacts and nonplatform decoder dependencies. Source/build assets and encoder-only
binaries are disclosed separately. Export contains a hashed independent decoder.

When the card selects `standard-codec-available-v1`, rank by
`reported_accounting[scored_partition].actual.deployment_total_bytes` and retain
`decoder_accounting` as the strict total. Only pinned standalone standard library
files are deducted. A custom binary remains charged in full even if it includes a
statically linked codec. Legacy cards retain their original accounting policy.

For `offline-plus-online-v1`, report offline preparation, online encoding and
their combined time. Each stage includes measured native process startup, I/O and
computation; required dataset-dependent compilation belongs to offline time.
Combined trials sum the paired offline and online nanoseconds before computing
medians and throughput. There is no reuse amortization and no sum of independently
selected stage medians. Evaluator staging, hash verification, diagnostic builds
and correctness gates are excluded from codec timing and included in evaluation
wall time. Data-independent builds remain separately disclosed.

New cards set `encoding_floor_scope: combined`: the 100 MB/s bare minimum and
noise rule apply to end-to-end encoding, including offline work. Continue seeking
higher speed and compression after clearing it. If the densest implementation
misses this floor, deliver a separately qualified fast mode and retain the slower
maximum-compression mode with its measured tradeoffs. A codec without an offline
stage reports zero offline time and combined time equal to online time. Preserve
all three trial series in results and exports. An existing card explicitly using
`encoding_floor_scope: online` retains that older rule; other legacy cards retain
their sealed timing policy. Decoding ends with complete byte reconstruction using
only its declared bundle.

All seven fresh trials in each direction are retained, with no warmup subtraction
or fastest-trial selection. Whole-dataset timing covers corpus; validation-only-v2
covers development. The card's encoding floor uses decimal MB/s; decoder speed is
reported separately. Smoke cards cannot certify eligibility. Noise follows the
predeclared relative-MAD threshold; reruns consume distinct attempts.

Peak RSS includes possible bootstrap high-water marks; sampled native RSS can miss
short peaks. Corruption/diagnostic checks are bounded deterministic tests, not a
proof of safety or coverage-guided fuzzing. Report these practical limits without
weakening the required checks.
