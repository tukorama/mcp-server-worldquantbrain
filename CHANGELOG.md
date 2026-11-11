# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow
[SemVer 2.0.0](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `draw_mermaid` draws a LangGraph agent as a Mermaid flowchart, with the tools grouped by area and
  by what they can change.

## [0.1.0] - 2026-11-11

### Added

- MCP server with 25 tools, plus `submit_alpha` behind `WQB_ALLOW_SUBMIT`.
- Native LangChain tools and a prebuilt LangGraph agent in the `langgraph` extra.
- Stdio and authenticated streamable HTTP transports.
- Saved sessions, biometric login hand-off and a simulation ledger.
- Built against `mcp` 2.3.

[Unreleased]: https://github.com/tukorama/mcp-server-worldquantbrain/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/tukorama/mcp-server-worldquantbrain/releases/tag/v0.1.0
