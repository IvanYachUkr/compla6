# Compression Lab

Start with the current [README](../README.md), [research contract](RUN_CONTRACT.md),
[researcher workflow](AGENT_WORKFLOW.md), [candidate ABI](CANDIDATE_ABI.md) and
[completion controls](COMPLETION_CONTROLS.md). The earlier controller design,
native evaluator design and validation reports preserve their original release
contracts; they do not override the current generated research instructions.
Runtime setup is in [runtime/README.md](../runtime/README.md); the verified native
prefix and dependency locks are explained in
[dependencies/README.md](../dependencies/README.md).

A release ZIP contains one application directory. Verify its manifest before
installing or creating a virtual environment:

```sh
python3 tools/verify_release.py .
```

To package a prepared source tree, first build the matching wheel and list the
exact fresh public validation receipts in `RELEASE_RESULTS` in
`tools/package_release.py`. Then choose a new output directory outside the source
checkout:

```sh
python3 tools/package_release.py --output /absolute/new-release
```

Add `--without-mcp-wheels` to omit the optional MCP wheelhouse. Source, tests,
native locks/licenses and the offline build wheel remain included. The packager
checks file hashes, sizes, executable modes and ZIP integrity, and writes the ZIP,
SHA-256 sidecar and `release-receipt.json` together in the output directory.

Historical documents describe their named versions. The immutable 0.5.0 archive
retains prior raw results, generated workspaces, original input and full runtime
observations; the new ZIP references it in `provenance/PREVIOUS_RELEASE.json`.
Packaging does not re-certify that historical evidence.
