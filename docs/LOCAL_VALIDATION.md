# Compression Lab 0.5.2 verification

Fresh combined-release checks on 8 September 2026 (Europe/Berlin). This package
combines the controller-cleanup 0.5.1 core with the Prime operational fixes.
See [the integration history](INTEGRATION_0_5_2.md). Historical 0.5.1 evidence is
separately labeled and does not replace the fresh receipts below.

| Check | Observed result | Receipt |
|---|---|---|
| Complete Python suite | 297 passed; no failures, errors or skips; 628.352 seconds | `results/integrated-suite/work/full-suite.json` and `.txt` |
| Complete native benchmark | 10 screens and 9 full quality checks passed; 19 planned attempts; no failures | `results/integrated-native/work/benchmark/SUMMARY.json` and `attempts.json` |
| Finish against native evidence | Baseline rejected as agent submission; negative finish accepted and stable after restart; export after closure passed | `results/integrated-native/work/benchmark/CONTROLLER_RECEIPTS.json` |
| Native Prime SDK integration | 13 passed using the actual SDK and Python controller with deterministic local transport | `results/integrated-native-sdk.tap` |
| Controller incident checks | 5 cases, 23 assertions passed | `results/integrated-impact/report.json` |
| Release tools | 17 passed after updating the legacy launcher test for the selected profile bind | `results/integrated-release-tools.txt`; initial stale assertion in `integrated-release-tools-before.txt` |
| Installed wheel | 102 production files and 17 public host/docs files match source; offline pinned dependencies pass pip check | `results/integrated-installed-files.json` and `integrated-build.json` |
| Source preservation | All 102 production files and 134 source/tool files unchanged through checks; two packaging/test files have documented final hashes | `results/integrated-source-freeze.json` and `integrated-source-verified.json` |
| Prior releases | Both distinct 0.5.1 archives retain their original hashes | `results/integrated-preserved-releases.json` |
| Actual commissioned isolation | Dedicated UID 997; prior sessions/data absent; protected mounts read-only; private disk temporary storage | `results/integrated-namespace-preflight.json` |
| Actual messaging handlers and MCP | Official skill routing works both ways; live brief/inventory/profile/status passed; no model calls | `results/integrated-messaging-preflight.json` and `integrated-mcp-preflight.json` |

The native benchmark used six deterministic 4 MiB objects, three per public split,
for 25,165,824 canonical bytes. All nine full controls passed exact quality; all
remain explicitly `smoke_not_certified`. Brotli4 and XZ3 also missed the encoding
floor. The 435.593-second benchmark retains all 19 raw results and
28 balanced CRC diagnostic trials, whose archive bytes are identical. Those
trials are supplemental diagnostics and do not replace scored measurements.
Both isolated validation commands held the real host benchmark lock and confirmed
complete descendant cleanup. Timing receipts identify their validation namespace.

The wheel is `dist/compression_lab-0.5.2-py3-none-any.whl`, SHA256
`953445291e58c425b2de7afbba24e8ab3391211be34cf6a9990d31c0fd1a0666`.
Build/MCP dependencies came from the pinned offline wheelhouses. The first build
setup used the parent wheelhouse path; it was corrected to `build-tools` before
building. No dependency version changed.

The real-provider native-controller smoke belongs to the historical cleanup 0.5.1
release. The current 13-test SDK run is fresh but uses deterministic transport.
The commissioned Hadoop environment uses the separately documented subscription
RPC runner; its host verifies completion from reports and immutable result/export
receipts. It does not configure the native controller's admission/finish state.
Any research-launch receipt is recorded outside this frozen release. Model usage
is informational; neither integration claims a universal hard token ceiling.

No new private-test evaluation, matched research productivity comparison or
refinement-benefit experiment was performed by these package checks. Existing
Text8/OpenStack measurements remain tied to their original capsules.

## Reproduce

Install the matching wheel and locked dependencies as described in
[MORNING_INSTALL.md](MORNING_INSTALL.md). Use fresh output directories on the
commissioned Linux host with its pinned native libraries and Prime 0.9.3 runtime.

```sh
LAB_RELEASE="$(pwd -P)"
.venv/bin/python tools/validate_isolated.py --output "$LAB_RELEASE/results/NEW-suite" -- \
  "$LAB_RELEASE/.venv/bin/python" "$LAB_RELEASE/tools/run_tests.py" \
  --output "$LAB_RELEASE/results/NEW-suite/work/full-suite"
.venv/bin/python tools/validate_isolated.py --output "$LAB_RELEASE/results/NEW-native" -- \
  "$LAB_RELEASE/.venv/bin/python" "$LAB_RELEASE/tools/benchmark_controller.py" \
  --output "$LAB_RELEASE/results/NEW-native/work/benchmark"
/opt/prime-agent-0.9.3/node --test tests/test_prime_controller.mjs
.venv/bin/python -I tools/controller_impact.py --out results/NEW-impact
.venv/bin/python -I -m unittest discover -s tools/tests -v
.venv/bin/python tools/package_release.py --output /new/path/release-052
```
