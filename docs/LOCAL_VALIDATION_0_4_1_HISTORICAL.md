> Historical 0.4.1 record. Current setup and verification are linked from
> [README](../README.md). Referenced old results and host wrappers remain in the
> immutable 0.5.0 archive identified by `provenance/PREVIOUS_RELEASE.json`.

# Local validation of Compression Lab 0.4.1

Verified 7 September 2026 on Linux x86-64 / CPython 3.12.3. The normal wheel
was installed into a new `/opt/compression-lab-0.4.1/venv`; all 99 installed
production files matched the frozen source. Wheel SHA-256:
`feff8ca20fd80748058de7be02d8b08f5d884e4df329e0a76985830fd229059c`.
The optional MCP 1.29.1 closure installed offline with required hashes and
passed `pip check`. Authenticated HTTP MCP passed from the separate agent UID,
negotiating protocol 2025-11-25 and exposing 15 public tools.

The predecessor's complete installed suite ran 169 tests without skips: 168
passed, and one old table test assumed missing controls were omitted. Its
expectation was corrected to verify both missing controls and measured screens;
the four-test module then passed. Eleven release-tool checks also passed.
The original error and correction are retained in `results/morning-checks/`.

The first full 100 MB stored control exposed an evaluator OOM: the corruption
checker retained about 35 complete mutant archives under a 3 GiB cgroup. That
failed 0.4.0 attempt and its partial gate evidence remain preserved separately;
none of its timings are used. Version 0.4.1 generates identical cases lazily and
releases validated temporary copies. The original 35-case byte-sequence hash,
a memory bound, independent review, and six focused installed tests passed.
These tests include full native gates/export, wire compatibility and real HTTP
MCP. Unchanged successful parts of the predecessor suite were not repeated.

The [full baseline and scaling table](../results/morning-041/BASELINES.md),
CSV/JSON, raw rows and runtime fingerprints are included. All 15 controls passed
full quality on a 100,000,000-byte train stream and a separate 100,000,000-byte
development stream. Seven fresh full-development native runs per direction per
control produced 210 timed invocations. Eight controls met the encoding floor;
seven were too slow, and all remain visible. No primary row had a noise flag.

Zstd 3 was the smallest eligible control: 31,409,586 bytes V+B+A+D,
212.90 MB/s encoding, 411.09 MB/s decoding. bzip2-9 was smallest overall:
24,977,658 bytes, 40.62 MB/s encoding, 73.46 MB/s decoding. Full Zstd-3 references
at one/two/four workers produced identical archives and decoder costs, with
encoding rates 123.94 / 176.60 / 212.90 MB/s and decoding rates
257.97 / 343.62 / 411.09 MB/s.

Primary limits stayed at four physical cores, four native workers, 2 GiB native
memory and a 3 GiB enclosing evaluator. Campaign peak memory was about 1.41
decimal GB. Timing includes fresh process/namespace startup and buffered file
I/O; OS caches were not flushed. MB means 1,000,000 bytes.

These are public development measurements and local deployment checks, not
held-out generalization or cross-platform certification. Subsequent model
candidates are reported separately. Native libraries and optional Rust are
explicit host prerequisites. C++ diagnostics do not instrument precompiled
libraries; Rust checked diagnostics do not claim unsafe-memory coverage.
Historical Pro/0.3 evidence remains separately labeled. See
[primary sources](PRIMARY_SOURCES_2026-09-07.md) for versions, licenses, AnyBlox
and prompting guidance; paper results are not measurements for this host.
