# Prime native controller adapter

The adapter uses the installed **Prime Agent 0.9.3 SDK** with its real model registry, authentication, MCP integration, IPython tool, RLM children, native sessions, and message delivery. The controller enforces model/effort admission, child count/depth, optional time and failure-round limits, and the evaluator's existing budget. Protocol version 2 requires at least one completion limit. A monetary commission additionally requires a trusted gateway meter and spending updates; the SDK usage summary is not a dollar admission gate. Tokens are native-reported cumulative **own** usage; there is no universal hard token ceiling or reservation gate.

It uses public SDK services and runtime interfaces. It does not wrap provider streams, patch private methods, modify the installed runtime, or change an existing campaign.

## Run from a configured host

The public workspace must already have a controller protocol allowing the selected model and effort. Supply an existing authorized Prime profile and the commissioned IPython kernel runtime; this command does not provision authentication. Direct Node launches must set `PRIME_AGENT_KERNEL_PYTHON`; the commissioned interpreter on this host is `/opt/prime-agent-0.9.3/kernel-venv/bin/python`.

```sh
export PRIME_AGENT_KERNEL_PYTHON=/opt/prime-agent-0.9.3/kernel-venv/bin/python
/opt/prime-agent-0.9.3/node tools/prime_controller.mjs preflight
/opt/prime-agent-0.9.3/node tools/prime_controller.mjs run \
  --workspace /absolute/configured-public-workspace \
  --cwd /absolute/agent-workspace \
  --state-dir /absolute/host-private-state \
  --agent-dir /absolute/authorized-prime-profile \
  --python /absolute/compression-lab-venv/bin/python \
  --model openai-codex/gpt-6-astra --effort medium \
  --prompt-file /absolute/task.txt
```

For recovery, pass the same workspace, state directory, profile, and Python arguments with `--resume true`; omit model/effort/prompt. Recovery opens exact saved native files, checks their saved model and effort, reconnects retained children, and never replays a startup prompt. A child's effort is not overwritten with its parent's effort.

The CLI polls while the first prompt runs. When a deadline is configured, a separate watchdog starts before `start()` can dispatch. Deadline and operator stops abort and drain native activity. An accepted explicit `finish()` drains the current root response without aborting its own tool turn. SIGINT and SIGTERM request an operator stop. Either startup or polling failure closes the runtime promptly.

## Host integration

The exports are `PrimeControllerAdapter`, `controllerHost`, and `sourcePreflight`. `controllerHost()` invokes the Python host CLI with an argument vector and JSON stdin. A deployment may inject `rpc(operation, arguments)` to reach a separately protected service.

```js
import { PrimeControllerAdapter, controllerHost } from './tools/prime_controller.mjs';

const adapter = await PrimeControllerAdapter.open({
  rpc: controllerHost({ workspace, python }), stateDir, cwd, agentDir,
});
try {
  const parent = await adapter.start({
    model: 'openai-codex/gpt-6-astra', effort: 'medium', name: 'controller', prompt,
  });
  await parent.runRlmChild(childPrompt, {
    name: 'worker', model: 'openai-codex/gpt-5.6-luna', thinking: 'high',
  });
  await parent.waitForRlmQuiescence();
  await adapter.sync();
  await adapter.poll();
} finally {
  await adapter.close();
}
```

Hosted callers must drive `poll()` from the host event loop during the experiment to process feedback and external closure; the independent deadline watchdog also covers a long initial prompt. Never call `poll()` or wait for quiescence from inside the root's own finish tool. That tool should return its controller receipt directly.

Optional `createServices(options)` and `sessionOptions` support host-owned SDK integration. The default services use the supplied Prime profile. Tests can supply real SDK in-memory settings/authentication and a deterministic registered provider. Preserve the supplied `resourceLoaderOptions` when customizing services: they carry the official `agent-message` skill and trusted policy hooks.

This module is not an OS authority boundary. Keep the host RPC, state directory, adapter source, and native transcripts outside agent write access. Commissioned deployments supply dedicated accounts and process/filesystem isolation as described by `docs/SECURITY.md` and the existing `tools/prime_launch_linux.sh`. That launcher alone does not create a protected host RPC. A direct same-UID SDK run is a labeled synthetic transport smoke and does not prove evaluator or filesystem isolation.

## Native behavior and recovery

- **Admission:** the public `SubagentRuntimeHost` checks the controller before constructing each child runtime, including nested children. Prime returns a handle before detached startup; a rejected child may have a handle and native startup failure but no prohibited provider call. Child names are durable keys under their parent. Ambiguous/repeated launches are not recreated; later work uses the retained child.
- **Context:** normal compaction remains native on the admitted model and effort. Prime 0.9.3 refinement deliberately omits reasoning, and branch summaries also omit session effort. In-memory settings disable automatic refinement; public hooks skip explicit refinement planning and cancel tree/session switching and forking. These unsupported paths are disabled without replacing native methods. Research lessons remain available through the compression-lab refinement API.
- **Usage:** `getOwnUsageSummary()` includes cached input and subtracts native child attribution. Cumulative own input/output is reported per native session, including compaction. Missing usage remains unreported. A reporting failure retains its snapshot for retry and does not block a new model call. Native corrections replace the prior snapshot instead of adding it twice.
- **Activity:** retained children become running again on a later message. Observation IDs/sequences are saved before reporting; unconfirmed observations retry with the same identity after recovery.
- **Delivery:** peer agents receive native queued receipts promptly. Controller feedback is claimed once and acknowledged only after `SessionManager.onPersist()` plus a durable native message entry prove acceptance. The native delivery waiter alone resolves too early, at `message_start`. Recovery acknowledges a persisted stable ID without resending. A claim without proof stays outstanding. An incomplete final transcript line does not invalidate earlier complete acceptance entries.
- **State:** an atomic fsynced journal stores IDs, hashes, paths, model/effort, status, and numeric usage. It excludes prompt/message text, model output, and credentials. New directories/files use 0700/0600. An EOF-bound child holds a kernel `flock`, preventing concurrent writers and releasing on host death; native session leases add another check. Do not delete the journal to replay an ambiguous launch.

## Verification and source basis

```sh
/opt/prime-agent-0.9.3/node --test tests/test_prime_controller.mjs
```

The tests use the actual SDK, native RLM runtime, message persistence, and Python controller with synthetic committed-evidence fixtures. Only the transport is deterministic and local, with known cached-input/output counts and a call that can wait for native abort. They cover admission, retention, ambiguous startup, usage/compaction, queued/recovered receipts, a truncated transcript tail, deadline drain, finish inside a native tool, status/binding recovery, and writer exclusion. They contact no model and create no credentials. This is integration evidence, not a research result or real-provider smoke.

The source review used installed 0.9.3 JavaScript and its public declarations on 2026-09-08. Upstream: [PrimeIntellect-ai/prime-agent](https://github.com/PrimeIntellect-ai/prime-agent). Installed package metadata pins agent-core/provider dependencies to release 0.9.3. Relevant files under `dist/core/` are `agent-session-services`, `agent-session-runtime`, `agent-messages`, `session-manager`, `agent-session`, `compaction/compaction`, `compaction/branch-summarization`, and `refinement/refinement`. Preflight requires version 0.9.3 and checks SDK availability; it does not prove account access or provider success. A changed runtime needs another compatibility review.
