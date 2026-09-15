# Relational workloads in Compression Lab 0.2.1

The Pro increment is integrated into the installed wheel and shared CLI/MCP engine.
The original 0.1.0-relational.1 archive is unchanged; its private-unsupported status
is historical.

Local full acceptance passed static RLB1 exactness/quality and 54 mutable cases:
twelve regression scenarios on each of two seeds plus five repetitions of three
operation-length/merge-count schedules per seed (30 measurement trials). The oracle
checks exact state, point/foreign-key reads, joins, lexical export/order, every
acknowledged transaction, rejected-bundle atomicity, token replay/conflict and
monotonic head/checkpoint/generation across subsequent work. External SIGKILL and
short-write/fsync/rename faults exercise recovery and the next transaction/merge.
Six deliberately broken stores were all detected.

The local audit added bounds and adversarial lexical/schema/null/empty coverage,
longer composed schedules, query-response limits, process-observer exit handling,
runtime accounting checks and descriptor-based path boundaries. A protected 0.2.0
suite plus one corrected MCP assertion rerun verifies 109 distinct engine cases.
0.2.1 changes only version/shared instructions; module equivalence is recorded.

Both workload families support owner-only freeze/private/disclose. A real
separate-UID synthetic mutable lifecycle completed a fresh full public recheck,
one private attempt and disclosure, with `private_eligible=true` and repeat denied.
No old IMDb held-out corpus was used. This validates the bounded workflow and
process-crash contract, not a new research result.

The full reference is Python; interpreter/extensions/dependencies are charged.
C++ protocol/sanitizer controls also ran. Python and external libraries were not
rebuilt with sanitizers. Logical/allocated store bytes, runtime, WAL/snapshots/
metadata, RSS and evaluator-observed latency are separate measurements. Kernel
write counters are not physical-drive amplification. OS caches are not dropped;
polling can miss transient peaks.

Power loss, full IMDb scale, arbitrary SQL, updates/deletes, multi-writer operation
and a production C++ store are outside the contract. The static 100 MB/s encoding
floor is never applied to mutable transaction rates.

Evidence under `results/local-integration/`:

- `relational-acceptance-0.2.0-r2/ACCEPTANCE.json`, public raw trials and negative controls;
- `owner-mutable-0.2.0/summary.json`, aggregate synthetic owner receipt;
- `protected-wheel-unittest-0.2.0.log`, `protected-mcp-path-rerun-0.2.0.log`;
- `final-functional-code-equivalence.json`;
- `oci-acceptance-0.2.0-r3/acceptance/summary.json`, actual static/persistent OCI checks;
- `final-prime-sdk2-worker-probe-r2.json`, actual final-service worker through MCP.

See [CONTRACT.md](CONTRACT.md) and [current usage](INTEGRATION.md).
