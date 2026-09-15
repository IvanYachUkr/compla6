# Migration from 0.2.2 to 0.3.0

## Preserve, install, start a new run

Preserve the old workspace and exports unchanged. The complete submitted input ZIP
and its hash/manifest are in the outer release's `provenance/`. Historical package
files remain available there even where the working README/tools have changed.
Install the 0.3.0 wheel in a new environment, run `doctor` and `doctor --release`,
then initialize a **new** workspace from the authorized public card/manifest.

Do not edit an existing workspace's runtime digest, source seal or ledger to make
old results appear current. Source/runtime changes intentionally invalidate reuse.
Re-register useful 0.2.2 candidate source and run full gates under the new runtime.
Moving old evidence is fine; resuming its evaluations on changed toolchain/paths
without a fresh run is not. Exported native packages are portable artifacts; the
Python benchmark workspace itself is not an arbitrary-host resumable checkpoint.

## Compatible and new interfaces

Existing CLI/MCP operations and candidate ABI remain supported. New operations:
`inventory`, `brief`, `feedback`, CLI `recipe-create` / MCP `recipe_create`, and
CLI `baseline-trial` / MCP `baseline_trial`. `evaluate` additionally accepts
`depth=screen`; `quick` retains its old non-promoting role. `baselines prepare`
retains the original matrix, while `tools/prepare_research.py` performs an explicit
cheap-screen/full-control sequence and keeps a preparation receipt.

Recipe IDs include `stored`, `zstd--3` (the minus-level spelling is intentional),
`zstd-1`, `zstd-3`, `zstd-9`, `zstd-1-dict`, `zstd-3-dict`, `lz4-0`, `brotli-4`,
`xz-3`, plus the documented transforms/static variants returned by `inventory`.
Do not infer IDs from display names. Static-codec factory support is limited to
Zstd/LZ4, not a generic claim that every family has a portable static closure.

Only a full, quality-passing result enters the qualified baseline set. Speed/noise
eligibility remains an additional distinction. Baseline cache reuse registers the
actual factory snapshot first and matches digest/card/runtime/depth/track—not the
human candidate ID. An old same-name experiment is not a control for another recipe.
The screen has a hard ceiling of 64 objects / 64 MiB. When no whole training object
fits, use quick/full evaluation; do not silently redefine object boundaries. A new
chunked dataset contract must be explicitly predeclared with group-safe splits.

New screen replies have `screen_passed`, `quality_passed=false`, `eligible=false`
and `screening` provenance/timing. Development score/timing fields are null.
Treat this as a diagnostic signal, never a missing full result to fill in manually.
`feedback` and paired diagnostics still require matching immutable result context.

## Versioned policies, unchanged primary ranking

Optional immutable card fields use their own schema version 1:

```json
{
  "screening_policy": {
    "schema_version": 1,
    "max_objects": 8,
    "max_canonical_bytes": 8388608,
    "seed": 20260907
  },
  "runtime_scenarios": {
    "schema_version": 1,
    "profiles": [{
      "id": "preinstalled-zstd-v1",
      "libraries": [{"soname": "libzstd.so.1", "sha256": "EXACT_64_HEX_DIGITS_FROM_INVENTORY"}]
    }]
  }
}
```

The illustrative SHA string is deliberately invalid: replace it with an observed
exact hash **before initialization**, or omit `runtime_scenarios` entirely. Empty,
unknown, duplicate, path-like and mismatched profile declarations fail closed or
remain explicitly unmatched; no SONAME-only exemption exists. Schema extensions
are rejected on mutable-store cards, which keep their own accounting contract.

Primary accounting remains the complete `standalone-v1` view. Supplemental
`installed-runtime-report-v1` is an explicitly predeclared deployment assumption,
not a codec score, a frontier objective or a reason to remove files from an export.
Only matching nonplatform library bytes are deducted. Dictionaries, notices,
configuration, executable and packed source remain reported. Historical-primary,
historical-additive and complete-deployment totals remain available together.

Identity native templates still write **CLB1-v1**; the original and new templates
produce identical archives across all five controls in the differential tests.
Transformed templates write **CLT1-v1**, with a transform ID and literal fallback;
their own exported decoder is required. Never claim an old identity decoder accepts
CLT1. The full ABI document retains stream/object framing around these envelopes.

Dictionary fitting now uses `bounded-hash-order-prefix-v2` sampling: deterministic
train-only prefixes, at most 128 KiB per object, 8 MiB total and 4,096 chunks. Every
contributing object is hash-verified in full before its prefix is consumed, so
verification I/O can exceed the sample-memory bound. Dictionary/source identities
therefore change. Registration and all copied dictionary bytes remain charged.

## Data reads, budgets and evidence

Discovery, briefs and enqueue/registration use sealed metadata instead of repeatedly
hashing all payloads. A screen verifies selected whole training payloads only.
Quick/full workers and export still hash all public bytes. This avoids O(dataset)
discovery costs without falsely certifying unchecked development data. A regression
explicitly changes development bytes: brief/screen do not read them, full rejects.

Trusted factory/dictionary work is now wall-budgeted and serialized. Failure keeps
its consumed attempt. Factory sampling bounds do not impose hard preemption of
trusted Python fitting; host/model costs, model thinking and manual source editing
are not silently included in the engine's evaluation-wall budget. Record those
separately for equal-budget research comparisons. Exact cache hits still incur and
record the work needed to reconstruct/verify a recipe; they are not free credits.

Runtime/source hashes bind results. Read-only result files plus ledger digests do
not defend against a malicious agent with the evaluator UID; deploy the unchanged
OS ownership boundary. Full sanitizer checks instrument native wrappers and custom
source, not opaque precompiled distro `.so` or `.a` implementations.

## Host migration

The current shared skill is `src/compression_lab/data/SKILL.md`. Existing owned
connection installation refuses to overwrite a modified skill/config. Preserve your
edits; disconnect the unchanged old owned entry, then reconnect from the new venv,
or merge/review your custom copy manually. Preview first. Reconnection is not a
live protocol or provider-authentication test.

Prime's old 0.9.2 path layout is no longer assumed by the Linux launcher. Set the
explicit commissioned runtime root, Prime executable and kernel Python. A legacy
copy is in `provenance/legacy-tools/`; the sibling host guide explains current
0.9.3 verification and what remains untested on that desktop.

ZCode loads native `.zcode` servers instead of merging same-scope `.agents` servers.
An unrelated native server can hide an old generic entry; user-level names can
shadow workspace names. Inspect the effective tool list after reconnecting. Never
copy a bearer token into a committed workspace. Authenticated HTTP is the hardened
cross-UID path; generated stdio entries are public-smoke convenience only.

Do not replace the old pinned OCI inventory with invented current hashes. The
preserved image recipe is historical; a 0.3 image and latest Brotli/XZ builds must be
built, observed and tested locally before claims about those runtimes are made.
