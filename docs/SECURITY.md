# Security boundary and owner operation

This is a first-release research evaluator using real Linux isolation primitives,
not a security audit or protection against a malicious kernel, system administrator,
compiler, privileged agent, or compromised trusted evaluator installation.

## Two different boundaries

The agent must run as a dedicated **unprivileged UID**, with no sudo/capabilities,
Docker/LXD/wheel membership, host control socket or route to the evaluator UID.
Keep provider credentials in that agent's home, never the corpus/evaluator tree.
The evaluator must run as another **non-root UID**, using an administrator-owned
installation outside the writable public project. Keep its corpus, private card,
journal and outputs in a 0700 owner directory, without named ACLs or symlink
ancestors. An agent sharing the owner UID does not meet the threat model.

Generated code runs in a fresh user/mount/PID/network/IPC/UTS namespace. Builds
see the read-only installed compiler runtime, a read-only source copy and a fresh
output directory. Native codec invocations see only their exact ELF closure,
declared runtime package and permitted input, plus fresh output and temporary
storage. The host home, /etc, /sys, owner tree and host /proc are absent. The /proc
directory stays empty; it is not a bind of host process information. Capabilities
are dropped, no_new_privs is set, and seccomp denies network and namespace/mount,
ptrace, process_vm, kernel instrumentation and related escape-oriented syscalls.
Native runtime fork/clone is denied. Builds require child compiler processes;
RLIMIT_NPROC cannot be treated as a root boundary, so root certification is refused.

A parent-death guard and unshare's kill-child behavior terminate the isolated
process tree after evaluator death. Tests kill a real evaluator process and check
that its native descendants are no longer live. Resource monitoring discovers
children through proc PPid data, including hosts lacking /proc/.../children.

## Host provisioning

As administrator, create two ordinary dedicated accounts and a protected evaluator
installation. Choose numeric UIDs locally; do not reuse an account with privileges.
Provision the shared lock before starting *any* evaluator process:

```sh
install -d -o root -g root -m 0755 /run/compression-lab
install -o root -g root -m 0666 /dev/null /run/compression-lab/benchmark.lock
# Create /srv/lab-owner owned by the non-root evaluator, mode 0700.
# Keep /srv/public-work writable by the agent and readable by the owner.
```

The engine prefers this root-owned lock; otherwise public exploration uses the
/tmp lock. Do not replace or truncate the lock while any evaluator is running.
For an ephemeral container, provision it in the entrypoint before dropping UID.
One lock serializes builds and measurements across cooperating users on that host.
It does not make a busy multi-tenant machine a designated benchmarking host.

Put an operator-supplied private card and private manifest below the owner root.
The card contains only:

```json
{"schema_version":1,"adapter":"bytes","private_manifest":"manifest.json"}
```

The manifest contains schema_version 1 and an objects array. Each object declares
alias, group (or source_group), relative path, canonical_bytes and canonical_sha256.
Paths are relative to the containing private manifest. The public card never
contains this manifest, its names, paths, identifiers or statistics.

Run the owner commands in README as the non-root evaluator UID. `owner-init`
examines path/permission metadata but does not open private card contents. `freeze`
imports the immutable candidate and public data through descriptor-relative,
no-symlink reads, verifies actual inventories rather than claimed sizes, and reruns
full public gates under the owner. A changed runtime or smoke card blocks freeze.

`evaluate-private` fsyncs private_started with the frozen digest before opening
private metadata or bytes. Only an interrupted private_started operation may be
explicitly resumed with the same run and candidate. Attempts and partial evidence
stay owner-only. Failure/cancellation/completion are terminal; a fresh test requires
a separately authorized new run, not editing a latch. Aggregate disclosure is an
explicit owner action and consumes the test for adaptive research. MCP exposes no
owner commands or private artifact directory. Running MCP as the owner defeats
agent separation and is not a supported deployment.

## Limits and fail-closed behavior

The original Pro builder did not certify a private lifecycle. Local integration
completed separate-UID synthetic static/mutable owner lifecycles with fresh public
rechecks, one private attempt and aggregate disclosure. It also built the OCI image
and ran an isolated live Prime session. These bounded checks are not a security
audit or IMDb/power-loss certificate. `doctor --release` and owner promotion still
refuse missing prerequisites. Verify these boundaries on each deployment host.

Resource enforcement combines RLIMIT_AS (except ASan's huge virtual reservation),
RLIMIT_CPU/NPROC/NOFILE/FSIZE, bounded tmpfs, process-tree RSS and elapsed-time
monitoring, and bounded diagnostic/aggregate output checks. RSS and aggregate
output checks are polled, not cgroup-hard instantaneous limits; bursts can overshoot
before termination. Use an outer container/cgroup memory, pids and disk quota on a
dedicated host when evaluating adversarial code. This implementation is not a replacement for a hardened
multi-tenant execution service. Namespaces require kernel support; never downgrade
private work to --exploratory.

Do not run evaluator imports from an agent-writable directory. Root/kernel and
trusted-runtime changes invalidate the scientific boundary. No general side-channel
or differential-privacy claim is made. A final compression-size/timing aggregate
can carry information; disclosure remains under the owner, not the agent.

A declared training provenance record is not a proof of how arbitrary agent-written
artifacts were learned. The engine's conventional Zstandard dictionary training
really reads only train groups, and registration validates its declared hash set.
Arbitrary externally produced models require operator review or a reproducible
train-only generation procedure before scientific claims about learned artifacts.
