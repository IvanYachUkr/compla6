# Dependency locks and the native prefix

Start with the current [README](../README.md) and
[controller installation guide](../docs/CONTROLLER_USAGE.md).
The core wheel needs Python 3.11 or newer and has no third-party Python runtime
dependencies. Optional MCP 1.29.1 uses the hash-locked CPython 3.12 Linux x86-64
closure in `../requirements-mcp.lock` and `../wheelhouse/mcp-linux-x86_64-cp312/`.
The offline build backend is separately pinned in `../requirements-build.lock`.
These are integration pins, not claims about the latest upstream versions.

`native-lock.json`, verified on 7 September 2026, pins Zstandard 1.5.7, LZ4 1.10.0,
Brotli 1.2.0, XZ/liblzma 5.8.3, zlib 1.3.2 and bzip2 1.0.8. It records official
source URLs, release dates, available archive checksums, immutable source
identities and license scope. A missing published archive checksum remains
explicitly null; a Git commit identity is not an archive SHA-256.

Native controls require an evaluator-owned, verified prefix selected through
`COMPRESSION_LAB_NATIVE_PREFIX`. They do not rely on whichever codec libraries
happen to be installed system-wide. The commissioned host uses
`/opt/compression-lab-native-2026-09-07`; another host must build and verify its
own prefix. The prefix and its receipt remain host prerequisites outside the
Python wheel. The receipt identifies actual headers, libraries and build probes;
a source lock alone does not establish that those outputs were built or tested.

For a new prefix, run the package's build helper from the application root with
all paths explicit. Keep the prefix and `receipts` directory under one parent:

```sh
python3 tools/build_native.py \
  --lock ./dependencies/native-lock.json \
  --download-dir /absolute/native/downloads \
  --build-dir /absolute/native/build \
  --prefix /absolute/native/prefix \
  --receipt-dir /absolute/native/receipts \
  --jobs 2
export COMPRESSION_LAB_NATIVE_PREFIX=/absolute/native/prefix
```

Preloaded archives are verified against the lock; missing archives require the
pinned upstream download. The evaluator looks for `native-build-receipt.json`
inside the prefix or in its sibling `receipts` directory. No global install or
`ldconfig` step is needed. Rust candidates additionally require the explicitly
commissioned `COMPRESSION_LAB_RUST_TOOLCHAIN`; see the current installation guide.
Changing a compiler, library or toolchain changes runtime identity and requires
new validation and a new workspace.

Upstream and dependency licenses remain in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md),
the lock records, wheel metadata and the verified native prefix. Candidate
exports retain and charge their required decoder runtime and notices under the
experiment's accounting policy.

`upstream-lock.json` and `upstream-extra.json` retain the earlier source audit.
They are distinct from the active native build lock. The old full
`observed-runtime*.json` dumps and their original guide remain in the immutable
0.5.0 archive referenced by the generated `provenance/PREVIOUS_RELEASE.json` at
the application root. They are historical observations, not current runtime
attestations or required installation inputs.
