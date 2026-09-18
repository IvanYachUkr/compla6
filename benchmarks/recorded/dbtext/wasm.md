Original corpus: 39,841,347 bytes; 23 original columns; 1,492,936 rows. Same server CPU 8, one execution core, one warmup and seven measured trials. Every row uses the same native encoder as before; WASM changes only the decoder.


| Method | Payload MB | Decoder kB | Package MB | Compress MB/s | Decode fresh MB/s | Decode warm MB/s |
|---|---:|---:|---:|---:|---:|---:|
| LZ4 | 24.607 | 16.496 | 24.623 | 506.8 | 2971.0 | 3129.8 |
| Zstd1 | 15.383 | 16.536 | 15.400 | 325.1 | 1127.1 | 1236.6 |
| Astra bulk native | 15.134 | 24.328 | 15.158 | 20.7 | 4033.3 | 4210.3 |
| Astra bulk WASM | 15.134 | 16.473 | 15.150 | 20.7 | 3035.0 | 3275.0 |
| FSST | 24.941 | 16.672 | 24.958 | 168.7 | 2856.4 | 3049.7 |
| OnPair+ | 22.198 | 16.784 | 22.215 | 31.8 | 1448.1 | 4149.7 |
| Astra rows native | 17.828 | 28.304 | 17.856 | 38.0 | 4441.3 | 4621.7 |
| Astra rows WASM | 17.828 | 18.073 | 17.846 | 37.9 | 1886.6 | 3261.1 |


**Selected rows, fresh decoder — milliseconds, lower is better**

| Selected | FSST | OnPair+ | Astra native | Astra WASM |
|---|---:|---:|---:|---:|
| 1% | 1.536 | 19.012 | 0.720 | 1.715 |
| 3% | 2.505 | 19.883 | 1.338 | 5.874 |
| 10% | 4.793 | 21.610 | 2.794 | 4.448 |
| 30% | 11.335 | 25.659 | 6.760 | 13.136 |
| 100% | 33.967 | 38.310 | 17.037 | 29.711 |


**Selected rows, warm decoder — milliseconds, lower is better**


| Selected | FSST | OnPair+ | Astra native | Astra WASM |
|---|---:|---:|---:|---:|
| 1% | 1.008 | 0.781 | 0.655 | 0.766 |
| 3% | 1.966 | 1.671 | 1.214 | 1.483 |
| 10% | 3.923 | 3.337 | 2.557 | 3.017 |
| 30% | 10.127 | 7.555 | 6.368 | 7.606 |
| 100% | 32.339 | 22.496 | 16.493 | 20.684 |


Native package sizes exclude separately pinned conventional libraries. WASM package sizes count the entire module, including embedded LZ4. The runtime is an execution-platform dependency.


Fresh means decoder initialization plus reconstruction, not a cold runtime or cold disk. Warm repeats reconstruction with the initialized decoder. Native library loading and WASM module compilation/instantiation are outside the RAM-to-RAM timer; WASM startup measurements are retained separately. V8 uses eager optimized compilation before timing. Row samples and capacities exactly match the earlier native protocol.


Port selection rule: at most 10% median slowdown across full/selected decoding workloads, while preserving existing baseline wins. This is a practical reporting rule, not a statistical equivalence test.


Decisions: bulk remains an additional result; retain native in the primary table; rows remains an additional result; retain native in the primary table.
