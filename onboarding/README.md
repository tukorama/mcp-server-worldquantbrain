# Engineering Onboarding

This directory walks you step by step through how `mcp-server-worldquantbrain` works, from the fundamentals of agentic tool calling down to simulation concurrency, quota preservation, static verification, and hands-on local labs.

Treat the Python source under [src/mcp_server_worldquantbrain/](../src/mcp_server_worldquantbrain/) as the source of truth. Every concept below maps directly to real modules and tests you can inspect and execute locally without a live WorldQuant BRAIN account.

## Curriculum

| Module | Topic | Key Files |
|---|---|---|
| [01 — Agentic Foundations](01-agentic-foundations.md) | Why MCP splits Tools, Resources, and Prompts; conditional `submit_alpha` exposure; structured error recovery | `tools.py`, `resources.py`, `prompts.py`, `knowledge/` |
| [02 — Control-Plane Architecture](02-control-plane-architecture.md) | The 6-layer dependency graph; tracing a call from MCP/LangGraph to async `httpx2` and long polling | `ops/registry.py`, `client/base.py`, `integrations/langgraph.py` |
| [03 — State, Quota, and Safety](03-state-quota-and-safety.md) | SQLite simulation ledger, single-flight expression locks, `POST` non-retry discipline, and submission gates | `client/ledger.py`, `sim_manager.py`, `ops/submit.py`, `redaction.py` |
| [04 — Hands-On Labs](04-hands-on-labs.md) | Runnable terminal exercises covering CLI introspection, mocked MCP sessions, ledger transitions, and property checks | `cli.py`, `tests/mcp/`, `tests/unit/test_invariants.py` |

## Prerequisites

Install [uv](https://docs.astral.sh/uv/) and sync the workspace development environment once:

```bash
uv sync --all-extras
```

All commands in these modules run offline against local checks and `httpx2.MockTransport` responders unless explicitly noted otherwise.
