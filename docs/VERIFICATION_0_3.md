> Historical 0.3.0 record. Current setup and verification are linked from
> [README](../README.md). Referenced old results and host wrappers remain in the
> immutable 0.5.0 archive identified by `provenance/PREVIOUS_RELEASE.json`.

# Compression Lab 0.3.0 — executed verification and limits

## Result

The final source passed **134/134 core regression tests**,
with zero failures, errors or skips (219.358 seconds). The final
reproducible smoke completed **10/10 train-only screens and 9/9 full-quality
controls**. These are **not nine certified results**: every control is explicitly
ineligible because this is a public synthetic smoke. Two controls additionally
retain timing-noise flags, and XZ fails the unchanged 100 MB/s encoding floor.

Source, input manifests, raw trial arrays, failed attempts, compiler/runtime hashes,
export receipts and negative results are included. Nothing was fetched from a
private or independent test split. No model/provider calls were made by the tests,
preparation, benchmark or integration probes; these are engineering checks, not
an experiment establishing better LLM research productivity.

## Evidence map

All paths below are relative to the package root. JSON and raw logs are the source
of truth; this document rounds numbers only for display.

| Check | Evidence |
|---|---|
| Original supplied package: 116 tests, 112 pass, 4 SDK-absent skips | `results/redesign/original-suite.log`; `final-tests.json` is the earlier **original** baseline, despite its historical filename |
| Final 134-test suite and per-test names | `results/redesign/regression-release.json`, `.txt` and `release-execution-receipt.json` |
| Ten support-tool tests | `results/redesign/tooling-release-process.log`; earlier `tooling-final.log` retained |
| Clean isolated installed wheel, complete native control and export | `results/redesign/installed-wheel-acceptance/summary.json`, `baselines.json`, `baseline.zip`; `installed-wheel-acceptance-process.log` |
| Built wheel/source byte match and package metadata | `results/redesign/wheel-verification.json`, `wheel-build.log` |
| Official SDK stdio/HTTP and public-boundary regression | `tests/test_mcp.py`, `tests/test_redesign_mcp.py`; final regression log |
| Additional read-only live SDK stdio probe | `results/redesign/mcp-probe-release.json`, `mcp-probe-release-process.log` |
| Preparation and cached recipe integration | `results/redesign/tool-preparation.log`, `tool-stdio-probe.json`; appended public receipts in `smoke-v1/workspace/` |
| Final benchmark plan, all attempts and summary | `results/redesign/smoke-release/PLAN.json`, `attempts.json`, `SUMMARY.json` |
| Trial-by-trial old/new CRC comparison | `results/redesign/smoke-release/crc-ablation/` |
| Exported Zstd, transformed Zstd, static Zstd runtimes | `results/redesign/smoke-release/*-export.zip`, `exports.json` |
| Actual compiler, libraries, headers, SDK/build installed files | `dependencies/observed-runtime-release.json` |
| Official current releases, exact source identities and licenses | `dependencies/upstream-lock.json`, `upstream-extra.json` |
| All 29 retained MCP wheelhouse hashes checked against lock | `results/redesign/mcp-wheelhouse-verification.json` |
| Original input bytes and per-file change inventory | Outer release `provenance/INPUT_VERIFICATION.json` and the verbatim input ZIP |

The SDK tests exercised the official v1.29.1 library, including real stdio and
bearer-authenticated loopback HTTP, cancellation/protocol behavior covered by the
retained suite, and absence of private owner operations from model-facing tools.
They are not a live Prime/ZCode UI/provider session. See the per-test names for the
precise assertions; no generic “all integrations certified” claim is made.

The regression suite retains relational byte framing, null-versus-empty, lexical
spelling and ordering tests, and mutable transaction, persistence, crash, bounded
protocol, independent oracle and complete-store accounting tests. The new static
screen and supplementary deployment profiles do not replace those contracts.

## Reproduction

After provisioning the explicitly documented native prerequisites:

```sh
python3 -m venv /absolute/new-core-venv
/absolute/new-core-venv/bin/python -m pip install --no-index --no-deps \
  dist/compression_lab-0.3.0-py3-none-any.whl
COMPRESSION_LAB_VENV=/absolute/another-clean-venv \
  sh tools/acceptance.sh /absolute/new-installed-acceptance
/absolute/new-core-venv/bin/python tools/benchmark_redesign.py \
  --output /absolute/new-redesign-smoke
# With the documented optional compatible MCP SDK environment:
python tools/run_tests.py --output /absolute/new-regression-report
python -m unittest discover -s tools/tests -v
```

