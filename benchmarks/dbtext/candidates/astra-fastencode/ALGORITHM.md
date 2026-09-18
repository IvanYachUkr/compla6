# Fast-encoding typed bulk variant

One complete original column forms each archive. The encoder validates the existing exact typed representations for fixed-prefix decimal identifiers, fixed-width two/four-symbol strings, canonical hexadecimal integers, verified constant-template UUIDs, and bounded coordinates. Those archives retain every alphabet, prefix, template and formatting parameter needed for reconstruction. Records preserve original order. The decoder uses the validated SIMD implementations, including SSSE3 UUID template filling and four-record hexadecimal batches.

All remaining columns receive one Zstd level 1 compression pass. A 16-byte header stores magic, version and original length, followed by the complete conventional frame. There is no HC alternative, phrase fitting, corpus identity switch or offline data stage. Both type validation and Zstd compression occur inside lab_encode, from raw bytes to the final archive.

lab_open validates framing and retains archive pointers or constructs the small archive-derived typed state. lab_decode reconstructs the complete exact column bytes; every LF and final unterminated row remains unchanged. This bulk variant does not implement selective-row access. No decoded-column cache or auxiliary artifact is used.

The custom decoder, its code constants and all archive bytes are charged. The separately pinned conventional Zstd library is excluded only under the standard-codec-available convention and included in strict deployment. SSSE3 is a CPU requirement. Data-independent compilation is recorded separately from compression.

This is an explicit operating tradeoff: compression should approach the Zstd1 baseline and beat the more elaborate phrase portfolio, while decoding is expected to be slower than the chosen fast-decoding bulk variant. Matched Lab/server results determine the final claims.
