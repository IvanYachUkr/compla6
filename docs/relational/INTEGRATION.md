# Using the integrated relational workloads

Install the package using the [installation guide](../INSTALL.md).
`examples/relational/` contains tiny generated cards and RLB1 inputs. Dataset cards
select `static_relational` or `mutable_store`; registration validates the ABI.
The same Engine serves CLI and MCP.

```sh
compression-lab init --workspace /absolute/new-work --dataset examples/relational/mutable_store/dataset-card.json
compression-lab profile --workspace /absolute/new-work
compression-lab baselines prepare --workspace /absolute/new-work
compression-lab resume --workspace /absolute/new-work
```

Complete model-free source acceptance (suite, both workloads, six broken controls,
comparison/resume and export verification):

```sh
python tools/accept_relational.py --output /absolute/new-acceptance-directory
```

Use native Linux with the declared toolchain and namespaces/seccomp. Official MCP
tests require the locked SDK. Every run preserves its fingerprint and raw evidence.
Tiny generated fixtures remain smoke/ineligible for performance promotion.

The mutable candidate declares `mutable-store-v1` and its complete runtime.
Protocol/oracle/failure semantics are in [CONTRACT.md](CONTRACT.md). `compare`
accepts results from the same workspace and rejects incompatible workload/card
contexts. `resume` returns advisory state without spending evaluation budget.

Owner lifecycle commands are in the [API guide](../API.md). The owner needs a separate
non-root UID, protected installation, 0700 tree with an existing parent, benchmark
card and a public snapshot readable by that owner. The live public MCP ledger is
owned by the public evaluator; another owner UID needs an explicit operator
snapshot transfer.
