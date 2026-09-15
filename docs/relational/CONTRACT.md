# Relational workload extension: contract v1

This contract defines the static relational and mutable-store workload interfaces.

## Workload and engine boundary

Dataset cards select exactly one workload:

| Workload | Adapter | Candidate interface | Throughput eligibility floor |
|---|---|---|---|
| `independent_objects` (default for old cards) | `bytes` or `hcb1` | Existing native stream/directory commands | Static encoding only: 100,000,000 bytes/s |
| `static_relational` | `relational_bundle` | Existing native commands, `opaque_bytes` or `rlb1-lexical` input domain | Static encoding only: 100,000,000 bytes/s |
| `mutable_store` | `relational_bundle` | Version 1 persistent JSONL process | None for import, transactions, merges or queries |

`Engine.evaluate(..., workload=None)` derives selection from the immutable card.
An explicit selection is a cross-check, not permission to change the workload.
CLI and MCP call that same function. The existing durable worker calls
`workloads.bridge.evaluate`; reservation, budgets, cancellation, reconciliation,
terminal latches and immutable result IDs stay in the original engine.

The mutable objective reports actual stores after a specified sequence. It does
not scale a tiny database to the static deployment horizon. Full mutable results
can become eligible for the bounded process-crash contract after full gates on a
benchmark-role card. The separate owner copies/rebuilds the candidate, reruns public
gates, then executes one private attempt with the same protocol. Public training
hashes remain frozen; private evidence is not exposed through MCP. Power loss is
not certified. Tiny synthetic acceptance is not production-scale qualification.

## Static relational bytes: RLB1

Integers in the framing are unsigned little-endian:

```
4 bytes     magic = RLB1
u32         exact UTF-8 schema JSON byte count
N bytes     schema, including whitespace and member spelling/order
u32         table count
repeat table count times, in declared order:
  u16       UTF-8 table name byte count
  N bytes   exact table name (not a filesystem path)
  u64       lexical table payload byte count
  N bytes   exact table bytes
32 bytes    SHA-256 of every preceding byte in the bundle
```

Bounds: 64 MiB complete bundle, 1 MiB schema, 64 tables, 128 columns per table,
1,024 UTF-8 bytes per table name, and one million rows per table. Frames must
contain exactly the declared tables in exactly the declared order, without
trailing bytes. UTF-8 table names can contain CR/LF and are not normalized into
paths. The original outer HBI1/HBA1 transport continues to preserve stream aliases,
including the original one-MiB alias boundary.

Schema version 1 uses `csv-lexical-v1`. It declares ordered tables, ordered
columns (`int64`, `uint64`, `text`, `bytes`), nullability, optional primary keys,
foreign keys referencing primary keys, and optional derived constraints:
`copy(source)` or `prefix_id(source,prefix,width)`. Integers admit lexical `+`,
leading zeroes, and `-0`, while enforcing signed/unsigned 64-bit value bounds.
Text is UTF-8; `bytes` cells can contain all byte values when correctly quoted.

The lexical scanner, rather than a normalizing CSV writer, owns table parsing:
commas delimit fields; double quotes enclose fields and doubled quotes escape a
quote; CR, LF and CRLF terminate records outside quotes. Embedded quoted CR/LF is
data. A missing final record terminator is valid. Unquoted `\N` is null;
quoted `"\N"` is a literal string. An empty field and `""` have the same empty
logical value but different source bytes. Empty tables are valid.

**Static correctness is whole-bundle byte equality.** It includes schema JSON,
all framing, every source lexeme, quoting, null marker, table/row order and
terminator. Typed-value equality alone is insufficient. A derived field can be
logically reconstructible while its quote/number spelling still requires a
residual; the engine does not waive that residual's storage cost.

The synthetic fixtures include title IDs in source order `7,2,9`, lexical `007`,
`+0000`, `-0`, signed integer minima/maxima, unsigned maximum, null versus empty,
copy/prefix fields, quotes, embedded CR/LF, and mixed/missing terminators.
Seventeen relational encoder fixtures are valid RLB1, not arbitrary malformed
bytes disguised as valid relational inputs. Archive corruption is tested
separately using the existing native gate.

## Mutable candidate manifest and execution

Mutable candidates provide `store_candidate.json`, not a static
`candidate.json`. Generate a complete, host-pinned manifest rather than copying a partial runtime lock:

