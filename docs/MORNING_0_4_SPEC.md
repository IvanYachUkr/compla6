# Compression Lab 0.4 implementation contract

This version implements the user's 7 September 2026 morning request and later
multicore clarification. Historical 0.2/0.3 results keep their original contracts.
The public source baseline is the verified Pro architecture ZIP, SHA-256
`68da18722fb722c1a4d0052cb2ea787aad86d5cdb5273ade0313d19aa9c4d42e`.
The repair candidate is secondary input subject to inspection and local checks.

## Inputs and outputs

Inputs are immutable dataset cards with train/development objects, a native
candidate source manifest, pinned compiler/library files and explicit resource
and accounting policies. Outputs are immutable raw results, a comparable table,
and an independently usable decoder package. The owner-only test stays outside
all agent workspaces and is usable only after a complete submission freeze.

For the morning Text8 run, train and development are separate exact 100,000,000
byte streams from the previously prepared external corpus. The primary allowed
CPUs are 4,6,8,10: one logical CPU from each of four physical Ryzen 5600H cores.
The one-worker reference uses CPU 4. The shared native memory budget is 2 GiB
total, not per thread. All actual CPU IDs, topology, thread counts, memory,
library hashes and measurement policies are recorded in comparison identity.

## Resource contract

`runner.execute` accepts optional `threads=1` and `cpus=None` keywords. Threads
is an integer in 1..64, including the native main execution thread. CPU IDs are
unique nonnegative integers in the current process affinity; omitted CPUs are
resolved deterministically from available physical topology. Existing callers
retain one-worker behavior. No silent clamping or widening of unavailable CPUs.
Invocation records include resolved affinity, allowed native threads, observed
thread/process peaks, total-memory measurements and CPU time where available.

The runner passes the resolved limits to its trusted namespace helper, pins the
CPU set, exposes consistent `COMPRESSION_LAB_THREADS` and common thread-pool
environment limits, and scales CPU-time limits with the allowed CPU budget.
Native multithreading must work while process creation, new namespaces, network
access, host files and privilege escalation remain denied. Permit only genuine
same-process thread clones, and handle modern libc clone3 fallback correctly.
Bound and observe thread counts in addition to process counts. Memory remains
aggregate and the enclosing evaluator cgroup has an explicit matching CPU cap
and additional controller headroom. Do not claim polling is an instantaneous
peak or strict task cap; report the actual enforcement mechanism.

New cards can select a primary resource profile and one/two-worker references;
jobs, results and caches include the selected profile. A reference result cannot
replace the primary winner merely because its accounting happens to be smaller.
Mutable workload contracts remain unchanged unless explicitly supported and
tested; a static-profile change must not silently loosen mutable limits.

## Decoder accounting contract

The new explicit primary policy is `supervisor-decoder-v1`. For the exact
development stream, expose `V + B` and rank by `V + B + A + D`, where:

- V is the complete compressed archive, including all framing and names.
- B is the actual compiled decoder executable(s) needed to decode it.
- A is all other decoder-required dictionaries, tables, config and artifacts.
- D is the decoder's nonplatform native dependency closure, charged once.

Source/build assets, encoder-only artifacts and binaries, research manifests,
Python/MCP infrastructure and installation tools are disclosed separately.
They do not become decoder runtime charges merely because they are distributed.
If one binary implements both encoding and decoding, its entire size is B.
The shared platform allowlist remains explicit and hash-pinned. Installed-codec
incremental views are supplementary and must not silently alter this primary sum.

Introduce native manifest v2 decoder role membership, preserving v1 loading for
old contracts. Derive ELF dependency closures from built files rather than
trusting declared byte counts. Build a decoder-only runtime containing exactly
the charged files. Every decode correctness, independence and corruption gate
must run from that runtime with no encoder, training inputs or engine mounted.
Rebuild and sanitizer runtimes obey the same role separation. Export that decoder
bundle with a complete hash inventory and a standalone reproduction command.
An undeclared required file must fail decoding, not be borrowed from host state.
Generate a canonical `decoder-command.json` inside each v2 decoder runtime and
charge its physical bytes in A. Execute decoding from that descriptor, so literal
command arguments cannot carry uncharged tables or configuration. A missing or
changed descriptor fails verification; the full research manifest is not a
fallback configuration source.

## Timing and comparison contract

