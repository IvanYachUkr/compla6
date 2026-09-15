# Morning native factory integration

Status: **bounded integration passed on 2026-09-07**. All seven families compile
through real registration and diagnostic builds, stage separate decoder bundles,
and pass tiny exactness, directory/stream parity and corruption checks. No
official 100 MB performance benchmark or held-out compression result was run.

Stage: `handover/morning-2026-09-07/staging/compression-lab`.
Machine evidence: [summary.json](morning-native-integration-2026-09-07/summary.json),
seven per-family JSON files, dictionary/parallel JSON, and the two test logs in
`research/inputs/morning-native-integration-2026-09-07/`.

## Source provenance and integration corrections

Reviewed the supplied README, format/build documentation, `parallel_baseline.cpp`
and `parallel_workers.hpp`. The delivered ZIP's 23 manifest entries passed their
checks. Only the two new parallel source files were copied into `data/native`.
Existing `lab_crc.hpp` was byte-identical and was not overwritten; existing
`baseline.cpp` and transform helpers were not replaced.

| Asset | SHA-256 |
|---|---|
| Delivered ZIP | `d9814022884c88bdcded789ba538d609d78a5f998c2f1a2ebd2519204bd9b131` |
| `parallel_baseline.cpp` | `30698d1b99978e74ef22cda94156781400b09eeac6db7782b02d59ff5033917a` |
| `parallel_workers.hpp` | `856d211ec30e115a982f30dcf17d52dede8ae04b358d2265a97210a9a7169d25` |
| Unchanged `lab_crc.hpp` | `87a5ff131dae51905c293a10a489e7d9d77164e16034cf2c9555d8c0f7af4961` |
| Final installed native receipt | `d5c9258c1f6291825a7782748c377c16e930401a60eef14a903c3be6a2ba28e9` |

Factory corrections in `native_baselines.py`:

- Set the actual Pro macro `CL_CHUNK_SIZE=4194304`; the former
  `CL_CHUNK_BYTES` spelling was ignored by this source.
- Use LZ4 level 1 for its fast control (`parallel-lz4-1`). The source accepts
  level 1 or HC levels 3..12; old level 0 would not compile. Factory validation
  now matches those supported levels.
- Copy only the required CRC and worker headers into these new candidates.
  A local name-shadowing mistake in that integration loop was caught on the
  first factory invocation and corrected before successful registration.

The first XZ attempt correctly rejected `include/lzma.h` because its per-family
receipt entry was missing. The source/receipt owner and parent repaired receipt
generation and installed a verified receipt; no factory verification was relaxed.
The header bytes did not change. All six pinned libraries link statically, with
their licenses carried into the declared runtime and charged.

Source inspection confirmed the trailing positional dictionary argument for all
four commands, matching the factory. `CL_THREADS` counts main, and the runtime
environment caps it. The pool uses at most the largest object's chunk count,
has distinct per-worker codec state, and completes/cancels work before returning.
The CLP1 format charges a 48-byte object header and 20 bytes per chunk, with
checksums and literal fallback; the tests also include HBA transport framing.
The supplied synthetic performance claims remain inherited, unverified here.

## Fresh verification

The factory suite passed **3 tests in 208.153 seconds**, including a seven-family
subtest. It used one representative level per family: stored 0; others 1. Each
family used five synthetic records: empty, all byte values, deterministic random
bytes, repeated text and zeros. Every family passed:

- Real `candidate.register`, verification, and independently linked normal and
  ASan/UBSan encoder/decoder builds.
- Exact round trips and directory/stream parity through all four commands,
  with byte-identical normal and diagnostic archives.
- 35 malformed stream/archive cases under normal execution and 12 diagnostic
  cases, requiring nonzero exit and empty output.
- Physical decoder-only runtime inventory and exact B+A+D accounting. The
  decoder runtime excludes the encoder executable and build-time vendor assets.
  Both diagnostic ELF roles load ASan and UBSan runtimes.

| Control | Pinned version | Decoder B bytes | Decoder A bytes | D bytes |
|---|---|---:|---:|---:|
| Stored | stored-v2 | 80,176 | 165 | 0 |
| Zstd | 1.5.7 | 342,320 | 19,805 | 0 |
| LZ4 | 1.10.0 | 84,272 | 15,712 | 0 |
| Brotli | 1.2.0 | 252,208 | 1,249 | 0 |
| XZ | 5.8.3 | 145,880 | 83,443 | 0 |
| zlib | 1.3.2 | 117,040 | 1,167 | 0 |
| bzip2 | 1.0.8 | 107,832 | 2,061 | 0 |

B includes statically linked codec code. A here includes shipped license files
and the charged decoder command descriptor. D is zero because only platform
libraries remain dynamically linked. These are exact local bundle inventories,
not compression scores or a comparison of all fifteen configured recipes.

