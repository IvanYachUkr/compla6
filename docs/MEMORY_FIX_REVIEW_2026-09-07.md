# Morning 0.4.1 corruption-memory review

**Scope:** Read-only review of the staged lazy corruption-case change on
2026-09-07. This is implementation evidence, not a compression result.

## Finding

The change preserves the previous 35 malformed-archive cases and their order.
`corruption_cases` yields the six framing cases, two header mutations, twelve
payload-bit mutations, seven truncations/extensions, and eight seeded random
inputs in the same sequence. The sanitized path still executes the first 12
cases, matching the former slice of the eagerly built list.

The old retention failure is addressed: the generator holds only its current
mutation instead of retaining all complete archives. The explicit deletion of
the repeat, reordered, subset encode, and subset decode results also removes
unneeded full-object references before accounting and corruption checks.

## Remaining bounded peaks

The evaluator still intentionally keeps the full public input and its encoded
archive while testing. A payload bit mutation transiently holds the original
archive, the packed one-object archive, a mutable payload copy, and the packed
mutant output. Truncation and append cases have a similar bounded multiple of
one object. Directory parity and stream unpacking also materialize one full
comparison result at a time. These are proportional to one object/archive,
not the former 35-case accumulation; native decoder working memory remains a
separate measured/enforced limit.

## Verification

`tests/test_corruption_memory.py` passed both checks: the fixed 35-case
sequence fingerprint and a 4 MiB-payload tracemalloc bound. `py_compile` for
the staged gates module and `git diff --check` also passed. No throughput or
native workload was run for this review.