```python
from pathlib import Path
from compression_lab.workloads.registry import create_reference, create_python_candidate

# The supplied tiny durable store, with its complete local runtime lock.
create_reference(Path("reference-store"))

# An independent Python implementation of the protocol below.
source = Path("my_store.py").read_text(encoding="utf-8")
create_python_candidate(Path("custom-store"), source, "my-store")
```

The generated `store_candidate.json` declares `schema_version:1`,
`workload:"mutable_store"`, `protocol_version:1`, the candidate ID, language,
single-worker execution, determinism, source/artifact/runtime paths, launch
command, `durability_claim:"process_crash_only"`, and a complete `python_runtime`
lock object. The launch command uses `{python} -I -S -B {runtime}/program.py`.
Complete generated manifests accompany acceptance evidence. Registration refuses
exploratory execution before building; missing isolation never silently falls
back to an ordinary host subprocess.

Python uses a trusted distro `/usr/bin/python3`, never an agent-supplied
interpreter for host-side discovery. Interpreter, stdlib files, declared dynamic
extensions and non-platform ELF dependencies are copied, hash-pinned and charged.
Site packages, customization modules and implicit user modules are excluded;
execution uses `-I -S -B`. The supplied store needs no PyPI dependencies. Tested
runtime facts are recorded in the acceptance inventories, not inferred from a
package name or version alone. The initial supported layout is distro
`/usr/lib/pythonX.Y`, not arbitrary venv/Conda/embedded runtimes.

C/C++ manifests instead supply `native_build` with the existing explicit normal
and ASan/UBSan recipes, output paths, executable, toolchain and dependency locks.
They launch `{runtime}/EXE` as one long-lived process. Registration and full clean
rebuild reuse the original native builder. Native diagnostic runs require actual
ASan and UBSan runtime linkage. The included native protocol probe and deliberately
invalid memory access test that transport/diagnostic path; **the complete durable
reference store is Python, not a C++ database implementation**. Rust mutable
certification is unsupported in this first extension.

Registration inventories complete source and runtime trees. Candidate identity
includes the exact source, build outputs, language runtime, dependencies, protocol
and workload. Full evaluation rebuilds or rematerializes the same source and
requires an identical candidate/build key. Re-registering under a changed local
runtime produces a new identity; evidence from another runtime is not reusable.

## Isolation

Each persistent process gets its own Linux user, mount, PID, network, IPC and UTS
namespaces, chroot, capability drop, `no_new_privs`, seccomp and one-CPU affinity.
Only its declared read-only candidate/runtime files and platform libraries are
mounted. The entire root is remounted read-only; `/output` is the only writable
regular-file mount. Temporary files must be placed in `/output` and are charged.
Network, forks/threads, namespace escapes and privileged inspection syscalls are
denied. The evaluator's reference state, request/ack ledger, source dataset paths
and result directory are not mounted. Tests attempt real host-file, network,
fork and unaccounted-root writes and require denial.

The new `_persistent_sandbox.py` reuses trusted mount/filter/capability helpers
from the shared native sandbox implementation. A
parent-death guard and process-group kill clean up persistent processes. Time,
address-space, file-size and descriptor limits apply. Observed RSS/space polling
and operation-boundary inventories add limits but are **not hard cgroup peak
proofs**. Unlinked open regular files are counted through descriptor inventory;
brief transients and anonymous/shmem use still have measurement limitations. Kernel vulnerabilities and a malicious evaluator host
are outside this boundary. The private-owner lifecycle additionally requires a separate identity and a 0700
tree. This is never a power-failure certificate.

## Bounded wire protocol

Requests and responses are UTF-8 JSON, exactly one object followed by one LF.
No log messages are allowed on stdout. At most one request is in flight.
Request size is at most 1,048,576 bytes including LF; response size at most
8,388,608 bytes; decoded export/query blob at most 5,242,880 bytes. A build seed
must fit inside one bounded request after base64 encoding. There is no streaming
multi-request import in version 1.

```
request: {"version":1,"id":1,"op":"hello","args":{}}
success: {"version":1,"id":1,"ok":true,"result":{...}}
failure: {"version":1,"id":1,"ok":false,
          "error":{"code":"recovery_required","recovery_required":true}}
```

IDs are positive non-boolean integers below 2^63, strictly increasing within a
process. Version/shape/ID mismatches, duplicate JSON keys, nonfinite numbers,
noncanonical base64, oversized output, trailing extra responses and timeouts are
rejected. Stderr is separately bounded. Candidate errors may include a bounded
human-readable `message`. A failed response never counts as an acknowledgment.

