# Installation

Version 0.6.4 requires Python 3.11 or newer. Native evaluation targets Linux
x86-64 with the toolchain and isolation prerequisites described in
[the dependency guide](../dependencies/README.md) and
[host provisioning](SECURITY.md).

Run from the repository root:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
compression-lab --version
compression-lab doctor
```

For the optional MCP integration, use `python -m pip install '.[mcp]'`.
The pinned CPython 3.12 Linux x86-64 dependency set is also available in
`requirements-mcp.lock`; install it with
`python -m pip install --require-hashes -r requirements-mcp.lock`.
Dependency wheels are downloaded during setup and are not committed here.

Build a distributable wheel with
`python -m pip wheel --no-deps --wheel-dir dist .`.
See [runtime setup](../runtime/README.md) for optional OCI templates and
[controller usage](CONTROLLER_USAGE.md) for workspace preparation and operation.
