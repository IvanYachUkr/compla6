> Historical 0.2.2-era installation record. Current instructions are in
> [MORNING_INSTALL.md](MORNING_INSTALL.md) and [CONTROLLER_USAGE.md](CONTROLLER_USAGE.md).

# Installed Linux service, 2026-09-06

An additional isolated 0.2.2 installation at `/opt/compression-lab-0.2.2/venv`
provides opt-in paired development diagnostics. Its active research service is
`compression-lab-paired.service` on loopback port 8768. This uses fresh,
version-matched workspaces; it does not modify old result fingerprints. During
the controlled pair, the agent's default connector is temporarily disabled so
only the experimental evaluator is available. The saved connector preference
will be restored when that comparison finishes. See [release status](RELEASE_STATUS.md).

The following describes the preserved default 0.2.1 installation.

Compression Lab 0.2.1 lives in root-owned `/opt/compression-lab-0.2.1/venv`.
`/usr/local/bin/compression-lab` and `compression-lab-mcp` select it. Old 0.1.1 and
0.2.0 installations remain intact for their existing experiment fingerprints.

Prime 0.9.2 is the default `prime-agent`. Node 22.23.2 and its dedicated kernel
live in `/opt/prime-agent-0.9.2/`. A separate temporary directory isolates its
daemon from old 0.7.2 HRTIM daemons. `prime-agent-0.7.2` preserves the prior
launcher. No old running process was restarted.

## Public service

Enabled `compression-lab-mcp.service` runs as `compression-evaluator` (UID 995),
SDK 1.29.1, at `http://127.0.0.1:8766/mcp`. Workspace:
`/var/lib/compression-lab/hadoop-sol-high-0.2.1-2026-09-06`.
`compression-agent` (UID 997) has an editable workbench and cannot read private
research, evaluator credentials or the result ledger. Neither UID has elevated groups.

```sh
systemctl is-active compression-lab-mcp.service
systemctl is-enabled compression-lab-mcp.service
compression-lab --help
prime-agent --version
sudo -u compression-evaluator /opt/compression-lab-0.2.1/venv/bin/compression-lab resume \
  --workspace /var/lib/compression-lab/hadoop-sol-high-0.2.1-2026-09-06
```

The isolated launcher is configured for native Prime MCP. Starting it only makes
provider requests when a prompt is submitted:

```sh
sudo -u compression-agent /opt/prime-agent-0.9.2/launch-compression-lab \
  /var/lib/compression-lab/hadoop-sol-high-0.2.1-2026-09-06
```

Provider auth stays in the agent's private home. The evaluator's local bearer
token is in a mode-0600 environment file outside this repository. Never put either
into command arguments, exports or a project. The public service exposes no owner
or private operations.

`runtime/` contains the unit templates, scoped AppArmor policy and tmpfiles rule
for `/run/compression-lab/benchmark.lock`. The slice disables cpuset delegation only
for this evaluator slice so service and captured runtime agree; candidate CPU
pinning remains active. Scoped AppArmor allows the evaluator's temporary namespace
mounts on Ubuntu. Units and lock provisioning were checked; no reboot was performed.

## Reproduction, upgrade and removal

The source ZIP includes an offline core wheel, tests, generated examples and public
evidence. `tools/acceptance.sh /absolute/new-output` is the tested fresh-install/
full-gate smoke command. MCP wheels target CPython 3.12 Linux x86-64; OCI instructions
are in `runtime/README.md`.

Install upgrades in a new versioned directory and initialize a fresh workspace when
its runtime changes. Preserve old candidate/results trees and installations;
edits to a protected package invalidate evidence. Switch service/launchers after checks.

`sudo systemctl disable --now compression-lab-mcp.service` stops future service
runs without deleting data or sign-in. Removal can then unlink only these owned
CLI launchers/units and retire unused versioned directories. Preserve evidence and
account homes until the owner explicitly chooses deletion. No removal was performed.

## Helpers from live-session feedback

The current workspace contains protected `workbench/lab_client.py` and a discoverable
`.agents/skills/compression-lab/SKILL.md`. See [AGENT_WORKFLOW.md](AGENT_WORKFLOW.md).
Registration through the client prepares public candidate permissions so the
separate evaluator can read agent-owned files. Full responses remain in `lab.last`;
displayed summaries are bounded. The conventional source/dictionary is available
at `workbench/baseline-source`.

This public service uses umask 0022 and a mode-0755 export directory so newly
completed export ZIPs are readable, but not writable, by the agent and operator.
Ledger/jobs/candidates/results remain in mode-0700 directories. This setting is
for the public service only, not private-owner directories. Both old experiment
workspaces and their immutable results remain preserved.

For another provisioned public workspace, the operator can copy `tools/lab_client.py`
to its protected `workbench/lab_client.py` and `docs/AGENT_WORKFLOW.md` to its
`.agents/skills/compression-lab/SKILL.md`, keeping both read-only to the agent. The
core wheel and CLI/MCP work without this optional client convenience layer.
