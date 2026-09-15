> **Historical 0.2.2-era document.** Retained for context, not a fresh 0.3
> execution claim. Current entrypoints are `../README.md`, `MIGRATION_0_3.md`
> and `VERIFICATION_0_3.md`. Do not run old machine-specific commands blindly.

# Compatibility evidence, 2026-09-06

The local integration uses the official MCP server SDK 1.29.1 with an offline
29-package wheel/hash lock. Real SDK tests initialize/list/profile over stdio,
exercise mutable register/evaluate/status/resume/export, and test authenticated
loopback HTTP including bad tokens and foreign browser origins. A separate
Prime client runtime using MCP SDK 2.1.1 successfully called the SDK 1.29.1 server.

Prime 0.9.2 is installed in a protected runtime with its own kernel environment.
The default launcher was updated while the existing 0.7.2 HRTIM daemons and their
kernel remained intact. One live Luna/high run used native RLM and native MCP
through a dedicated unprivileged UID and filesystem namespace. It completed
profile/baseline discovery, candidate edits, registration, quick/full evaluation,
status/artifacts, resume and export. Its novel Hadoop codec did not beat the
best conventional control and missed the 100 MB/s encoding floor. The host's
estimated cost is not an invoice. All provider credentials remain outside the
repository. No purchase or reset credit was used.

Codex 0.153.4 recognized the generated project MCP entry using its real `mcp get`
command. Disconnect restored the original configuration. No separate Codex
candidate-building model trial was run. ZCode 3.11.2-6792's installed guide and
configuration precedence were checked; preservation/idempotence fixtures pass,
but a ZCode model session was not exercised.

Connection operations serialize writes, preserve unrelated TOML/JSON entries,
back up owned state and refuse conflicts or removal of edited entries. Prime
uses documented `mcp add/get/remove`; a same-UID launch alone does not provide
private isolation. Use the protected agent launcher and separate evaluator for
live work that must exclude private data.

The earlier Pro builder's skipped SDK/live-client statements describe only the
immutable original archive. Current execution records are in
`../results/local-integration/` and the repository integration report.

Official interfaces, accessed 2026-09-06:
[Prime 0.9.2 release](https://github.com/PrimeIntellect-ai/prime-agent/releases/tag/v0.9.2)
(published 2026-09-05),
[Prime MCP](https://github.com/PrimeIntellect-ai/prime-agent/blob/main/packages/coding-agent/docs/mcp-integrations.md),
[Codex MCP](https://developers.openai.com/codex/mcp/),
[ZCode MCP](https://zcode.z.ai/en/docs/mcp-services),
[official MCP SDK](https://github.com/modelcontextprotocol/python-sdk).
The documentation is maintained; local CLI/protocol/run records establish the
specific installed compatibility claims above.

The subsequent fresh Sol/high session also used native RLM/MCP and finished with
an eligible public-development improvement over conventional dictionary Zstd-1.
The default persistent service selects its workspace. The deployed optional client
helper was then verified with SDK 2.1.1 under the actual agent UID: automatic public
source permission repair, compact output, cache-hit registration and readable,
hash-verified public export, with continued private/ledger denial and zero model calls.
The discoverable workspace skill includes these recipes and the distinction between
conventional controls and historical custom-codec reference runs.
