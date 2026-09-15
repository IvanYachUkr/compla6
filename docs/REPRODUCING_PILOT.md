> **Historical 0.2.2-era document.** Retained for context, not a fresh 0.3
> execution claim. Current entrypoints are `../README.md`, `MIGRATION_0_3.md`
> and `VERIFICATION_0_3.md`. Do not run old machine-specific commands blindly.

# Reproduce the frozen public Hadoop comparison

The source release includes all 469 public objects, the exact group-split card,
full raw results and source/runtime exports. It includes no provider account or
historical private corpus. The final Sol receipt states its separate outcome.

After verifying/installing the release, extract the included conventional and
Luna exports into new directories, then run the same frozen-source comparison:

```sh
python -m zipfile -e results/local-integration/final-exports/zstd-1-dict.zip /tmp/lab-control
python -m zipfile -e results/local-integration/final-exports/luna-template-final.zip /tmp/lab-luna
python tools/recheck_pilot.py \
  --workspace /absolute/new-hadoop-recheck \
  --dataset results/local-integration/hadoop-input/card.json \
  --baseline-source /tmp/lab-control/candidate/source \
  --candidate-source /tmp/lab-luna/candidate/source
```

Use the interpreter where the core wheel is installed. Native package/toolchain
requirements and the OCI build are in `runtime/README.md`. The program runs both
full native quality gates and seven timing trials per direction, saves complete
raw results, comparison/resume and exports, and makes no model call. A different
host receives a different fingerprint and may produce different timing/binaries;
it must meet the card's gates independently. Sources include pinned library/tool
paths, so another native distribution may need an explicitly new candidate manifest.

This reproduces frozen algorithms; it does not claim to reproduce stochastic
model reasoning or search economics. The live model used only the training split
to design/train; the final source and card are frozen. All-object archive bytes
include train and development, whereas the ranking score uses development only.
The smaller `tools/acceptance.sh /absolute/new-output` remains the quick one-command
fresh-install/full-gate smoke check.

For the Sol winner, extract `final-exports/sol-prefix-final.zip` in the same way and
supply its `candidate/source` directory as `--candidate-source`. Pass
`--candidate-name sol-prefix-final` so the new report labels it accurately. The
source is frozen; this makes no model call and does not alter the recorded pilot.