A separate Zstd dictionary case trained a 65,536-byte dictionary from 839,680
bytes of fixed training-only samples. Registration accepted the declared
training hash and rejected an empty allowlist. All four dictionary commands,
exactness, parity, twelve normal corruptions and decoder dictionary accounting
passed. This test did not train on any evaluation record.

The parallel canary used **16,777,233 canonical bytes in five chunks**. Normal
1/2/4-thread and diagnostic 4-thread runs all decoded exactly and produced the
same **265,742-byte** HBA archive:

`4a882be6f9b9568d42902c6e8f45f3c7a1a819c2b094c1230820c799d33c5cf5`

Normal decoder task peaks were 1/2/4 on CPUs `[0]`, `[0,2]`, `[0,2,4,6]`.
Diagnostic encode and decode both observed four native tasks. Polling captured
only one task during the short normal 2/4-thread encode calls in the final run;
these samples alone do not establish their peak. Each profile is a single
correctness canary, with no throughput claim.

## Diagnostic exception failure and narrow sandbox fix

The initial six compiled families passed valid diagnostic inputs but failed on
empty-stream rejection: ASan reported main-stack top zero, warned about
`__asan_handle_no_return`, and generated a stack-buffer-overflow report during
exception unwind. A separate valid minimal throw/catch program reproduced the
warning in the sandbox; the same ELF outside it returned the expected rejection
cleanly. This isolated the problem from codec behavior.

The parent explicitly authorized a narrow `_sandbox.py` change after that
reproducer. An isolated helper first proved the fix and isolation boundaries.
Only diagnostic execution now temporarily mounts a proc filesystem belonging
to the existing private PID namespace, retains a read-only bind of its own live
`self/maps`, normally unmounts the full proc, and removes the temporary mount
directory before exec. The final `/proc` contains only `self/maps`; no host proc
is bound, and no PID enumeration, environment, fd, root or `self/exe` link is
exposed. Ordinary execution still has an empty `/proc`. Proc's locked
nosuid/nodev/noexec flags are preserved on the read-only remount.

`tests/test_sanitizer_proc_maps.py` passed **4 tests in 3.113 seconds**. It checks
expected exceptions without sanitizer false positives, exact proc contents,
the current post-exec stack and executable mappings, read-only enforcement,
parent-PID/other-proc denial, and the unchanged normal runtime. A real heap
overflow still produces an ASan heap-buffer-overflow report; a real stack
overflow still fails its bounds diagnostic. No suppression or gate weakening
was introduced. Missing `/proc/self/exe` can still cause executable-name lookup
warnings; these are not sanitizer violation reports.

The minimal source, ELF and before/after measurements are retained in
`morning-native-integration-2026-09-07/asan-exception/`. The initial attempted
direct maps bind is recorded as a setup failure, not a successful result.
The source rationale is consistent with GCC 13.3's sanitizer implementation:
failure to read process mappings sets the main-stack bounds to zero.
[GCC 13.3 sanitizer source](https://raw.githubusercontent.com/gcc-mirror/gcc/releases/gcc-13.3.0/libsanitizer/sanitizer_common/sanitizer_linux_libcdep.cpp),
version-tagged source, individual file publication date unavailable; accessed
2026-09-07. The generic runtime warning references
[sanitizer issue 189](https://github.com/google/sanitizers/issues/189), opened
2015-08-31; accessed 2026-09-07. That issue concerns stack switching and is not
itself evidence of a codec bug.

## Reproduce and scope

Run from the stage root, with its exact `src` directory selected:

```sh
COMPRESSION_LAB_NATIVE_PREFIX=/opt/compression-lab-native-2026-09-07 \
PYTHONPATH="$PWD/src" aa-exec -p compression-lab-evaluator -- \
/opt/compression-lab-0.2.2/venv/bin/python -m unittest discover -s tests -p test_native_factory_v2.py -v

PYTHONPATH="$PWD/src" aa-exec -p compression-lab-evaluator -- \
/opt/compression-lab-0.2.2/venv/bin/python -m unittest discover -s tests -p test_sanitizer_proc_maps.py -v
```

The optional `COMPRESSION_LAB_NATIVE_TEST_EVIDENCE` directory retains raw native
canary JSON. Factory tests skip without the pinned prefix; sanitizer tests do not.

Task-owned changes are the two new native source files, factory integration
corrections, two focused test files, and the explicitly authorized diagnostic
maps helper. Parent changes to compact build metadata, inventory display,
receipt installation and release packaging are separate. Complete installed
acceptance, all-recipe benchmarks, finite enclosing resource commissioning,
held-out evidence and the official 100 MB workload remain parent responsibilities.