Output directories must be new. Native source/runtime changes require a fresh
workspace. An extracted historical benchmark workspace records this environment's
paths and fingerprint: inspect it as evidence, do not edit hashes to resume it on
a different host. The release verification helper checks byte inventories and
modes; it does not magically reproduce timings or make old workspaces portable.

## Final smoke method

`tools/benchmark_redesign.py` writes its fixed recipe plan **before** measurements.
The seed is `compression-lab-redesign-smoke-v1-20260907`. Six deterministic objects
of 4 MiB each form **24 MiB = 25,165,824 canonical bytes**: independent training and
development objects for synthetic logs, little-endian uint32 data and opaque SHAKE
bytes. The development partition is 12 MiB. The generator and test establish exact
reproducibility and distinct split/group identities. There are no private objects.

The full speed columns below use **training plus development together**, seven
fresh encoding processes and seven fresh decoding processes, pinned to one CPU.
Time includes namespace/helper/native startup, native I/O and supervision. The
filesystem cache is warm; no `fsync`/cold-storage durability claim is made. There
is no startup subtraction, warmup trimming or replacement by a self-reported
codec-kernel time. The archive column and ratio use **development only**; the ratio
is compressed bytes divided by canonical development bytes, before fixed costs.

Observed host: Debian 13.3, Linux 6.18.35, AMD EPYC 9V74, Python 3.13.5, GCC/G++
14.2.0. The cgroup reports four CPU cores of quota, allowed CPU set 0–4 and 4 GiB
memory. This is a shared execution host, not a dedicated quiet benchmarking box.
All dispersion/trial arrays remain in evidence. The suite and clean-install check
were serialized outside this final benchmark; no claim of control over other
host tenants is made.

| Control | Dev archive bytes | Dev archive ratio | Encode MB/s | Decode MB/s | Additional flags |
|---|---:|---:|---:|---:|---|
| `reference-crc-0-2-2` | 7,680,693 | 0.610407 | 114.65 | 127.35 | none beyond smoke |
| `zstd-1` | 7,680,693 | 0.610407 | 202.08 | 220.25 | none beyond smoke |
| `stored` | 12,583,107 | 1.000015 | 189.57 | 215.73 | timing_noisy_logged_rerun_required |
| `lz4-0` | 9,195,575 | 0.730799 | 200.33 | 220.38 | timing_noisy_logged_rerun_required |
| `brotli-4` | 8,093,216 | 0.643191 | 101.92 | 173.87 | none beyond smoke |
| `xz-3` | 4,742,735 | 0.376919 | 5.96 | 123.21 | encoding_below_100_MBps |
| `zstd-1-shuffle4` | 4,754,587 | 0.377861 | 181.62 | 200.24 | none beyond smoke |
| `lz4-0-shuffle4` | 5,355,787 | 0.425640 | 233.26 | 226.86 | none beyond smoke |
| `zstd-1-static` | 7,680,693 | 0.610407 | 180.46 | 222.59 | none beyond smoke |

Every row has `quality_passed=true` and `eligible=false`. `reference-crc-0-2-2`
is the verbatim original native C++ source rebuilt with the same current compiler
and Zstd library, evaluated by the final engine—not a selectively imported old
measurement. Total smoke harness wall time was 250.049 seconds;
that includes work beyond the timed native codec processes.

The four-byte shuffle is useful on this deliberately mixed synthetic fixture;
it is not evidence of universal gains on logs, Text8 or arbitrary relational data.
XZ has the smallest archive in this table but is not a valid fast-track winner.

## CRC ablation and byte compatibility

A separate fixed alternating old/new versus new/old diagnostic records seven
fresh sandboxed trials per implementation per direction. Median encode speed was
**118.89 → 201.91 MB/s
(1.698×)**; decode was
**126.06 → 223.30 MB/s
(1.771×)**. Its archives are byte-identical.
The diagnostic cost is separately recorded and does not replace scored full trials
or silently consume an unreported model budget.

The accelerated IEEE CRC-32 helper is checked against Python/zlib across 1,027
lengths and incremental updates. Tests compare complete identity archives with
the original implementation across all five native families. Transformed candidates
have explicit CLT1 framing and separately tested inverses, tails, malformed input
and corruption checks. ASan/UBSan instrument candidate source, **not** precompiled
distro shared libraries or copied static archives.

The preceding `smoke-v1` replication is retained unchanged: its alternating encode
speed ratio was 1.693×
and decode 1.650×.
It is not averaged into the final score or discarded for its noisy controls.

