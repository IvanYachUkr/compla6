# Installation

Version 0.7.0 requires Python 3.11 or newer. Native evaluation targets Linux
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

For agent tools and plots, use `python -m pip install '.[mcp,plots]'`.
Tables and CSV output work with the core installation; plotting needs Matplotlib.
The pinned CPython 3.12 Linux x86-64 dependency set is also available in
`requirements-mcp.lock`; install it with
`python -m pip install --require-hashes -r requirements-mcp.lock`.
Dependency wheels are downloaded during setup and are not committed here.

Build a distributable wheel with:

```sh
python -m pip wheel --no-deps --wheel-dir dist .
```

The wheel contains the library, command-line tools and native ABI/timing sources.
The Git checkout and source distribution also contain the reviewable benchmarks,
baseline wrappers, selected candidate sources and recorded result extracts.
Native codec libraries and datasets are supplied separately.

For a new deployment, install the wheel into a new virtual environment and create
a new workspace using the [native workflow](NATIVE_STRINGS.md). The workspace
records its input, driver and library hashes, framing and timing settings.
Configure the evaluator service to use that environment and workspace; keep
existing experiments on their original environment until they finish.

For repeatable benchmark replays, follow the [benchmark guide](../benchmarks/README.md).
Generate tables and plots from the saved output using the [report command](REPORTING.md).
See [runtime setup](../runtime/README.md) for the optional OCI and service templates,
and [controller usage](CONTROLLER_USAGE.md) for file-interface experiments.
