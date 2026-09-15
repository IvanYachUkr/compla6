# Engine, CLI and MCP API

`brief` returns the sealed `run_configuration`, common research instructions,
current budget use and relevant next actions. New cards default to whole-dataset
research. `manifest_template(name)` returns a complete codec-free C++ v2 manifest;
`record_hypothesis(statement, expected_benefit, falsifier)` records an experiment
before implementation. Include the returned ID as `hypothesis.experiment_id`.

MCP advertises tools for the configured run. `baselines` and `baseline_trial` are
absent when comparisons are hidden; `recipe_create` and codec `inventory` are
absent in from-scratch mode. `experiment_brief` and `finish` exist only after the
host configures a controller, before starting the service. Restart the MCP service
after host configuration changes. Without a controller, completion means export,
RESULT.md, CHECKPOINT.md and the supervisor report described by `brief`.

`feedback` takes one `result_id`. `compare` takes two to eight `result_ids`;
`diagnostics=True` requires exactly two quality-passed full static results in
reference/candidate order. The HTTP helper `lab.py TOOL JSON` preserves these
schemas. Long evaluations return a job ID; poll `status`. CLI names are hyphenated
and support `--wait`.

`evaluate` accepts `screen`, `quick` or `full`. Screens use whole fitting objects
from the card's training or corpus partition; quick and screen cannot qualify.
Final static claims require complete full gates and all seven timing trials.
`primary_size_policy` selects strict deployment or the pinned standard-codec-
available view; both raw accounting views remain available. See RUN_CONTRACT.md.

Metadata-only discovery/registration checks sealed card/manifest structure;
selected payload hashes are verified on screen and all public hashes on full/quick
workers and export. Do not treat registration as unchecked dataset certification.

---

# Engine, CLI and optional MCP

Python entrypoint: `compression_lab.engine.Engine(workspace)`. CLI entrypoint:
`compression-lab`, or `python -m compression_lab`. Versioned JSON responses contain
schema_version, run_id, job_id, candidate_digest, status, reason_codes, metrics
and artifact references. Result IDs bind complete raw JSON. stdout is data;
private data is not an implicit resource in any public interface.

`init` snapshots the card/manifest/public objects and fingerprints the runtime.
`profile` verifies and summarizes only that snapshot. `register` inventories,
copies, builds and hashes a native codec or a declared-runtime persistent store. `evaluate` reserves a durable attempt
and returns a job ID immediately; `--wait` polls the same result. `status` accepts
a run workspace, job ID or result ID. `artifact` reads a bounded allowlisted
range; `cancel` requests process-tree cancellation; `export` validates freshness
and produces a self-inventoried candidate/evidence ZIP. Export does not mean that
an ineligible or smoke measurement has become certified.

`baselines prepare` runs the native conventional matrix (or the durable reference store for mutable cards), caches actual result IDs
by name/depth in the frozen runtime, and records unavailable controls separately.
`baselines list` returns measured rows, the deployment-size/encoding-speed frontier,
best size and best eligible controls. Availability or compilation failures are not
fabricated performance points. Deterministic and seeded-random `control` tracks
run the same full evaluator with the same predeclared per-track limits.

Agent/evaluation budgets count registration/build wall time and failed registration,
and reserve one evaluation before launching it. Completed/failed/cancelled jobs
remain consumed. Each track has independent counters under the same limits.
The optional controller reports native cumulative own-session input/output tokens;
this is informational and has no mandatory token cap. It does not measure every
provider-internal retry or all agent-side latency. Baseline generation, dictionary
setup and controller bookkeeping are separate from native timed encoding. A study
claiming LLM gains must define how it compares these costs.

## Durable evidence

The event journal is authoritative; atomic JSON snapshots are caches. Every complete
journal line is hash chained and fsync'd. Only an incomplete final append is
recoverable; an invalid complete record fails. Source/card/runtime changes invalidate
promotion and export. Large evidence is stored separately from compact status.
A killed job is marked failed and retains partial evidence instead of silently
starting another trial or recovering an attempt budget. Native descendants are
terminated by parent-death handling. An agent may write its public workbench; public evidence is still **not trusted
for private release**: the owner must re-evaluate.

## MCP

`python -m compression_lab.mcp_server --workspace /absolute/work` imports official
`mcp.server.fastmcp.FastMCP` and runs stdio. It exposes public discovery,
registration, evaluation, result/feedback/export and controller/refinement operations.
Long evaluation uses durable
jobs, not a long-lived model orchestrator. Registration remains a bounded native
build and may take longer than a status call. Candidates supplied through MCP must
be inside the public workspace. The API never exposes freeze/evaluate-private.

`tests/test_mcp.py` covers SDK stdio initialization, profile, unsafe-path
rejection and mutable register/evaluate/status/resume/export.
`tests/test_mcp_http.py` covers authenticated HTTP transport.

MCP also exposes `compare` and advisory `resume`. Compare accepts two to eight
distinct IDs with matching card, runtime, workload and depth, verifies evidence,
and returns static cost/speed or mutable operation/store metrics, including
failures. It does not select a statistical winner. Resume preserves consumed
budgets and terminal latches; the best eligible result remains available after
an inferior full candidate. Neither operation calls a model.

Since 0.2.2, `compare --diagnostics --results REFERENCE CANDIDATE` (MCP:
`compare(result_ids=[reference, candidate], diagnostics=True)`) adds bounded
development-group regressions, a descriptive paired bootstrap, deployment-cost
decomposition and aggregate-public speed headroom/MAD. This optional mode needs
exactly two quality-passed full static results. Deltas are candidate minus
reference. It changes no score, eligibility, budget or stored evidence. Timing follows the card scope; the bootstrap covers the scored partition
(training-excluded development or complete corpus) and excludes fixed costs. Raw evidence and ordinary comparisons stay available.

The optional HTTP endpoint uses `--http-port` and `--bearer-token-env`. It binds
only 127.0.0.1, uses the official SDK's origin/DNS checks, and requires a random
pre-provisioned token of at least 32 characters. Tokens remain outside the repo.
There are no public owner/private-disclosure methods.
