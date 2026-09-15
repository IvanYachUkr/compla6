# Experiment controller contract

Historical contract for the user-approved 7 September 2026 redesign, retained for provenance. Current version-2 completion limits and researcher instructions are defined by [COMPLETION_CONTROLS.md](COMPLETION_CONTROLS.md) and [RUN_CONTRACT.md](RUN_CONTRACT.md); existing runs keep their original contracts.

## Authority and inputs

The evaluator owns immutable candidate/result evidence and its dataset, resource, accounting and timing policies. A separate controller owns a versioned run protocol: objective (qualification or measured improvement), deadline, permitted root/worker models and reasoning levels, child limits and bounded feedback policy. Only the host configures that protocol and reports native lifecycle/usage. Agents cannot change it through MCP.

The agent owns hypothesis selection and implementation within its authorized workspace. Candidate assertions and memory interpretations are untrusted input. Public success must reference an exact committed full result from the agent track, compatible with the configured primary profile and current sealed context. Baseline controls remain comparators. All required deployment costs are included through existing rank-cost accounting.

## Completion and lifecycle

An explicit finish request contains a stable request ID, exact candidate/result IDs and requested outcome. The controller verifies evidence and records a receipt or a bounded rejection. Retrying the same request returns its receipt; reusing an ID with different content fails. An export remains an archive operation, including for negative results.

Candidate qualification, improvement against a compatible baseline and the reason a run stopped are separate values. Evaluator-budget exhaustion cannot erase a qualifying result from the last allowed attempt. A negative result or an infrastructure-blocked outcome is reportable without inventing success. No new research starts after the run is closed or its deadline passes; archival access remains available.

Native parent turns, retained children, evaluator jobs and queued deliveries have independent durable identities. An idle parent with outstanding work is waiting. A completion event wakes the appropriate retained session. A queued feedback message is not treated as acknowledged until the adapter observes its acceptance. Recovery reconciles durable state with native state and uses stable IDs to prevent duplicate launches or measurements.

Admission checks child/model/effort and the deadline before provider execution.
Model usage is informational: the host records each native session's cumulative
own input (including cached tokens) and output. Polling replaces a snapshot;
parent totals exclude descendants. Missing or corrected usage never blocks
research or finish. There is no mandatory token cap, reservation, dispatch permit,
or backend capability self-attestation. Completion records usage at its decision;
late native reports remain available in the current brief.

An explicit host stop waits for the active root turn, children and evaluator work
to drain. Public finish may execute inside the root's own tool call; the adapter
drains that turn afterward. Queue wait, active evaluation time and total deadline
are recorded separately. Existing stored result semantics remain readable.

## Agent interface and context

Python and MCP operations share names, argument meanings and structured results. A compact brief exposes the contract, compatible baselines, current/best candidate, outstanding work, native usage, remaining evaluator budget and allowed next operations. Full immutable artifacts remain available by ID. Resume and feedback derive from the same policy as finish. Host administration and private operations remain absent from the public tool set.

## Scoped refinement

`RefinementStore(engine)` supports propose, check, bounded list, versioned rollback and a digest-bearing snapshot. An entry records a hypothesis/interpretation, committed public evidence IDs, a structured measurable claim and optional verified procedure reference. The initial version is provisional. A check uses two new, comparable full results to record support or refutation of that structured claim within the exact dataset/runtime/profile/accounting/timing scope. Free text remains an unverified interpretation. Refuted or incompatible entries cannot silently become current guidance.

A procedure refers to a file in a verified immutable candidate snapshot, with a hash and safe relative path. Retrieval never executes it or grants privileges. Refinement versions and before/after evidence support rollback. No private evidence or automatic cross-dataset global promotion is permitted. An impact comparison pins the memory version and reports any adaptive policy changes.

## Failure cases and acceptance evidence

Required regressions include premature success, wrong candidate/result/profile, baseline-as-submission, screen-as-full, success on the final trial, active children/jobs, duplicate request/delivery/usage events, restart around acknowledgment, host/deadline termination, excessive or unauthorized child admission, public/private evidence boundaries, unsupported/refuted memory and context drift. Existing exact round-trip and owner tests remain authoritative for codecs.

The impact assessment separates (1) deterministic controller/replay reliability from (2) actual agent research outcomes. Model comparisons use fresh public development tasks, matched model/effort and evaluation/resource budgets, preserved raw outputs and total parent/child usage. A fixture test is never presented as a demonstrated compression or generalization gain. Private test data remain sealed from adaptation.

## Primary sources

All accessed 2026-09-07. Living documentation has no fixed publication date.

- OpenAI, GPT-6 model guidance (living): https://developers.openai.com/api/docs/guides/latest-model — explicit autonomy, instruction-file audit, deliberate delegation and proportional verification.
- OpenAI, GPT-5.6 guidance (living): https://developers.openai.com/api/docs/guides/latest-model?model=gpt-5.6 — concise nonduplicated instructions, relevant tools, measured effort and programmatic-tool choices.
- OpenAI, Harness engineering, 2026-02-11: https://openai.com/index/harness-engineering/ — mechanically enforce invariants and return actionable feedback.
- OpenAI Agents SDK guardrails (living): https://openai.github.io/openai-agents-python/guardrails/ — agent/tool guardrails apply at distinct boundaries; hosted MCP and built-in shell do not automatically use function-tool guardrails.
- Karten et al., Prime Agent v1, 2026-08-24: https://arxiv.org/html/2608.23552v1 — persistent computation and sessions, external-state refinement with fixed weights, execution accounting; nanoGPT results do not establish a universal harness advantage.
- Prime Agent 0.9.3 installed docs and implementation — explicit skill visibility controls host-handler registration; runtime capabilities must be verified for the selected transport.
