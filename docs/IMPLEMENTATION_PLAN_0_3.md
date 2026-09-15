# Compression Lab 0.3 implementation and verification plan

Goal: make mature codecs easy to discover, compose and compare, with cheap search
feedback and unchanged independent certification.
Architecture: retain Engine/worker/bridge; add screening, deployment reports and
research discovery as small modules. Reuse native framing and candidate snapshots.
Stack: standard-library Python, native C++17, existing codec C APIs, official MCP.
Spec: ARCHITECTURE_0_3.md.

- [x] Verify input hashes and run the unmodified suite. Preserve the input ZIP.
- [x] Add failing tests for deterministic train-only selection, scenario matching,
      screen non-promotion, exact baseline caching and full-only frontiers.
- [x] Implement screening.py, deployment.py, inventory.py and research.py;
      wire them through Engine, CLI, MCP and the workload bridge.
- [x] Test and implement wire-compatible CRC acceleration and transform hooks.
      Compare identity archives with the original executable; exercise empty,
      binary, odd-length, corrupt and directory inputs under ASan/UBSan.
- [x] Add source factories and static-codec build-asset accounting. Verify every
      copied asset, dynamic closure and exported decoder independently.
- [x] Update prompt, portable host preparation/launch/probe instructions and
      client helpers without reading credentials or invoking model calls.
- [x] Record current official stable dependencies separately from installed/tested
      binaries; provide hash-verified setup and exact source/license provenance.
- [x] Run full regression, CLI/MCP transport checks and reproducible smoke/control
      experiments; retain all failures and raw trials. Do not trim noisy runs.
- [x] Build the wheel, test a clean installation, generate release manifests and
      migration notes, verify the final complete ZIP and provide it to the user.

## Executed evidence and release boundary

The final 134-test suite, 10 support-tool tests, clean installed-wheel acceptance,
10 screens / 9 full-quality smoke controls and live SDK probes passed. Detailed
limits and original failed attempts are in `VERIFICATION_0_3.md`. Packaging is
performed by `tools/package_release.py`; its external receipt is the final gate
for the complete ZIP and contains the verified file count and ZIP SHA-256.

Desktop/provider commissioning, a distinct-UID owner boundary, upstream source
builds beyond the observed distro runtime, a new cp312/OCI installation and an
MCP-v2 server migration were not executed. These are explicitly retained host
checks, not checked-off integration claims.