Full static correctness covers every public train/development object, required
arbitrary-byte fixtures, deterministic/reordered/subset independence, directory
parity, bounded corruption rejection, reproducible build and supported native
diagnostics. Report diagnostic scope honestly for each supported language.
For C++, require independently instrumented encoder and decoder binaries using
ASan and UBSan. The available pinned stable Rust 1.95.0 path instead uses explicit
debug assertions and overflow checks, with all conflicting codegen switches
rejected. Label this `rust-checked-v1`; it does not provide ASan, UBSan, or coverage
of unsafe-code memory errors. Pin the compiler and complete used sysroot tree.
C++ diagnostic invocations expose only live read-only `/proc/self/maps` from
their private PID namespace, to support sanitizer main-stack discovery and
exception unwinding. The temporary full proc mount is removed before exec.
Host processes, file descriptors and environments remain unavailable; regular
timed native execution keeps an empty proc directory. Do not suppress sanitizer
errors; real stack/heap corruption must still fail.
Training uses train bytes only; cheap screens remain train-only and ineligible.

Primary timing uses the complete development workload, seven fresh native
process invocations in each direction, externally measured elapsed time including
namespace startup and native I/O. Its numerator is exact development bytes.
No startup subtraction, warm-cache speed multiplier, invented multicore scaling
or selective trial deletion. Every timed output must match the exact archive or
original hash. Keep historical combined train/development timing fields distinct.
The minimum encoding floor is 100,000,000 input bytes/s under the declared primary
multicore allocation; 150 MB/s single-worker is an aspiration, not a gate.
All declared conventional controls remain visible even when slow or failed.

Extend the existing MCP/CLI baselines operation into one complete comparison
table, including supported recipes and missing/queued/failed/measured states,
candidate position, independent size/speed ranks and gaps. Show exact bytes,
V/B/A/D costs, ratio/bits per byte, encode/decode throughput, resource profile,
observed memory/tasks, exactness/eligibility and immutable result IDs. Never rank
incompatible cards, resource/timing/accounting policies or screens together.

## Native controls and dependencies

The declared conventional families are stored, Zstd, LZ4, Brotli, XZ/liblzma,
zlib and bzip2. Use representative fast and ratio settings and train-only Zstd
dictionary variants. Include native one-worker reference controls and explicitly
named independent-chunk parallel controls. The separate native side-task spec
defines their new C++ source, bounded deterministic chunk format and worker API.
No candidate is forced to use this format: the model may choose any correct
native algorithm, library or hybrid and C++ or Rust with supported diagnostics.

Pin official source archives, hashes, build flags, licenses and resulting runtime
files. Stage pinned libraries project-locally; do not replace unrelated system
libraries. Inspect both current upstream and tested versions and label any
difference. Provide a working offline package or verified reproducible installer.

## Failure modes and acceptance

Missing tools, unsupported diagnostics, failed library downloads/checksums,
bad resource profiles, OOM/timeouts, undeclared files, decode failures, stale
fingerprints, incompatible comparisons and noisy timings remain explicit failures.
Never transform one into a speed or generalization claim. Tests must cover the
load-bearing new boundaries, not duplicate implementation details. Then perform
clean wheel installation, real stdio and authenticated HTTP MCP, representative
native full gates, the complete declared validation baseline matrix and the one
native Prime Sol xhigh session. Frozen old artifacts remain untouched.

## Sources and design rationale

The maintained primary-source bibliography is
`research/inputs/MORNING_PRIMARY_SOURCES_2026-09-07.md` in the parent repository.
AnyBlox (PVLDB 18(11), 2025, DOI 10.14778/3749646.3749672) supports the relevance
of packaging decoders with data; this release does not adopt its Wasm execution
architecture. Official GPT-5.6 guidance motivates concise outcome/constraint/
evidence prompts with design before implementation and no unnecessary agent
framework. User requirements, exact accounting and immutable evidence are the
binding constraints; recommendations in downloaded packages are review inputs.

## 0.4.1 full-size corruption memory correction

The first 100 MB stored control exposed an evaluator OOM: eagerly retaining
all malformed archives exceeded its 3 GiB enclosing cgroup before timing.
Generate the same deterministic corruptions one at a time, with identical byte
content, order, seed, 35 normal cases on the fixed regression payload, and the
first 12 diagnostic cases. Release validated temporary comparison copies before
continuing. Preserve all rejection rules, input sizes and native resource limits.
The original failed run remains evidence; new measurements use a newly
commissioned runtime identity. This changes control-plane memory use, not the
compression format, accounting, native resource budget or timing definition.
