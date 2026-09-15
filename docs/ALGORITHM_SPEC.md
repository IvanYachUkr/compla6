# Version 0.4 contract

For new 0.4 workspaces, [MORNING_0_4_SPEC.md](MORNING_0_4_SPEC.md) is the governing implementation contract. The historical sections below remain provenance for earlier releases.

# Compression Lab local integration contract

The static codec, accounting and isolation contract is in `CANDIDATE_ABI.md`
and the public workflow in `../src/compression_lab/data/SKILL.md`. `relational/CONTRACT.md` specifies RLB1
and the bounded insert-only movie-store protocol. This addendum defines local
integration changes before their implementation. Original downloaded releases
remain immutable evidence; the installed local release has its own version.

## Optional paired development diagnostics

`compare(..., diagnostics=True)` accepts exactly two quality-passed full static
results with matching card, runtime, workload and depth. The first is the
reference; every delta is second minus first. Only committed immutable public
evidence is read. Quick, failed, mutable, empty-development and unpaired results
are rejected for this mode; the existing aggregate comparison remains available.

Pair objects by alias, exact canonical byte count and group, using development
objects only. Reuse the seeded 1,000-draw paired group bootstrap for archive
relative deltas. Return at most five regressing objects and five best/worst
groups, group count and largest group's canonical-byte share. This is descriptive
feedback from reused public development data, not independent test evidence.
Bootstrap estimates exclude fixed package costs and shared batch framing.

Decompose the development deployment-horizon total delta into projected archive,
artifact/runtime, manifest config, executable and non-platform dependency bytes;
these components must add exactly to the total. Report packed source separately.
Show speed headroom and relative MAD from the existing seven-trial aggregate
public timing, explicitly labeled train-plus-development with process/startup/I/O
included, warm-cache/no-fsync. Do not imply development-only throughput, rank a
winner, change eligibility, run a model, or consume another evaluation.

## Workload routing and evidence

Registration, worker execution, owner copying, full public rechecks and private
execution dispatch from the frozen dataset card and the declared candidate
interface. Static byte candidates and persistent store candidates cannot be
interchanged. Native and store registration inventories are verified after a
no-follow owner copy. Only declared runtime executables receive execute bits.
Result IDs bind run, candidate, card, runtime, workload, depth and track to a
committed job. Completion is idempotent and charges an attempt only once.

Public and private mutable execution use the same gates. A full benchmark-role
mutable result can be eligible for the bounded process-crash contract after
all oracle, isolation, rebuild, recovery and measurement checks pass. Eligibility
does not certify power loss, multiple writers, updates, deletes, arbitrary SQL,
production-scale throughput or cold OS caches. No static 100 MB/s floor applies.

For mutable ranking, compare the sum of development stores after the longest
predeclared operation sequence plus the candidate deployment cost once. Report
all shorter sequence results as well. Static ranking keeps its predeclared
canonical-byte deployment horizon. Neither rank is selected from private scores.

## Generic movie inputs and operation schedules

Every valid RLB1 movie schema must work, including empty tables, missing familiar
fixture IDs, and collisions with demonstration insert IDs. Evaluator-created
transaction payloads choose deterministic unused IDs from the seed and preserve
their identities across replay/reopen/crash. Foreign keys refer to seed rows or
rows in the same atomic transaction. Rejection controls choose real duplicate
keys and absent foreign keys from the actual state, never assumed fixture IDs.

The card may declare measurement sequence lengths and merge counts; defaults
are 2 inserts/2 merges, 8 inserts/2 merges, and 16 inserts/4 merges. Each is run
for every seed and every declared repetition in independent stores. Merge
positions are deterministic and numbered. Latency, canonical growth, complete
store costs and kernel-accounted writes remain separate by sequence and object.
The same schedules are frozen for owner rechecks and private evaluation.

## Owner single-use lifecycle

Freeze copies and verifies public input and the candidate, then performs fresh
full public gates under the private owner identity. It retains public training
hashes for reproducible builds; private rows are never treated as training.
After freeze, the private-started journal transition is fsynced before any
private manifest/object read. Private RLB1 bytes receive the same format/schema
validation as public RLB1 bytes. Private evidence is marked private and remains
inside the owner-only directory; it is never exported through public MCP.

A crash consumes the same private attempt. Resume either recovers the already
saved outcome or marks that attempt interrupted without reading private input
again. There is no mid-gate replay or second adaptive score. Disclosure returns
aggregate cost/latency/status only, with adaptive reuse explicitly forbidden.

## Required falsification checks

Preserve existing native regression tests. Add failures before fixes for a
valid movie seed with arbitrary/empty IDs, quick comparison with no development
projection, a later ineligible result replacing resume's eligible export,
mutable owner copying and adapter validation, and private evidence/training
context. Verify all operation schedules and cost comparisons at equal horizons.
Run the full merged suite and complete synthetic relational acceptance command,
then one real protected owner lifecycle. Synthetic checks validate infrastructure;
they do not become new held-out compression research evidence.

## Bounded query output and retained file handles

Point/join frames are limited to 5 MiB decoded. Both oracle and reference store
calculate frame size before concatenation or base64 encoding. Oversized queries
return the explicit `query_output_limit` error and leave the store usable and
unchanged; the evaluator verifies that behavior. This is a bounded-result API,
not an arbitrary-size SQL interface.

The persistent observer counts regular store files retained by unlinked open
file descriptors as well as directory entries. An active process with unreadable
FD inventory fails closed. Linux task state Z/X or PF_EXITING is recognized to
avoid killing healthy parents during child teardown. Polling still measures
observed peaks and is not a kernel aggregate disk quota; file-size and descriptor
limits provide additional ceilings.

## Filesystem import boundary

Registration opens every source directory component and file with no-follow
descriptors before parsing or copying bytes. It checks the declared file set,
rejects links and special files, and retains the 20,000-file/512 MiB source cap.
The public MCP path cannot enter a linked parent directory outside its workspace.
Owner initialization and every lifecycle entry reject descendant symlinks,
hardlinks, special files and files/directories owned by another identity before
opening private input or writing evidence. The trusted owner controls the 0700
tree after that metadata check; an agent cannot race its descendants.
