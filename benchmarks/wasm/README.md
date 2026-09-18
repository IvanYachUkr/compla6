# Optional WebAssembly measurement

`driver.mjs` is the exact server WASM timer adapter. It consumes the same archives
and row IDs as `dbtext/driver.cpp`, loads the same decoder entry points, checks
output canaries, and reports open/setup, fresh reconstruction, warm reconstruction
and cleanup separately. Module compilation, instantiation, input copies and host
output copies are outside those native-style operation timers; compile and
instantiate times are written to a separate `.runtime.json`.

```sh
node driver.mjs decoder.wasm decode archive.bin restored.bin ORIGINAL_BYTES /dev/null
node driver.mjs decoder.wasm rows archive.bin selected.bin ORIGINAL_BYTES ids.u64le
```

The decoder is built from the unchanged DBText candidate C++ source with
Emscripten 6.0.9, `-O3 -DNDEBUG -msimd128 -mssse3 -fno-exceptions`,
`-sFILESYSTEM=0 -sALLOW_MEMORY_GROWTH=1 -sINITIAL_MEMORY=67108864 --no-entry`, and
exports `lab_open`, `lab_decode`, `lab_rows`, `lab_close`, `malloc`, `free`.
The bulk decoder also links LZ4 1.9.4 compiled for WASM. See
[build.py](build.py) for executable commands and pinned dependency checks.

The recorded server run used Node 24.19.0 and a pinned CPU. The main comparison
tables remain native: the WASM follow-up lost warm throughput. This is an
exploratory portable decoder comparison, not a claim that all baselines were
benchmarked in WASM. [Recorded paired results](../recorded/dbtext/wasm.md).
