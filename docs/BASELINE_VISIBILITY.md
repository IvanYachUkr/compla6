# Baseline visibility in 0.5.4

Set `"baseline_visibility": "visible"` or `"hidden"` in the dataset card before
initializing a fresh run. Omission means `visible`; invalid values fail card
validation. The choice is sealed by the existing card digest. Do not edit a
running or historical workspace to change it.

Hidden runs supply no benchmark baseline table, scores, rankings, comparison
bits, baseline job/result IDs, baseline budgets or baseline-only candidate IDs.
The public CLI and MCP use `ResearcherEngine`. Its explicit ledger projection
selects public identities before raw evidence is read; generic status, artifact,
export, feedback, comparison, paired diagnostics and refinement cannot recover
owner baseline attempts. Baseline tools return a no-comparison notice without
executing a factory or returning cached evidence. Absolute objectives, own
candidate measurements and own comparisons remain available.

Algorithms are unrestricted by this setting. A researcher may request a recipe,
register the source and evaluate it as an agent experiment. If its immutable
source/build digest equals an owner baseline candidate, its new agent attempt
remains public and the baseline attempt remains private. Benchmark cache reuse
does not substitute for that new evaluation. Public refinements and controller
feedback also use the public view. Hidden controllers require the `qualification`
objective; baseline improvement cannot decide researcher completion.

The owner API remains `compression_lab.engine.Engine`. For trusted host CLI
inspection or preparation, put `--owner-evidence` before the command, for example:

```sh
compression-lab --owner-evidence baselines prepare --workspace /host/run --depth full
compression-lab --owner-evidence compare --workspace /host/run --results RESULT_A RESULT_B
```

That flag is not exposed through MCP and is not an authorization mechanism.
Workers use the owner Engine to retain complete immutable raw evidence. New
ledger registration and result indexes record visibility provenance; existing
raw result fields, trial counts, resource limits, encoding floor, full-corpus
timing and decoder accounting are unchanged. Default visible workspaces retain
their old behavior without requiring these indexes.

## Host provisioning requirement

The researcher must not be able to read the evaluator workspace directly.
Expose a deliberate allowlist: the sealed public data/card, safe runtime metadata,
the researcher's workbench, newly requested recipe sources and own exports.
Keep state/ledgers, jobs, results, candidates, registration cache, controller,
refinements, factory/preparation receipts, campaign/commission files and owner
exports outside the researcher's readable filesystem namespace. Do not copy
baseline tables, score-bearing examples, prior result files or run history into
prompts or the public workbench. The API view cannot protect a whole-workspace
readable mount. Verify effective access from the actual researcher UID and mount
namespace before model admission.

The commissioned 0.5.4 Prime launcher uses `tools/researcher_mounts.sh` to mount
only the card, public corpus, workbench and `researcher-exports` in hidden mode.
The GLM commissioning adapter uses the same helper. Hidden MCP exports go to
`researcher-exports`; the owner `exports` directory is absent from the model
namespace. Finish owner controls before admission, and keep their temporary
factory work and host completion receipts out of the public workbench.

## Verification

`PYTHONPATH=src:tests python -m unittest test_baseline_visibility -v` exercises
sealed cards, owner/public identity separation, generic-ID denial, own evidence,
same-source registration, controller/refinement feedback and visible defaults.
With the official MCP SDK installed it also exercises the real stdio transport.
Fixtures preserve real ledger/evidence paths but replace native builds and
worker execution; these checks do not certify benchmark throughput or host mounts.

After commissioning the protected runtime, run one targeted integration: prepare
an owner standard-codec baseline on a tiny fresh hidden card, verify that the
researcher cannot read its files/IDs, then have the researcher request and
register the identical recipe and complete one fresh full agent evaluation.
Confirm its own raw artifact/export is readable, the owner baseline remains
unchanged and inaccessible, and visible mode still supplies the expected table.
