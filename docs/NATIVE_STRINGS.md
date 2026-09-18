# Native string workflow (0.6.6.dev1)

The current package adds a native string-column workload to the existing file
workflow. Candidates provide separate encoder and decoder shared libraries using
[`codec.h`](../src/compression_lab/data/strings/codec.h). The owner supplies the
dataset, resource settings and objective in `strings.json` and `commission.json`.

The workload reports package size, fresh and warm reconstruction, and sorted
selected-row reconstruction at 1%, 3%, 10%, 30% and 100%. The optional native
research service supports candidate snapshots, reproducible builds, diagnostic
checks, matched comparisons, direct exports and a host-configured server broker.
It does not change the sealed contract of earlier file-interface runs.

For the code used in the published tables and portable replay commands, see the
[benchmark review](../benchmarks/README.md). For research service entry points,
see [`native_strings.py`](../src/compression_lab/native_strings.py) and
[`native_strings_server.py`](../src/compression_lab/native_strings_server.py).

This is the development version used by the recent runs, not a newly certified
stable release. Historical installation/audit notes are intentionally omitted
from this team-facing repository.
