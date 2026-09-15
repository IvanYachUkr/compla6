# Supervised Prime RPC runner, 0.6.0

`tools/prime_rpc_check.py` runs native Prime 0.9.3 JSONL RPC through the commissioned Linux launcher, with an independent public evaluator. It now requires the version-2 completion controller before starting research. `tools/prime_goal.py` bridges native session activity, durable feedback receipts, evaluator evidence and remaining completion limits. The explicit `--probe-only` option retains the historical unsupervised lifecycle probe for local fixtures.

Install the matching wheel and host tools into a protected capsule. Keep the campaign, driver output, budget ledger and controller RPC outside agent write access. Use distinct agent and evaluator UIDs; the trusted driver invokes controller operations as the evaluator UID. A fresh native profile contains only commissioned authentication/settings and no earlier research output. Credentials and profiles never enter the repository or release. Disable native automatic refinement to preserve the commissioned reasoning effort. For the bundled paid gateway also set `retry.enabled: false` and `retry.provider.maxRetries: 0`; the gateway owns capacity retry admission.

Configure the controller and generate the workbench prompt before launch. Campaign model, effort, child limits and any deadline must match the sealed protocol. `deadline_utc: null` means no campaign deadline, but another controller limit is still required. A monetary commission supplies `budget_ledger`, pointing to a protected gateway ledger with the same cap. See [completion controls](COMPLETION_CONTROLS.md) for the exact fields and six retry delays.

```json
{
  "status": "research_running",
  "workspace": "/var/lib/compression-lab/RUN/public",
  "profile": "/var/lib/compression-lab/RUN/profile",
  "native_home": "/var/lib/compression-lab/compression-agent",
  "agent_user": "compression-agent", "agent_uid": 997,
  "prime_root": "/opt/prime-agent-0.9.3",
  "lab_root": "/opt/compression-lab-0.6.0",
  "native_prefix": "/opt/compression-lab-native-2026-09-07",
  "mcp_url": "http://127.0.0.1:8774/mcp",
  "parent_model": "openai/gpt-5.6-sol", "parent_thinking": "max",
  "child_model": null, "child_thinking": null,
  "maximum_children": 0, "maximum_depth": 0, "deadline_utc": null,
  "budget_ledger": "/var/lib/compression-lab/RUN/private/budget.json",
  "driver_unit": "compression-prime.service",
  "driver_output": "/var/lib/compression-lab/RUN/driver"
}
```

These paths and identities are host configuration examples, not portable defaults. `prime_launcher` and `kernel_python` may select existing protected commissioned launch wrappers. The launcher maps only the selected profile and public workspace, with helpers and installed runtimes read-only. Its IPython kernel retains the native resource allowance while the evaluator enforces the separate codec resource profile. Never mount owner data, other profiles or prior solutions into the agent namespace.

```sh
python3 /opt/compression-lab-0.6.0/tools/prime_rpc_check.py --campaign /absolute/campaign.json
```

The driver owns failure continuation; do not also run the legacy observer as a second feedback writer. The official native messaging skill remains loaded. Actual root and child identities/model/effort are checked against the commission, and all retained children count toward its lifetime roster. Completion waits for child/evaluator activity. New rounds use the same native session and include remaining limits and acceptance criteria. After closure the matching guardian recognizes a completed run awaiting owner audit and does not advance experiments itself.

For recovery, preserve the native profile, controller state, latest driver status and private output. Select a new output directory and pass `--resume-session` with the exact retained logical native session file and `--prior-status` with the preserved status. The runner resumes native identities without overriding retained child effort, acknowledges already persisted feedback without replay and requires reconciliation when delivery is ambiguous. Do not delete a journal or transcript to force another attempt. Provider quota exhaustion requires verified allowance and supervisor authority before resuming. Capacity failures use the gateway's bounded retry schedule automatically.

`prime_recover.py` remains available for the specific native failed-registration descriptor case. It checks that the exact driver, its cgroup and dedicated agent are inactive before moving the one verified descriptor into a new protected directory. The descriptor contains authentication material; keep it outside the repository and release. This helper does not reset research budgets, move a run to a changed scientific contract or manufacture successful delivery.

Local JSONL and native SDK fixtures exercise continuation, final handoff and provider failures without model calls. Release integration checks use synthetic data, not existing campaign corpora. A running native process or accepted controller finish proves neither a new compression result nor the correctness of a researcher's verbal claims; inspect the retained evaluator evidence and delivered software.
