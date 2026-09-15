# Provenance and redistribution notes

New Compression Lab engine, tests, workflow text and native control wrapper:
MIT, as in LICENSE. No fonts or model weights are included.

`hcb.py` and `stream.py` selectively reuse the strict parsers supplied by the
operator in the implementation brief. The attached reference material did not
carry a standalone repository license. These files retain their original
comments; this handoff to the supplying operator does not assert permission for
unrestricted third-party redistribution of that reference material. The original
brief is preserved separately, including its manifest, source provenance and
library source distributions. Public smoke objects and regression fixtures are
unchanged, except that the demo writes a normalized *copy* of their manifest.

The 0.4 native controls use pinned source-built static libraries: Zstandard 1.5.7
(BSD option), LZ4 1.10.0 (BSD-2-Clause), Brotli 1.2.0 (MIT), XZ 5.8.3,
zlib 1.3.2 and bzip2 1.0.8. The verified native prefix contains each upstream
license. Generated candidates retain their selected codec's notices, charge
those files in the declared decoder runtime and include them in exports.
`dependencies/native-lock.json` and the native build receipt identify exact
source archives, hashes, build outputs and probes. Historical shared-library
controls retain the actual installed dependency closure and original notices.
Platform libc, libm, libstdc++, libgcc and the ELF loader are inventoried but are
not bundled as candidate runtime files. The declared accounting policy assumes
that this platform runtime is already installed.

The supplied Python source distributions are not used as timing proxies. Their
binding versions (zstandard 0.25.0, lz4 4.4.5, Brotli 1.2.0) are distinct from the
native library versions selected by each candidate. The unchanged brief records their provenance.
The old evaluator and historical candidate implementations are not installed or
executed by this package.

The optional MCP adapter uses the official Python MCP SDK 1.29.1. The source
release includes its 29-package, hash-locked CPython 3.12 wheelhouse; upstream
license metadata accompanies those wheels. No authentication material, provider
account, model weights or imitation MCP protocol is included. Local installation and transport verification are recorded in release check receipts.
The inherited 0.3 SDK 2.1.1 client-interoperability claim is historical evidence.

The Moby-derived seccomp/AppArmor profiles are Apache-2.0; attribution, original
source commit and `runtime/oci/MOBY-LICENSE` are included. The OCI base is the
official Ubuntu image and uses its signed package/license inventories. Candidate
Python runtimes are generated from the operator's installed distribution and
charged. This source ZIP does not redistribute the copied mutable interpreter
or its dependency binaries; operators must supply their distribution notices
when separately distributing such candidate exports.
