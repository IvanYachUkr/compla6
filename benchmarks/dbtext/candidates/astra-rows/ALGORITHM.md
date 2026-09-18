# Shared pruned phrases with direct row access

One archive contains one complete original column. The ABI accepts raw bytes and preserves LF terminators, the final unterminated row if present, row order and all byte values. Encoder and decoder are separate native libraries.

## Fitting and archive construction

The encoder first validates exact structured formats: a learned fixed prefix followed by decimal integers; fixed-width strings over a small learned alphabet; canonical hexadecimal integers; UUIDs with verified invariant positions; and the corpus's bounded decimal-coordinate format. Learned prefixes, alphabets, UUID templates and coordinate integer parameters reside in the archive. Values use fixed-stride integer/nibble records or direct bit addressing. Format validation covers every original row; failures use the generic phrase format.

For other columns, the supplied OnPair+ byte16 implementation performs grammar fitting and tokenization as an encoder component. All of this runs inside lab_encode. The custom archive and decoder then apply three further steps:

1. Visit the pair grammar from newest to oldest. Remove a used phrase if replacing its occurrences by its two children saves more expanded dictionary and descriptor bytes than it adds in tokens and newly activated child entries. Propagate frequencies and recursively rewrite the token stream.
2. Remove unused phrases. Process used phrases from longest to shortest, storing a phrase only if its bytes are not already present inside a longer stored phrase. A hash table of used phrases and exact byte comparisons locates shared substrings. Descriptors contain a 24-bit pool offset and an 8-bit length. No phrase is longer than 16 bytes; a charged 16-byte pool tail permits fixed-size reads.
3. Store every row's token offset as a 16-bit displacement from a 32-bit group base. A group starts with 128 rows; fitting reduces that power-of-two size if needed to fit the displacement. Group bases and every row boundary are charged archive bytes.

The text frame contains a 32-byte header (magic/version, original bytes, row count, symbol count, token count and group size), the row directory, descriptors, alignment, shared dictionary bytes, read padding, and remapped 16-bit tokens. No trained information is embedded outside the archive.

## Native reconstruction

Opening a text archive checks framing dimensions and assigns pointers. It does not expand the phrase grammar, scan preceding rows or reconstruct the column. Structured opening builds only small formatting tables from archived parameters. There is no persistent uncompressed cache.

A text lookup computes two row endpoints from group bases and relative offsets, then reads only that token interval. Four-token loops load descriptors and copy dictionary bytes directly to the caller's output; bounded tails respect exact output capacity. Arbitrary IDs can be unordered or repeated. Structured lookups compute the addressed integer/bit record directly.

For at least 32 consecutive requested IDs in a text column with average row length at most 32 bytes, the decoder expands that requested interval in one stream. SSE2 LF comparisons scan only its reconstructed output to return row boundaries. This reduces dispatch for the 100% workload while preserving direct access to subsets. Other requests use independent indexed rows.

## Accounting and timing

All recognition, fitting, grammar pruning, substring sharing, index construction and serialization run inside lab_encode. No offline fitting or generated-code stage is required. Data-independent compiler builds are accounted for separately. Decoder.so contains the entire custom decoder and constants; there is no conventional codec dependency in reconstruction and no auxiliary learned artifact. Archive bytes include all payload, framing, indexes, tables and padding. Lab charges the complete custom decoder once per corpus and retains strict deployment totals.

The x86-64 decoder uses SSSE3 byte shuffles to fuse UUID formatting into three bounded overlapping vector stores, with constants derived from the archived template during open. Hexadecimal full reconstruction batches four fixed eight-digit records when possible and uses a scalar fallback for shorter values. Random row addressing and the archive format are unchanged. These are decoder algorithms and small formatting tables, not fitted artifacts or uncompressed row caches. The build explicitly enables SSSE3.