## Accounting did not disappear with faster code

The new helper increases code/binary cost. The static-link variant moves library
code into a larger executable and includes its `.a`/headers as charged build
assets; it is not a free preinstalled-library assumption. Actual development totals:

| Implementation | Packed source/build assets | Executable | Nonplatform shared dependencies | Historical primary total | Standalone deployment total |
|---|---:|---:|---:|---:|---:|
| Original C++ / shared Zstd | 5,175 | 57,616 | 825,336 | 7,691,200 | 8,570,943 |
| 0.3 shared Zstd | 6,569 | 69,920 | 825,336 | 7,692,594 | 8,583,115 |
| 0.3 static Zstd | 438,896 | 824,760 | 0 | 8,124,921 | 8,512,563 |

All numbers are bytes. Each total follows its versioned accounting definition;
configuration, notices/dictionaries and archive bytes are not omitted. The raw
report also retains the historical additive source-plus-binary view and the
100 MB development projection. No view is silently substituted for another.

The predeclared installed-Zstd profile can deduct only the exact matching SONAME
**and SHA-256** from its supplementary incremental-deployment view. The primary
standalone totals above remain unchanged, and exports still contain the complete
runtime. Source, configuration, executable, notices and dictionaries are never
waived. Static linking helps one deployment view slightly here while substantially
increasing the source/build-asset view; both are reported.

## Security, portability and remaining host checks

All six actual candidate sandbox canaries passed: network denied, fork denied,
private canary inaccessible, runtime read-only, PID namespace active and host
`/proc` absent. However, release preflight is **blocked** with
`root_build_process_limit_not_certified` and `owner_boundary_missing`. Public root
smoke is not a substitute for distinct evaluator/agent UIDs, a commissioned owner
boundary and a one-shot private test. The suite also independently relocates an
exported runtime and decodes without engine, dataset or training mounts.

The clean installed-wheel acceptance passed all its quality gates, recorded seven
encoding and seven decoding trials, and exported a runtime. Its small Hadoop
fixture was below the 100 MB/s encoding floor; the result retains both
`encoding_below_100_MBps` and `smoke_not_certified`. Installation success was not
misreported as speed eligibility.

The built core wheel installs without third-party Python runtime packages. Native
libraries remain explicit operator prerequisites. The supplied cp312 wheelhouse
was hash-verified, but tests here used cp313 plus compatible base-image packages;
this is not a new pristine cp312 lock installation. The build backend 82.0.1 was
preinstalled: its upstream wheel is **not** included in the input. The hash-pinned
operator setup for it is documented rather than claiming an offline wheel exists.

Current upstream sources were verified through the web. Container DNS/network
prevented source retrieval and new upstream builds. In particular, Brotli 1.2.0
and XZ 5.8.3 were **not built here**; measured versions are 1.1.0 and 5.8.1.
MCP 2.1.1 is recorded as current but not a tested server port; the server deliberately
retains tested 1.29.1. No new OCI image build is claimed.

Local desktop commissioning still needs the actual Prime 0.9.3 executable/capsule,
ZCode configuration precedence and UI tools, nonroot evaluator/agent mounts and
bearer-token permissions, provider dispatch and cancellation through those hosts.
The new launcher has syntax/configuration tests but was not run as a live desktop
capsule. ZCode's stable installer version/license/hash was not verified and no
installer is redistributed. The host instructions distinguish these checks from
working local official-SDK transport tests.

## Evidence history

Red/green logs are preserved. An early improved-suite attempt failed three SDK
checks because subprocesses could not import the not-yet-installed editable
package; that environment setup failure is retained in `initial-regression-*`,
not relabeled as passing. A bounded exploratory feature-test timeout is also kept.
After installing the editable package into the SDK test environment, the full
suite passed. Two final factual description corrections—build-backend provenance
and sanitizer coverage of precompiled static libraries—changed the engine digest,
so both regression and smoke were rerun against the final source. The earlier
successful `regression-final`/`smoke-v1` evidence was not edited.

Additional preparation/SDK probes after the first smoke append their own ledger
and budget receipts. They do not alter the first smoke's raw trials or summary.
The final release contains the original uploaded ZIP verbatim; the packager checks
all 249 original manifest entries, stages a complete source/evidence tree, writes
inner/outer SHA-256 manifests and checks every ZIP member hash and mode. The final
packaging receipt and ZIP SHA-256 accompany the deliverable.
