# Compression Lab 0.5.1 verification

Fresh checks on 8 September 2026 (Europe/Berlin). All receipts below belong to
this cleanup release. Historical workspaces and results remain in the immutable
0.5.0 archive identified by `provenance/PREVIOUS_RELEASE.json`.

| Check | Observed result | Receipt |
|---|---|---|
| Complete Python suite | 263 passed; no failures, errors or skips; 626.9 seconds | `results/validation-suite/work/full-suite.json` and `.txt` |
| Complete native benchmark | 10 screens and 9 full quality checks passed; 19 planned attempts; no failures | `results/validation-native/work/benchmark/SUMMARY.json` and `attempts.json` |
| Finish against native evidence | Baseline submission rejected; negative finish accepted and stable after restart; export after closure passed | `results/validation-native/work/benchmark/CONTROLLER_RECEIPTS.json` |
| Native Prime SDK integration | 13 passed; deterministic local transport, actual SDK and Python controller | `results/cleanup-native-sdk-02.tap` |
| Actual Codex subscription transport | Astra Medium and one Luna High child executed IPython; retained messaging, exact-ID restoration, usage and finish passed | `results/cleanup-live-prime.json` |
| Fixed controller incidents | Five synthetic cases; all 23 assertions passed | `results/cleanup-impact/report.json` |
| Release/input-verification tools | 17 passed | `results/cleanup-release-tools-02.txt` |
| Installed wheel | All 102 production files match source; offline MCP/core installation passes `pip check` | `results/cleanup-installed-files.json` |
| Source preserved during checks | All 108 source and package-metadata files unchanged | `results/cleanup-source-freeze.json` and `cleanup-source-verified.json` |
| Previous releases preserved | 99 protected 0.4.1 files match both source capsule and installed wheel; original 0.5.0 ZIP hash unchanged | `results/cleanup-preserved-releases.json` |

## What the native checks establish

The benchmark used six deterministic 4 MiB objects, three per public split:
25,165,824 canonical bytes. All nine full controls passed exact quality. All are
explicitly `smoke_not_certified`; Brotli-4 and XZ-3 also missed the 100 MB/s encoding
floor. This is implementation evidence, not a new qualified compressor result.
The 430.758-second benchmark includes all nineteen attempts, the fixed balanced
CRC comparison and three exports. The controller check additionally verified
export after closure. The supervised command held its benchmark lease for
435.186 seconds, with confirmed descendant cleanup.

The release includes all nineteen raw result JSON files and the supplemental
CRC trials. The CRC comparison verified identical archive bytes and retained all
seven trials per control per direction. It does not replace scored measurements.
Generated workspaces, compiled binaries and duplicate runtime inventories are
excluded from the release archive; the local validation workspaces are preserved.

The actual-provider check used a fresh synthetic controller with no compression
evaluations or hidden data. Its successful run reported 17,770 native own-session
tokens, including cached input, across the root and child. Restoration made no
additional model call and did not duplicate usage. This proves the exercised
transport, tool and lifecycle path, not an improvement in research productivity.
A prior setup attempt reached the model but its IPython tool lacked the explicit
kernel interpreter setting; its sanitized failed receipt is retained as
`results/cleanup-live-prime-setup-failure.json`. The launch instructions now set
the existing commissioned interpreter. No smoke process remained after cleanup.

The tested wheel is `dist/compression_lab-0.5.1-py3-none-any.whl`, SHA-256
`5ed0e43c7eb797a1fe58c7c4a771d7f477aed8f0efe443ed8d5e27a2687aee8d`.
The tested adapter SHA-256 is
`ecdf18ed693c0a86ca1a2b69f48b5c001e9fc4c923c80410030c3a8cd53fec8e`.

## Reproduce on the commissioned host

Install the wheel and locked MCP dependencies using
[the installation guide](MORNING_INSTALL.md). The native libraries, Rust toolchain,
Linux sandbox and Prime installation are host prerequisites. Use new output paths.

```sh
LAB_RELEASE="$(pwd -P)"
.venv/bin/python tools/validate_isolated.py --output "$LAB_RELEASE/results/NEW-suite" -- \
  "$LAB_RELEASE/.venv/bin/python" "$LAB_RELEASE/tools/run_tests.py" \
  --output "$LAB_RELEASE/results/NEW-suite/work/full-suite"
.venv/bin/python tools/validate_isolated.py --output "$LAB_RELEASE/results/NEW-native" -- \
  "$LAB_RELEASE/.venv/bin/python" "$LAB_RELEASE/tools/benchmark_controller.py" \
  --output "$LAB_RELEASE/results/NEW-native/work/benchmark"
/opt/prime-agent-0.9.3/node --test tests/test_prime_controller.mjs
.venv/bin/python tools/controller_impact.py --out results/NEW-replay
.venv/bin/python -m unittest discover -s tools/tests -v
```

`validate_isolated.py` holds the real host benchmark lock while its entire process
tree runs, and supplies a separate lock in a private mount/PID namespace. The test
command runs as the ordinary user. Native timing receipts identify that validation
namespace; they are not directly interchangeable with live campaign timings.
The actual-provider setup is described in [PRIME_CONTROLLER.md](PRIME_CONTROLLER.md).
It uses existing authorized authentication; the offline tests contact no model.

No matched research arms, refinement-benefit experiment or new held-out evaluation
has been performed for this release. Existing campaigns retain their own evaluator
and intervention histories. The release packager verifies the file manifest and
every ZIP member's bytes, size, mode and CRC, then writes an external
`release-receipt.json` and archive SHA-256 sidecar.
