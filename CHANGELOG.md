# Changes

## 0.7.1

- Full native qualification now enables UBSan instrumentation for the diagnostic
  build and rejects undefined behaviour observed on the supplied corpus. The
  previous native diagnostic path rebuilt without sanitizer flags; its recorded
  diagnostic passes are not UBSan evidence and need revalidation for that claim.
- Native exports verify the manifest, complete member inventory and every member's
  size and hash before reuse. New ZIPs are verified and published atomically, so
  interrupted writes cannot leave a partial archive at the final export path.
- Regression tests cover real signed overflow, valid qualification, damaged
  exports and interrupted-write recovery. Historical measurements remain intact.

## 0.7.0

- Native workspace setup pins the supplied data, row framing, timing driver and
  libraries. LF-separated columns, NUL-separated queries and bulk byte streams
  use the same evaluator.
- Research instructions follow the configured objective and interface. Shared
  clients support native submission, status and server comparison tools.
- A report command generates Markdown tables, CSV and optional Pareto plots from
  saved measurements, keeping machine, workload, timing and size conventions explicit.
- Portable benchmark runs record host, input and build metadata and share the
  evaluator's measurement lock.
- Source distributions include the benchmark code used for review and replay.

Existing recorded results, upstream benchmark sources and timing kernels are
retained. New workspaces use the declared 0.7.0 configuration; existing runs keep
their original configuration and software installation.
