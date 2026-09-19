# compla6

Team workspace utilities, version 0.7.1.

| Task | Start here |
|---|---|
| Prepare a dataset and run native research | [Native workflow](docs/NATIVE_STRINGS.md) |
| Inspect or replay the published benchmarks | [Benchmark code map](benchmarks/README.md) |
| Generate tables and Pareto plots from results | [Reporting](docs/REPORTING.md) |
| Use the file-interface research controller | [Controller workflow](docs/CONTROLLER_USAGE.md) |

Requires Python 3.11 or newer. Native workflows target Linux x86-64.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
```

Optional agent tools and plotting:

```sh
python -m pip install '.[mcp,plots]'
```

See [installation and deployment](docs/INSTALL.md), [release changes](CHANGELOG.md),
and the [API](docs/API.md). Historical tables remain in [R/README.md](R/README.md).
