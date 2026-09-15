> Historical 0.3.0 record. Current setup and verification are linked from
> [README](../README.md). Referenced old results and host wrappers remain in the
> immutable 0.5.0 archive identified by `provenance/PREVIOUS_RELEASE.json`.

# Runtime setup for Compression Lab 0.3.0

## 1. Separate host roles

The operator provisions software and authorized data. The evaluator owns sealed
workspaces, candidates, ledger/results and optional private boundary. The model's
agent has a different nonroot UID, no overlapping privileged groups, read access to
public input and write access only to its workbench. A desktop host is not itself
the trusted evaluator. Never mount private data into the agent or candidate runtime.

A same-UID public smoke is supported for convenience and was exercised here as
root inside this execution container. It cannot certify the full root/nonroot
resource and owner boundary. The preflight records both blocks explicitly.

## 2. Native Linux prerequisites

For an ordinary Debian/Ubuntu operator image (one-time networked provisioning):

```sh
sudo apt-get update
sudo apt-get install python3 python3-venv build-essential util-linux libseccomp2 \
  libzstd-dev liblz4-dev libbrotli-dev liblzma-dev
```

These package names request the distribution's versions, **not necessarily current
upstream versions**. `libxxhash` can appear in LZ4's actual ELF closure and is charged
accordingly. `git` is needed for the source-fetch helper; `bubblewrap` is needed only
for the optional agent launcher. Core evaluation uses util-linux namespaces/chroot
and seccomp, not a silent insecure fallback. Nested namespaces can require explicit
OCI/AppArmor provisioning; the preserved `runtime/oci/` policies describe the
previous tested host, not a newly built 0.3 image.

For pinned upstream source builds, read `../dependencies/README.md` and use the
hash-checking fetch helper. Builds at the new upstream versions remain a local
commissioning task. The archive contains no assertion that network provisioning
or the preserved Ubuntu snapshot can be reproduced from this execution container.

## 3. Core offline installation and build

```sh
python3 -m venv /absolute/lab-venv
/absolute/lab-venv/bin/python -m pip install --no-index --no-deps \
  dist/compression_lab-0.3.0-py3-none-any.whl
/absolute/lab-venv/bin/compression-lab doctor
/absolute/lab-venv/bin/compression-lab doctor --release
```

To rebuild offline, first provision the pinned build backend on a connected
operator machine. Its official wheel hash is in `requirements-build.lock`; the
input supplied an installed-file inventory, **not** a setuptools wheel:

```sh
# Connected operator step, not executed in this network-blocked container:
python -m pip download --only-binary=:all: --no-deps --require-hashes \
  -r requirements-build.lock --dest /absolute/build-wheels
# Offline target after transferring that hash-verified wheel:
/absolute/lab-venv/bin/python -m pip install --no-index --no-deps --require-hashes \
  --find-links /absolute/build-wheels -r requirements-build.lock
/absolute/lab-venv/bin/python -m pip wheel . --no-index --no-deps --no-build-isolation -w dist
```

The wheel in this release was built using the already installed 82.0.1 backend;
its actual installed-file hashes were recorded. The core wheel itself can be
installed offline without setuptools or MCP.

`requirements-build.txt` and `pyproject.toml` preserve the intentional 82.0.1 pin.
The verified latest setuptools version is 84.0.0, not that installed backend.
No model package, account key or network is needed to run a built native codec.

## 4. Optional MCP SDK

The complete supplied hash-locked wheelhouse is **CPython 3.12 / Linux x86-64**.
Use Python 3.12 for that exact installation, even though the core supports 3.11+:

```sh
python3.12 -m venv /absolute/lab-mcp-venv
/absolute/lab-mcp-venv/bin/python -m pip install --no-index --no-deps \
  dist/compression_lab-0.3.0-py3-none-any.whl
/absolute/lab-mcp-venv/bin/python -m pip install --no-index \
  --find-links wheelhouse/mcp-linux-x86_64-cp312 --require-hashes -r requirements-mcp.lock
/absolute/lab-mcp-venv/bin/python -m pip check
/absolute/lab-mcp-venv/bin/python tools/mcp_probe.py --workspace /absolute/public-work \
  --output /absolute/new-sdk-probe.json
```

This pins official server SDK 1.29.1 and the retained dependency closure, not current
SDK 2.1.1. For another Python/OS, resolve and hash a new compatible closure in an
operator environment; do not install incompatible binary wheels with `--no-deps`
and call it reproducible. The tests here used Python 3.13.5 plus compatible provided
base-image packages and the supplied pure-Python SDK wheel. Their actual versions
and installed-file hashes are in `dependencies/observed-runtime-release.json`; this is not
a pristine cp312 lock installation. Earlier cp312 installation evidence remains
historical. No fresh MCP-v2 server port or 0.3 OCI build is claimed.

## 5. Initialize and record before research

```sh
/absolute/lab-venv/bin/compression-lab init --workspace /absolute/new-work \
  --dataset /absolute/authorized/card.json
/absolute/lab-venv/bin/python tools/observe_runtime.py --output /absolute/new-observed.json
/absolute/lab-venv/bin/python tools/prepare_research.py --workspace /absolute/new-work
```

The default preparation sequence screens ten native controls and fully evaluates
stored, Zstd 1 and LZ4. Family unavailability is recorded, not disguised as a
successful baseline. Specify `--full` recipe IDs deliberately; an empty `--full`
requests no full controls. A mutable card uses its separate reference/oracle path.

Full static timing remains single-worker, pinned CPU, seven complete fresh native
processes, warm filesystem cache, no `fsync`, full namespace/bootstrap and native
I/O included. Engine wall budgets include its charged work but not provider billing
or arbitrary editing time. Provision a quiet host and serialize the **same shared
host lock** across evaluator instances. Record all trials, including noise flags.

Never run real speed comparisons while upgrading libraries or modifying installed
engine source. Snapshot change detection intentionally blocks mixed-runtime results.
Use different workspaces/runs for different source/runtime/objective contracts.

## 6. Hardened host connection

Run the public MCP service as the evaluator with a local bearer token passed through
a protected service environment, not committed JSON or CLI arguments:

```sh
/absolute/lab-mcp-venv/bin/python tools/serve_linux.py \
  --workspace /absolute/public-work --port 8766
# COMPRESSION_LAB_TOKEN must already be supplied securely to the service process.
```

Use only authenticated `http://127.0.0.1:8766/mcp`. The loopback probe reads the token
from an environment variable and never writes it to its report:

```sh
python tools/mcp_probe.py --http-url http://127.0.0.1:8766/mcp \
  --output /absolute/new-http-probe.json
```

A live service's working directory/cgroup/runtime must match the initialized
workspace. Account separation, runtime read-only mounts, token scope, effective
host config and provider tool dispatch must be checked on your actual desktop.
See the sibling `host-adapters/README.md` for Prime and ZCode. No service install,
credential read or model call was made on your account during this redesign.
