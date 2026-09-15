# Completion controller usage, 0.6.0

The evaluator owns candidate snapshots and measurements. The trusted host configures an immutable completion protocol, observes native activity and decides whether a failed research round must continue. The agent can inspect `experiment_brief` and request `finish` through the public tools. A natural-language final answer cannot bypass the protocol.

Use a new workspace and matching installed runtime. Keep controller state, host RPC, budget ledgers and native profiles outside agent write access. Commissioned research uses separate agent and evaluator UIDs; same-UID fixtures only test behavior. Installation and native dependencies are described in the [installation guide](INSTALL.md).

## Configure before preparing instructions

Copy `examples/completion-protocol.json` and set the exact commissioned model, effort and child limits. Protocol version 2 allows `time_limit_seconds` (or `deadline_epoch`), `max_cost_nano`, and `max_failure_continuations`. At least one limit is required; omitted or null limits are disabled. The time duration resolves once to a durable deadline, and any exhausted limit can end research. Three failure continuations mean one initial round and at most three additional rounds. Evaluator budgets remain binding. The example keeps `stop_on_success` false so research can improve speed and compression after first qualification.

```sh
compression-lab controller-configure --workspace /absolute/new-work --protocol /absolute/protocol.json
python tools/prepare_agent.py --workspace /absolute/new-work
compression-lab experiment-brief --workspace /absolute/new-work
```

Alternatively, pass `--protocol /absolute/protocol.json` to `prepare_agent.py` for a new unconfigured workspace. Configuration refuses replacement. `qualification` requires a compatible full agent result that passes all acceptance gates. For new cards the speed floor uses combined end-to-end encoding, including offline preparation. `improvement` additionally requires lower charged size than a compatible eligible supplied control and is unsuitable when comparisons are hidden. Research round limits count feedback deliveries after unsuccessful rounds, not polls, evaluator calls or capacity retries.

The brief includes the exact evidence and timing contract, remaining limits, candidate evidence, native usage, outstanding work and next action. Feedback, resume and finish use the same policy. Protocol version 1 remains supported for historical reproduction; its earlier feedback cap is not a version-2 research-round limit.

## Submit and finish

Register and evaluate source, poll the returned job ID, inspect feedback and preserve each result. Give a request a stable ID when retrying identical content after losing its response. A rejected request keeps its rejection under that ID; use a new ID after obtaining new evidence.

```sh
compression-lab evaluate --workspace /absolute/new-work --candidate CANDIDATE_DIGEST --depth full --request-id trial-001 --wait
compression-lab finish --workspace /absolute/new-work --request-id finish-001 --candidate CANDIDATE_DIGEST --result RESULT_ID --outcome success
```

Export the selected fast and maximum-compression modes and write the report/checkpoint before finishing. CLI rejection exits 2; MCP returns structured reason codes. A negative finish is rejected while the configured continuation allowances remain or a qualified result is available. Infrastructure stops require the trusted host. Exhaustion retains any qualifying final attempt and leaves archival reads and export available. An accepted finish inside a native tool allows the final native handoff to drain.

For the public Python bridge, call `await lab.experiment_brief()` and `await lab.finish(request_id="finish-001", candidate_digest=cid, result_id=rid, outcome="success")`. Full responses remain in `lab.last`; display truncation is explicit.

## Trusted execution and accounting

Use the supervised [native RPC runner](PRIME_SUBSCRIPTION_RUNNER.md) and [completion/retry controls](COMPLETION_CONTROLS.md) for the commissioned API gateway layout. The runner checks the sealed protocol before starting Prime and invokes controller operations under the evaluator UID. `record_spending` accepts trusted cumulative commitments from the private gateway ledger, including root, child, compaction, reservations and retry costs; native token/cost estimates cannot authorize paid requests. An idle root waits for active children or evaluation. Failure feedback is durably claimed and acknowledged only from matching persisted native message receipts. Ambiguous acceptance is reconciled without blind resending.

The alternative [direct SDK adapter](PRIME_CONTROLLER.md) uses public native SDK services and can consume host controller RPC. It reports cumulative own-session usage without a token cap. Money-only commissions still require a protected metered provider path; usage reporting alone is not a dollar admission gate. Host stops drain active work before closure. Do not launch multiple supervisors for the same native root.

Public scoped lessons remain governed by [REFINEMENT.md](REFINEMENT.md). Frozen and adaptive lesson modes do not change the scientific acceptance contract or expose private evidence.