| Operation | Arguments | Successful result |
|---|---|---|
| `hello` | `{}` | Version, bounds, capabilities, available fault points, `power_loss_certified:false` |
| `build` | `{"bundle_b64":"..."}` | Initial durable store metadata; only on an empty store |
| `open` | `{}` | Recovered durable store metadata |
| `transaction` | Token and ordered record list, below | Metadata, `txid`, `replayed` |
| `point` | `{"table":"titles","id":7}` | QRY1 bytes as `bytes_b64`, complete row count |
| `join` | `{"kind":"title_entities","id":7}` or `episodes` | QRY1 bytes and complete row count |
| `merge` / `checkpoint` | `{}` | Metadata after a new durable generation/checkpoint |
| `export` | `{}` | Exact RLB1 bytes as `bytes_b64`, byte count and SHA-256 |
| `stats` | `{}` | Metadata, receipt information, recovery state, optional accounting descriptions |
| `fault` | `{"point":"wal_short_write","mode":"error"}` | Armed point/mode; test interface only |
| `close` | `{}` | Metadata; successful response, then clean process exit |

Metadata fields `head`, `generation`, `checkpoint` are bounded unsigned integers.
Head starts at zero and increments exactly once for each new committed token.
Generation increases on each successful merge; checkpoint cannot exceed head or
regress. Token replay returns the original transaction ID with current metadata.
A token reused with different exact request contents is rejected.

```
{"token":"insert-001","records":[
  {"table":"titles","record_b64":"BASE64 OF ONE EXACT LEXICAL ROW"},
  {"table":"entities","record_b64":"..."},
  {"table":"episodes","record_b64":"..."},
  {"table":"relationships","record_b64":"..."}
]}
```

One to 128 records, at most 256 KiB decoded per record and 512 KiB decoded per
transaction, subject also to the one-MiB wire bound. The movie schema is fixed by
`fixtures.movie_schema()` and independently checked by the oracle and candidate.
FK validation happens on the entire staged bundle, allowing references to rows
inserted in that transaction. Existing normalized primary keys cannot be inserted
again. Relationships to existing rows and new episode titles are supported.
Updates, deletes and concurrent writers are explicitly unsupported.

Canonical inserts append raw records in transaction order to their named tables.
If a nonempty table's last record has no terminator, append exactly one LF before
the first new record; otherwise append no separator. Existing bytes are otherwise
unchanged, and new records retain their own terminators. This rule is part of the
state contract, including the resulting terminator of the previous final row.
There is no implicit sorting by IDs or regeneration of CSV spelling.

### Exact query framing

QRY1 is `b"QRY1" + u32(row_count)` followed by each row's `u16(name_bytes)`, UTF-8
table name, `u64(raw_record_bytes)` and the exact record bytes. Point returns zero
or one row. `title_entities` returns the selected title first, then each matching
relationship followed by its entity in relationship source order. `episodes`
returns the selected series title first, then each matching episode followed by
its episode title in episode source order. Missing titles return zero rows.
Duplicates arising from different matching relationships are preserved. These
are explicit query projections, not unspecified SQL ordering.

## Independent oracle and acknowledgment ledger

`oracle.py` uses the evaluator's lexical bundle parser and its own staging,
foreign-key checks, query serializer and state transition logic. It never imports
candidate `reference_store.py`, never asks the candidate to validate expected
answers, and never trusts candidate counts/hashes in place of comparing bytes.
The candidate is a standalone program with independently implemented parsing,
validation, framing and persistence.

Before writing a request, the evaluator appends and fsyncs it to a hash-chained
ledger outside the sandbox. Only an actually received valid response is recorded
as its response event. Unanswered requests remain in the ledger after a kill.
An operation can become durable without its reply reaching the evaluator, so
recovery resolves that uncertainty by replaying the token. This is not treated
as acknowledged-data loss or silently discarded. Rejected transaction checks
compare the entire database export and metadata, not only row counts.

## Reference durability mechanism and tests

The reference is deliberately small: exact lexical tables and indexes in memory,
checksummed append-only WAL records, fsync before acknowledging, snapshots plus
an atomically replaced `CURRENT` manifest, directory fsync, and an exclusive file
lock. Snapshots retain monotonic counters and all token receipts. Each WAL header
binds generation, checkpoint head and snapshot digest; each complete record chains
a checksum. Only an incomplete final WAL record is truncated and synced.
A complete bad checksum or stale generation is refused, not repaired into an
empty database. Only `CURRENT` selects live state, never the largest filename.
Recognized uncommitted/temp generations are cleaned before subsequent merges.

