# Relational workload extension implementation plan

Base: compression-lab 0.1.0, release ZIP SHA-256
3ebf241d2081fb98ca84041bbbb504fd1184b108f0a6b28d85179edcf8a4403c.
The user's authoritative follow-up is the implementation contract. No new
orchestrator, MCP SDK work, package rebuild, log benchmark reruns or private data.

## Architecture and integration constraints

New modules live under `compression_lab.workloads`. Existing jobs, budgets,
journals, digest verification and terminal latches remain authoritative. A small
bridge selects the evaluator and candidate inventory from a public dataset card.
Static RLB1 objects use the existing native reversible byte-stream interfaces.
Mutable candidates use bounded JSONL in one isolated persistent process, with an
independent evaluator state and append/fsync request/ack ledger outside the mount.
A Python durable reference candidate is standalone, not imported by the oracle.

## Work packages

- [ ] Bundle: lexical CSV scanner, schema/keys/derived fields, RLB1 framing,
      synthetic fixture builder; tests for exact bytes, null/empty/quotes,
      terminators/order, integer boundaries and invalid frames.
- [ ] Runtime: explicit immutable Python/native runtime inventory, bounded
      persistent subprocess, timeout/cancel/process isolation, metrics sampling;
      tests for hostile protocol replies, declarations and changed snapshots.
- [ ] Store and oracle: independent append-only reference, token receipts,
      WAL/checkpoints, atomic multi-table inserts and exact queries/exports;
      tests for normal operation, rejection atomicity and unsupported operations.
- [ ] Crash suite: actual SIGKILLs and deterministic I/O fault hooks; two required
      multi-recovery sequences, pending request resolution, orphan cleanup,
      monotonic metadata, short writes/fsync/rename failures; broken controls for
      counter reset, generation collision, bad continuation, duplicate export,
      partial transactions and wrong joins.
- [ ] Engine bridge: workload selection for evaluate/results shared by CLI/MCP,
      compare and advisory resume, export evidence and workload-specific gates.
- [ ] Verification: one-command model-free demo; regression suite; raw logs and
      inventories; incremental overlay, exact-base patch and preserved originals.

## Fixed policies

100,000,000 B/s is a static **encoding-only** floor. Mutable import and operation
latencies have no throughput pass/fail floor. Cache procedures are explicit;
reopen means a fresh process, not a cold OS page cache. Every timing sample is
kept. Process-crash validation is never power-loss certification. Python, WAL,
indexes, receipts, temporaries, source, binaries and dependencies are inventoried.
Unsupported claims fail closed. Evidence is append-only and public/synthetic.
