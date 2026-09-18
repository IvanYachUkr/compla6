# compla6

Team workspace utilities, version 0.6.6.dev1.

For the benchmark code review, start with [benchmarks/README.md](benchmarks/README.md).
It maps the timers, upstream benchmark, wrappers, selected implementations and recorded results.

Requires Python 3.11 or newer. Native workflows target Linux x86-64.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
```

Optional tool integration:

```sh
python -m pip install '.[mcp]'
```

See [installation](docs/INSTALL.md), [usage](docs/CONTROLLER_USAGE.md), and the
[API](docs/API.md). Historical results are in [R/README.md](R/README.md). The current native string
workflow is described in [NATIVE_STRINGS.md](docs/NATIVE_STRINGS.md).
