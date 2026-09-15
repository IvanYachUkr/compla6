> **Historical 0.2.2-era document.** Retained for context, not a fresh 0.3
> execution claim. Current entrypoints are `../README.md`, `MIGRATION_0_3.md`
> and `VERIFICATION_0_3.md`. Do not run old machine-specific commands blindly.

# Integration evidence and target versions

Access date 2026-09-06. Use maintained public interfaces and capability detection. The CLI is the universal baseline; MCP is an optional structured transport; hooks are optional.

## Prime Agent

Current stable release v0.9.2, published 2026-09-05:
https://github.com/PrimeIntellect-ai/prime-agent/releases/tag/v0.9.2

Official tarball SHA-256 verified locally: `d649b9f0258c77de7d02aba33d786925ce41a36b601e8e24eef0f00bdc2f1f49`.

The installed 0.9.2 CLI's help and read-only MCP list were verified. Documented stdio registration:

```sh
prime-agent mcp add compression-lab --cwd /absolute/project -- /absolute/python -m compression_lab.mcp_server
prime-agent mcp list
prime-agent mcp get compression-lab
prime-agent mcp remove compression-lab
```

Its Python runtime exposes `import mcp`, `await mcp.list_tools("compression-lab")`, and `await mcp.call_tool("compression-lab", "profile", {...})`.

Docs: https://github.com/PrimeIntellect-ai/prime-agent/blob/main/packages/coding-agent/docs/mcp-integrations.md

The local installation preserved existing old daemons and now points the default launcher to 0.9.2. The later isolated Luna run and actual MCP protocol tests are recorded in COMPATIBILITY_EVIDENCE.md. Never run an unknown help subcommand on a host that may interpret it as a prompt.

## Codex

Local `codex mcp add --help` was checked. The standard form is `codex mcp add NAME -- COMMAND ARGS...`; use a project entry where appropriate and preserve existing settings. Shared `.agents/skills` is documented.

https://learn.chatgpt.com/docs/extend/mcp?surface=cli
https://learn.chatgpt.com/docs/build-skills

## ZCode

https://zcode.z.ai/en/docs/mcp-services

Cross-checked with installed documentation: native workspace `.zcode/config.json` uses `mcp.servers`; shared `.agents/mcp.json` uses `mcpServers`. Native MCP configuration in the same scope can override the shared fallback. Merge the package's owned entry into the active source rather than overwriting the file or blindly creating an ignored fallback. Shared `.agents/skills` is supported.

## Runtime libraries

https://modelcontextprotocol.io/specification/2025-06-18/server/tools
https://github.com/modelcontextprotocol/python-sdk/releases
https://clang.llvm.org/docs/AddressSanitizer.html
https://llvm.org/docs/LibFuzzer.html

Use the official MCP SDK and pin the API version actually exercised; recent SDK changes mean old import examples must be checked. Do not ship an imitation JSON endpoint and call it MCP.
