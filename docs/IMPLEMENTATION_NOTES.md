> **Historical 0.2.2-era document.** Retained for context, not a fresh 0.3
> execution claim. Current entrypoints are `../README.md`, `MIGRATION_0_3.md`
> and `VERIFICATION_0_3.md`. Do not run old machine-specific commands blindly.

# What was implemented and what evidence means

The supplying operator's DESIGN.md is preserved alongside this record. The release
implements a small Python engine and CLI, not a new model harness. The final codec
runs natively and offline; no model, account, hidden corpus, old agent codec or
other-chat material was retrieved. Public source objects in the attachment are
inert input, never executable instructions.

| Contract | Implementation | Evidence |
|---|---|---|
| Verify input bundle | Unchanged supplied verifier and manifest | results/00-bundle-verification.txt |
| HCB / byte adapters and strict names | hcb.py, stream.py, dataset.py | foundation, demo-input and native tests |
| Reversible arbitrary native programs | candidate.py manifest/build, four native commands | C++ controls, intentionally broken native candidates |
| Immutable source/binary/artifact/runtime inventory | candidate.py, engine.py | stale snapshot, path/dependency, full rebuild tests |
| Honest side-information charge | accounting.py, framed-byte rows, dependency licenses | arithmetic, framing, complete exported inventories |
| Full public loop | engine.py, worker.py, CLI | installed matrix demo plus durable vertical-slice test |
| Exactness and strange valid content | gates.py | 35 supplied + 39 generated/name cases per full run |
| ASan and UBSan | clean sanitizer build + engine-owned runtime policy | passing full scopes and deliberate alias fault rejection |
| Bounded corruption | header/body/truncation/trailing/random mutations | no successful partial stream/directory output |
| Fresh timing | runner.py and seven-trial raw arrays | every encoder/decoder trial, no selected fastest attempt |
| Private filesystem and native isolation | owner.py, _sandbox.py | real UID read denial, real namespace denial probes |
| Crash/attempt/terminal state | fsync journal, worker identity, parent-death guard | journal replay, result-commit recovery, SIGKILL, cancellation |
| Current external integrations | connections.py and shared skill | Codex/ZCode fixture tests; Prime native CLI recipe only |
| Real MCP transport | official SDK adapter and official client test | test supplied but skipped here; SDK unavailable |
| Deployable package | pyproject + offline wheel + fresh venv | final wheel/build/install logs and installed demo |

Important limitations are part of the release, not pass conditions waived after
measurement. Smoke data cannot certify encoding speed. A private lifecycle was
not completed in the builder's root/shared environment. OCI image construction,
optional SDK installation/protocol execution, live Codex/Prime/ZCode sessions,
Windows/macOS execution, Rust sanitizer support, coverage-guided fuzzing, trained
router attribution and large-corpus scalability were not validated. The strict
Linux native runner and package CLI are implemented; portable interface schemas
are not a claim of tests on every operating system.

The engine implements supplied train/development partitions and grouped comparison.
The card's development-policy/folds metadata is retained; it does not automatically
run cross-validation. There is no trained router or oracle-routing module. Matrix
controls, deterministic/seeded random control tracks, counters and observed cost
views support attribution but do not account for model/provider costs outside the
engine. See API.md and SECURITY.md for those scope boundaries.

The builder's source/tests include failures found during implementation. They are
retained in results/ with later fixes and final runs; an old failed log is not
reported as a successful final run. Tests use actual native builds and OS behavior;
the only optional dependency skip is the official MCP protocol test. No sanitizer,
private, live-client or throughput pass was fabricated to meet a target.
