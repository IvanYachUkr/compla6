# Compression Lab 0.3: instrument the search, preserve certification

## Decision

Keep the Python control plane, content-addressed candidates, durable workers,
owner-only evaluator and static/mutable workload boundary. Add a small research
layer above them rather than a new agent framework. The LLM remains an offline
designer. Generated runtime code has no model/API dependency.

Alternatives considered: a wholesale agent-orchestrator rewrite would duplicate
Prime/ZCode scheduling and complicate the private boundary; patching only the
prompts would leave baseline discovery, cheap experiments and measurement scope
unaddressed. The chosen design changes what the model can cheaply observe and
reuse while preserving the trusted evaluator.

## Actual source trace

`cli.py` and `mcp_server.py` call `Engine`. `Engine.register` snapshots and builds
through `workloads.bridge` and `candidate.py` (or the mutable registry).
`Engine.evaluate` reserves a budgeted durable job; `worker.py` takes the host lock,
checks the runtime fingerprint and dispatches through the workload bridge.
Static gates in `gates.py` check reconstruction, object independence, directory
parity, corruption, reproducible builds, sanitizers and seven fresh processes.
`runner.py` includes namespace/bootstrap, I/O and supervision in elapsed time.
`accounting.py` ranks development projection with complete standalone fixed costs.
The mutable bridge instead runs a persistent bounded protocol, an independent
oracle, crash schedules and operation metrics; it has no static speed gate.
`owner.py` freezes a digest and consumes the independent test once.
`connections.py` installs owned-only host entries. Prime's native bridge and the
ZCode app-server snapshot are clients, not evaluators. The supplied evening
launchers have machine-specific paths, ports and deadlines; they are archival
examples, not portable setup commands.

## Implemented direction

1. **Train-only screening (`screen-v1`).** Deterministically select whole training
   objects under explicit byte/object bounds; check sample round trips and cheap
   arbitrary-byte fixtures; record three fresh-process observations. No directory,
   sanitizer or full corruption certificate is implied. Screen results have
   `quality_passed=false`, `screen_passed=true` only on success, and are never
   eligible. Full evaluation still executes every original gate and trial.
2. **Native capability and recipe inventory.** Distinguish installed binary
   versions/hashes from current upstream releases. Expose all five families,
   missing prerequisites, dictionary provenance, source factory and exact cached
   baseline trials to both transports. Cache by the actual registered digest,
   never a human-readable candidate name. Only full results enter qualified
   baseline frontiers.
3. **Reusable native support.** Preserve CLB1 for identity controls; accelerate
   its exact CRC without changing the polynomial or bytes. Supply bounded
   framing, literal fallback and reversible transform hooks around mature native
   codecs. Transformed archives use an explicitly different CLT1 envelope.
   Optional static-codec linking ships and charges its build assets; the linked
   bytes remain in the executable cost. It is not a library-cost waiver.
4. **Two deployment views.** `standalone-v1` stays primary. Optional
   `installed-runtime-report-v1` profiles must be declared in the immutable input
   card and match both SONAME and SHA-256. The full export is unchanged; only the
   supplementary incremental-deployment report deducts matching library bytes.
   Dictionaries, notices, configuration and executable bytes are never waived.
   Mutable workloads retain their separate complete-store accounting.
5. **Focused feedback.** A bounded research brief and result feedback expose
   missing controls, byte-cost components, speed headroom
   and the difference between search evidence and certification. Planner plus
   implementer is optional: one shared workbench, one explicit hypothesis, one
   implementation/evaluation writer. No mandatory agent swarm or long forms.

## Invariants and migration

The static encoding floor remains **100,000,000 bytes/s**, seven complete fresh
processes, no warmup removal, no startup subtraction, no independent-test data in
search, and no implicit platform-library expansion. The source archive supplied
by the user is preserved byte-for-byte. This release changes the engine digest;
start a new workspace rather than editing old runtime/card hashes. Historical
results remain historical evidence, not re-certified 0.3 results.

## Limits

A smoke benchmark is not an independent research result. Runtime self-reported
kernel timings (when used by a diagnostic tool) cannot replace trusted external
elapsed time. Sanitizers do not instrument already-compiled distro shared libraries or static
archives. Static-codec assets are hashed and charged, but not a claim of rebuilding
third-party source with ASan/UBSan. CRC is an
accidental-corruption check, not cryptographic authentication. Public workspace
integrity requires evaluator ownership/read-only mounts when a model can execute
shell code. Live Prime and ZCode sessions require the desktop host and are not
inferred from SDK transport tests.

## Read and budget boundaries

The sealed card/manifest authorize discovery and registration without O(dataset)
payload scans. Screens hash the selected whole training objects; quick/full workers
and export verify every public object. Manifest hashing alone never certifies
payload integrity. The data-scope regression distinguishes these cases explicitly.
Dictionary sampling bounds memory, not full contributing-object hash I/O. Trusted
factory time is serialized and charged, but not forcibly preempted; manual editing,
model thinking, provider tokens and supplemental benchmark diagnostics have separate
cost scope rather than pretending to be inside the evaluation ledger.

## Native extension point

`baselines.create` supplies `codec.cpp`, `lab_crc.hpp`, `lab_transforms.hpp`, a
complete candidate manifest and charged license/build assets. The reversible
transform runs before an established codec; decoding performs the inverse before
checking the raw checksum. Literal fallback retains the original bytes. This is a
small, independently testable composition point: change one representation, reuse
framing/corruption/accounting, and measure the ablation against identity. It is not
a reason to omit cost or mandatory native gates. Agent-written code may replace
the template entirely when a measured hypothesis warrants it.

## Measured scope

The source and host-adapter trace motivated these changes; it did not establish
that the previous add-on caused the reported GLM regression. The included experiment
tests native CRC throughput, reversible composition, cost disclosure and protocol
behavior. It does not test whether these prompts improve a model's success rate.
An actual study should predeclare datasets/groups, runtime, objective and model
budget, compare conventional selection and agent tracks, repeat independent search
seeds, keep all failed attempts, and freeze before a one-shot independent test.
Use a planner only when it reduces demonstrable search waste; one evaluator writer
and a single current hypothesis are the default.
