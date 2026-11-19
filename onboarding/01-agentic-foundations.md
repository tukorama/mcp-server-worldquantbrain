# 01 — Agentic Foundations

If you are coming from traditional REST scripts or notebook wrappers, building for an AI agent requires a different interface design. An LLM does not read a monolithic client module; instead, it interacts through **typed tool schemas**, **read-only resources**, **reusable prompts**, and **actionable errors**.

---

## 1. Control Plane vs. Research Loop

A standalone script often mixes credential files, blocking sleep loops, retry heuristics, and alpha evaluation inside one notebook cell. Inside an agent host:

- **Control plane (`mcp-server-worldquantbrain`):** manages login persistence, biometric inquiry URLs, catalogue caching, simulation concurrency, long polling, and pre-submission checks over async `httpx2`.
- **Reasoning loop (the agent host or your LangGraph app):** discovers datasets and fields, writes FASTEXPR expressions, evaluates results, and decides what to backtest next.

The base package installs only runtime protocol and HTTP dependencies—no `langchain` or `langgraph` is required unless you opt into the `[langgraph]` extra.

---

## 2. The Three MCP Primitives

When Claude Code, Cursor, Claude Desktop, VS Code, or ChatGPT connects over `stdio` or `streamable-http`, the server splits responsibilities across three MCP primitives:

| Primitive | Who Triggers It | Purpose in This Repo | Source File |
|---|---|---|---|
| **Tools** | The LLM | 25 typed operations (24 by default; 25 when `WQB_ALLOW_SUBMIT=true`) with Pydantic schemas and `ToolAnnotations` (`read_only_hint`, `destructive_hint`) | [tools.py](../src/mcp_server_worldquantbrain/tools.py) |
| **Resources** | Application or LLM read | On-demand reference guides (`brain://docs/fastexpr-cheatsheet`, `settings-guide`, `submission-criteria`) and live JSON (`brain://operators`, `brain://simulation-options`, `brain://alphas/{alpha_id}`) | [resources.py](../src/mcp_server_worldquantbrain/resources.py) |
| **Prompts** | Human user / client workflow | Guided research and alpha review templates (`design_alpha`, `review_alpha`) | [prompts.py](../src/mcp_server_worldquantbrain/prompts.py) |

Keeping multi-page FASTEXPR and settings guides in `brain://docs/*` keeps tool docstrings compact so every turn uses fewer input tokens.

---

## 3. Conditional Exposure of `submit_alpha`

Out of the box, **`submit_alpha` is not registered**. With `WQB_ALLOW_SUBMIT=false` (the default), the server exposes **24** tools and physically omits submission from `tools/list`. Only when `WQB_ALLOW_SUBMIT=true` is explicitly configured in the real environment (never from an untrusted directory `.env` file) does the server expose all **25** tools.

You can inspect what tools are active under your current configuration without starting a server:

```bash
uv run mcp-server-worldquantbrain tools
```

---

## 4. Dual Delivery: MCP Server and In-Process LangGraph

Every operation is written once as a transport-agnostic `@operation` in [ops/](../src/mcp_server_worldquantbrain/ops/):

- [tools.py](../src/mcp_server_worldquantbrain/tools.py) binds those operations into an MCP server with MCP Elicitation confirmations for submission.
- [integrations/langgraph.py](../src/mcp_server_worldquantbrain/integrations/langgraph.py) wraps the same `Operation` definitions into LangChain `StructuredTool` objects backed by a single background asyncio loop (`_Bridge`) and `HumanInTheLoopMiddleware` (`Command(resume=...)`).

---

## 5. Agent-Ready Outputs and Recovery

- **Null omission:** `CompactModel` in [models/base.py](../src/mcp_server_worldquantbrain/models/base.py) strips `None` fields at serialization time.
- **Trimmed payloads:** `get_alpha_pnl` rolls cumulative PnL up by year, `get_alpha_recordset` caps returned rows (`MAX_ROWS`), and `list_messages` converts HTML messages to text while stripping base64 data URIs.
- **Actionable errors:** Exceptions in [errors.py](../src/mcp_server_worldquantbrain/errors.py) expose `.for_agent()` with concrete `Hint:` guidance (`authenticate`, check missing universe for non-USA regions, or inspect `get_alpha` before retrying).
- **Bounded waits:** Clients that close short stdio tool calls (such as `claude-ai` / Claude Desktop) automatically cap per-call blocking to **45 s** via `Call.limit_wait()` and return `status="PENDING"` with `get_simulation_status` instructions.

Next: [02 — Control-Plane Architecture](02-control-plane-architecture.md)
