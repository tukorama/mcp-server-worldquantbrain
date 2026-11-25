# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow
[SemVer 2.0.0](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- A rejected password is retried after one minute, then two, and after three rejections in a row
  the server stops logging in and asks for `mcp-server-worldquantbrain auth`, instead of retrying
  every minute and risking a locked account.
- A redirect, an empty answer or a non-JSON page from a JSON endpoint is an error. `get_datafield`
  and `get_dataset` no longer answer one with a bare id, and a redirect on `POST /simulations` is
  no longer taken for a simulation id.

## [0.2.2] - 2026-11-19

### Added

- Explicit simulation ledger status transition validation (`VALID_LEDGER_STATES`), `0600` SQLite file permissions, and stateful property tests in `tests/unit/test_invariants.py`.
- AST architectural 6-layer dependency enforcement and `.gitleaks.toml` secret-pattern verification in `tests/test_coverage.py`.
- Strict Ruff (`C90` max-complexity `12`, `RUF`, `S`, `ASYNC`, `PT`, `PIE`, `PERF`, `RET`, `RSE`, `TID`, `PGH`, `PLC`, `PLE`) and strict Pyright across both `src/` and `tests/`.
- Progressive 4-module `onboarding/` guide and `uvx`-first setup instructions across Claude Code, Cursor, Claude Desktop, VS Code, and ChatGPT.

### Removed

- Standalone release guide.

## [0.2.1] - 2026-11-11

The `v0.2.0` tag exists but was never published, because its release workflow did not parse. This
release contains the changes listed under 0.2.0.

## [0.2.0] - 2026-11-11 [NOT PUBLISHED]

### Added

- `draw_mermaid` draws a LangGraph agent as a Mermaid flowchart, with the tools grouped by area and
  by what they can change.

### Changed

- Package summary, keywords, classifiers and the README introduction describe the project in the
  terms people search for. The README links are absolute, so they work on PyPI.
- The release workflow publishes `server.json` to the MCP Registry after the PyPI upload.

## [0.1.0] - 2026-11-11

### Added

- MCP server with 25 tools, plus `submit_alpha` behind `WQB_ALLOW_SUBMIT`.
- Native LangChain tools and a prebuilt LangGraph agent in the `langgraph` extra.
- Stdio and authenticated streamable HTTP transports.
- Saved sessions, biometric login hand-off and a simulation ledger.
- Built against `mcp` 2.3.

[Unreleased]: https://github.com/tukorama/mcp-server-worldquantbrain/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/tukorama/mcp-server-worldquantbrain/compare/v0.1.0...v0.2.1
[0.2.0]: https://github.com/tukorama/mcp-server-worldquantbrain/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/tukorama/mcp-server-worldquantbrain/releases/tag/v0.1.0
