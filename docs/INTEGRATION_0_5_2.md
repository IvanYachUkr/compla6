# Compression Lab 0.5.2 integration history

The immediate base is the controller-cleanup 0.5.1 archive, identified by digest
in `prime-integration-inputs.json`. That release removes mandatory model token
caps/reservations and keeps explicit finish receipts, native model/effort
admission, retained child identities, usage snapshots, feedback receipts and
scoped refinement.

A separate Prime operational 0.5.1 package had been based on 0.5.0 and missed
that cleanup release. It was used for the completed Text8 run. Its frozen
archive and that run's measurements remain unchanged. Version 0.5.2 combines
the cleanup core with those operational fixes, plus full reference-result
notifications. No prior measurement is relabeled as a 0.5.2 research result.

The native SDK adapter and commissioned JSONL subscription runner have distinct
host contracts. The latter is currently used by the isolated research slot;
it does not configure the native controller's admission/finish state. Its
completion is verified by the host from immutable public results and exports.
See `PRIME_CONTROLLER.md` and `PRIME_SUBSCRIPTION_RUNNER.md`.

The observer-only 0.5.2 staging candidate was tested but never installed or
released. Its validation remains supplemental and is not the combined release's
full-suite or benchmark evidence. All combined-release claims are documented
in `LOCAL_VALIDATION.md` and point to `results/integrated-*` receipts.
