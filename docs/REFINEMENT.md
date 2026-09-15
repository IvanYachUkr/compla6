# Scoped refinement memory

`compression_lab.refinement.RefinementStore(engine)` stores measured public
comparisons alongside **unverified agent interpretations**. It never executes a
procedure, evaluates a candidate, changes a codec, or makes a model call. The
engine's committed public results remain the authority for measurements.

## Entry contract

```json
{
  "id": "dictionary-size",
  "kind": "observation",
  "interpretation": "A smaller dictionary may explain the observed size reduction.",
  "evidence_result_ids": ["r-REFERENCE", "r-CANDIDATE"],
  "claim": {
    "metric": "package_bytes",
    "relation": "lower",
    "reference_result_id": "r-REFERENCE",
    "candidate_result_id": "r-CANDIDATE"
  }
}
```

Replace the placeholders with committed public result IDs. The only optional
field is `procedure`, required exactly when `kind` is `procedure`:
`{"candidate_digest": "DIGEST", "path": "codec.c"}`. The file must be a declared
source of the claim's registered candidate. Registration digest, source inventory,
manifest hash, and file hash are checked through reads that reject symlinks and
hard links. The returned reference includes its file hash and byte count; it
grants no execution authority and does not certify the whole candidate runtime.

Metrics are `package_bytes`, `encode_bytes_per_second`, or
`decode_bytes_per_second`; relations are strictly `lower` or `higher`. Package
bytes use the engine's `rank_cost`, including the current accounting policy's
deployment costs. Throughput uses recorded encode/decode medians. Ties do not
support either strict relation. Supplied metrics, extra fields, and nonfinite or
negative measurements are rejected with `compression_lab.util.Error`.

## API and evidence scope

- `propose(entry)` returns a flat lesson record with `status: provisional`,
  separate `claim` and `observed` objects, version, content digest, source IDs,
  and scope. A failed numeric claim is recorded, not treated as invalid input.
  An identical proposal is idempotent; a changed proposal creates another
  provisional version. Rephrasing an unchanged refuted claim is rejected.
- `check(lesson_id, [reference_result_id, candidate_result_id])` requires exactly
  two new IDs, in that order, for the original reference and candidate digests.
  IDs used anywhere in that lesson's history cannot be reused. A supporting
  repeat sets `supported_within_scope`; a contradicting repeat sets `refuted`.
  A refuted version cannot be checked again. Revising the structured claim or
  using explicit owner rollback is required to continue that lesson.
- `list(limit=8)` returns compact `entries`, current `context`, exclusion counts,
  and a notice that interpretations are untrusted. It skips refuted, unsupported,
  stale, and context-mismatched records. Interpretations are truncated at 1,200
  characters with an explicit flag. `limit` must be an integer from 1 to 32.
- `rollback(lesson_id, version)` restores the selected version **as a new
  version**, preserving the append-only history. Both current and restored
  scopes must match the engine. Expose this operation only through the owner CLI;
  the class itself does not authenticate callers.
- `snapshot()` returns a content-addressed pin of the current retrievable set:
  `digest`, `context`, `entry_versions`, and `entry_digests`. Refuted and expired
  lessons are absent. It contains public identities, not data paths or private
  metadata. The returned dictionary is a detached snapshot; the caller stores it
  with an experiment arm. Historical lesson versions remain in their journals.

Every evidence ID is resolved through `engine.raw()`. Results must be full,
quality-passed, public, and explicitly match the current primary resource
profile, card, runtime, workload, timing scope, and accounting policy. The scope
also includes a digest of the public manifest identities. Metadata is checked
without reading private data or repeating a benchmark. Full payload validation
belongs to the engine's evaluation, not this advisory store. Retrieval rederives
the comparison from public results and rejects changed procedure files.

Neither a supporting comparison nor a repeat validates the explanation. Every
version retains `interpretation_status: unverified_agent_interpretation` and
`trusted_policy: false`. This is scoped public repeatability evidence, not
universal causality or a new held-out generalization result. MCP consumers must
preserve these labels and treat interpretation text as untrusted content.

Each lesson uses `engine.root/refinements/<id>/events.jsonl` through the existing
`Ledger`, with the stage kept at `public_search`. A store lock serializes
mutations and snapshots. A version records its previous content digest, operation,
and, for rollback, restored version. Bounds are 128 lessons, 64 versions per
lesson, 2–8 proposal evidence IDs, 4,000 interpretation characters, 96 KiB per
record, and 2 MiB per referenced source file. Full stores and version histories
fail explicitly; the store never silently prunes evidence.

## Focused verification

```sh
PYTHONPATH=src:tests .venv/bin/python -m unittest test_controller_refinement -v
```

Tests cover scope and provenance rejection, fresh support/refutation, retained
versions and rollback, source path guards, and retrieval that rejects forged
memory metrics. A real `Engine` integration commits fixture measurements through
its normal public result path; it performs no compression benchmark. These tests
do not measure whether an LLM benefits from refinement memory.
