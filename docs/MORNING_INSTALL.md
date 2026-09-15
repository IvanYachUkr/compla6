# Compression Lab native installation

This guide installs the `0.5.1` wheel and describes the pinned native
closure verified on 7 September 2026. Run the commands from the
extracted `compression-lab-0.5.1/` directory. The core wheel is offline and has no
third party Python runtime dependency.

## Verify and install the core wheel

Verify the extracted release before creating an environment:

```sh
python3 tools/verify_release.py .
python3 -m venv .venv
.venv/bin/python -m pip install --no-index --no-deps \
  dist/compression_lab-0.5.1-py3-none-any.whl
.venv/bin/compression-lab doctor
```

The wheel targets Python 3.11 or newer. Keep `.venv` outside any release input
directory when rebuilding a release; the release packager excludes virtualenvs,
native build trees, downloads and evidence caches by default.

Run the support checks after installation:

```sh
.venv/bin/python -m unittest tools.tests.test_release_tools
```

## Optional MCP server

The tested optional closure is **MCP 1.29.1** on Linux x86-64 with CPython 3.12.
Use a CPython 3.12 environment and the hash-locked wheelhouse shipped with the
release:

```sh
python3.12 -m venv .venv-mcp
.venv-mcp/bin/python -m pip install --no-index --no-deps \
  dist/compression_lab-0.5.1-py3-none-any.whl
.venv-mcp/bin/python -m pip install --no-index \
  --find-links wheelhouse/mcp-linux-x86_64-cp312 \
  --require-hashes -r requirements-mcp.lock
.venv-mcp/bin/python -m pip check
```

The MCP lock and wheelhouse are an optional server environment. They are not
needed for the core CLI or for native decoder execution.

## Native prerequisites and verified prefix

Native controls require a Linux compiler/toolchain, `unshare`, libseccomp and
the headers/libraries needed by the selected control. The commissioned machine
used this verified prefix:

```text
/opt/compression-lab-native-2026-09-07
```

Its `native-build-receipt.json` records six probed static codec families and 50
verified output assets: Brotli 1.2.0, bzip2 1.0.8, LZ4 1.10.0, XZ 5.8.3,
zlib 1.3.2 and Zstandard 1.5.7. `dependency-validation.json` records that the
third party headers resolved from this prefix. The pinned Rust toolchain, when a
Rust candidate needs it, is at:

```text
/opt/compression-lab-native-2026-09-07/rust-1.95.0
```

The prefix is a host prerequisite and is not copied into the Python wheel. The
source package includes the 0.4 native parallel source and worker headers under
`src/compression_lab/data/native`, plus `dependencies/native-lock.json`.

To rebuild from preloaded, lock-named source archives, choose directories outside
the extracted release and pass every path explicitly. This avoids the script's
repository-specific defaults:

```sh
python3 tools/build_native.py \
  --lock ./dependencies/native-lock.json \
  --download-dir /path/to/native-downloads \
  --build-dir /path/to/native-build \
  --prefix /path/to/native/prefix \
  --receipt-dir /path/to/native/receipts \
  --jobs 2
```

Existing archives in `--download-dir` are reused and verified against the lock;
missing archives require the explicitly pinned upstream download. Do not use
`sudo`, global installation or `ldconfig`. Keep the receipt and dependency
validation output with the build record. The evaluator discovers
`native-build-receipt.json` inside the prefix or in its sibling `receipts/`
directory. Set `COMPRESSION_LAB_NATIVE_PREFIX=/path/to/native/prefix`.

## Timing boundary

Native encoder and decoder timings measure fresh native processes, namespace
startup and native I/O under the declared benchmark policy. Python virtualenv
creation, wheel installation, MCP startup and CLI/MCP request overhead are
separate setup or control-plane costs. They must not be added to a native
decoder throughput number unless a comparison explicitly defines an end-to-end
Python service measurement. Conversely, a native decoder result does not claim
that Python or MCP is installed or available on the target host.
