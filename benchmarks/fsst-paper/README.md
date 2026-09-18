# Later comparison: our wrapper around the FSST paper benchmark

Start with **[wrapper.cpp](wrapper.cpp)** (83 lines). This is our code, separated
from the authors' unchanged **[filtertest.cpp](../upstream/fsst/paper/filtertest.cpp)**.
[Recorded table](../recorded/fsst-paper/README.md).

The wrapper loads the Astra/OnPair+ encoder and decoder libraries, turns the
paper's string vector into the LF-preserving input expected by our codecs, and
returns the complete archive size. Before selective decoding it opens the
archive and initializes decoder state.

For each timed request it converts row IDs, calls `lab_rows`, and returns the
reconstructed byte count. Conversion and returned-offset production are inside
that timed call. Initialization is outside the paper's selective timer.

The authors' code still owns row selection, sorting, warmup, repeated timing,
byte comparisons and geometric-mean aggregation. Their existing FSST/LZ4 runners
remain in that file. [run.py](run.py) enables `DEBUG=1`, pins the CPU and repeats
the whole replay three times with rotated method order.

For a focused review of our additions, read the wrapper and the
[protocol comparison](../README.md#the-three-protocols). Consult the upstream
source when checking one of its inherited choices. It is not mixed into our
wrapper, and the original DBText timer is not used by this path.

`wrapper.cpp` was previously named `adapter.cpp`; its source bytes are unchanged.