Every full mutable evaluation runs these sequences for every public seed:

- Basic atomic bundle/episode/relationship inserts, queries, rejection, token
  replay/conflict and exact export, followed by checkpoint/reopen.
- Merge → close → open → acknowledged insert → close → open.
- External idle SIGKILL; external SIGKILL during merge before and after manifest
  commit; external SIGKILL during a short WAL write and after WAL fsync. Each
  recovery is followed by a new transaction, merge, close/open and full checks.
- Injected short WAL write, WAL fsync, snapshot fsync, manifest rename and directory
  fsync failure. Candidate must explicitly refuse until recovery or continue
  safely; any subsequently acknowledged transaction is checked after recovery.

Named pause hooks expose a synchronized point; **the evaluator sends actual
SIGKILL**. Idle kills do not use hooks. Error hooks inject errors at persistence
operations but do not emulate a drive/controller/power cut. Additional tests
externally perturb WAL tails, complete checksums, snapshot bytes, stale generation
headers and orphan/temp files, and open a second writer process. Six independently
registered source mutations must be detected: counter reset, stale generation
collision, continuation after partial WAL append, duplicate export, partial staged
transaction, and wrong join key.

**Process-crash validation is not power-loss certification.** A kernel process
kill leaves the kernel page cache and storage stack alive. Hooks plus fsync calls
are useful evidence, not proof about filesystem write reordering or real hardware.
No power-cut, VM-reset, block-device fault model, hardware flush or torn-sector
certification is performed. Manifests claiming such certification are rejected.

## Measurements and accounting

Raw results keep every case, operation, success/failure, process return code,
external kill, request/response ledger, source/runtime inventory and measurement
sample. Operation latency uses evaluator monotonic time from first request write
to complete response receipt. It excludes evaluator oracle work, ledger fsync,
profiling and filesystem scans. Spawn-to-first-reply is reported separately.
Measurements are single-worker and serial, not Python codec-binding proxies.

Each clean trial creates a new process and store, imports the seed, inserts an
atomic bundle, issues point/join reads, merges, inserts a new relationship, merges
again, exports, closes, reopens and checks point/join/export. Distributions retain
all samples and nearest-rank p90/p95/p99, medians and MAD without trimming. Object
aliases and merge rounds remain separate. Correctness/fault/diagnostic requests
remain available but are not pooled into clean measurement distributions.

The page cache is **not dropped**. Labels distinguish a fresh process and warm
requests; open itself may populate all indexes/data. A fresh process is not a
cold disk. All trials use the same explicitly documented procedure.

Complete file inventory counts WAL, snapshot, metadata, index, receipt/ack,
temporary and other files regardless of descriptive filename classification.
It reports logical and allocated blocks. The reference's receipts are embedded
in WAL/snapshots and charged there once; its indexes are in RAM and included in
RSS, not falsely reported as durable zero-cost indexes. The evaluator's own
request/ack ledger cost is reported separately as evidence overhead, not a decoder
runtime dependency. Fixed artifacts, launch manifest, packed/raw source, native
binary or Python program, interpreter/stdlib and non-platform libraries are all
reported, with complete deployment and source-primary totals.

Write amplification reports per-trial Linux `/proc/<pid>/io` `write_bytes`
deltas across transactions and both merges divided by the independently measured
canonical growth. It is **kernel-accounted write amplification**, not application
`write()` byte totals, filesystem journal accounting, physical drive writes or
NAND amplification. `wchar` includes stdout and is never substituted for disk
writes. Missing counters produce null, not fabricated estimates. Final store
inventories and observed merge peaks are retained; polling can miss transients.

Tiny inputs are correctness fixtures, not performance evidence for IMDb/Hadoop.
No log benchmarks, corpus download, hidden data, model calls or live agent-host
integrations are performed by this acceptance command.

## Local measurement and query bounds

Cards may declare one to four unique `measurement_sequences`, each with
`transactions` (1..256) and `merges` (1..transactions). Defaults are 2/2, 8/2 and
16/4; each sequence runs per object and repetition in a new store. Results keep
sequence lengths separate, rank the longest, sum development stores, and add
shared deployment cost once. These are measured stores, not a projected database.

Point/join responses have a 5 MiB decoded frame limit. A larger valid result must
return `query_output_limit` without changing or poisoning the store. Oracle and
reference preflight size before concatenation/base64 encoding. Empty seeds and
seeds colliding with demonstration IDs receive deterministic unused insert IDs.
