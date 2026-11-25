# 02 — Control-Plane Architecture

Every Python module inside [src/mcp_server_worldquantbrain/](../src/mcp_server_worldquantbrain/) belongs to one of six architectural layers. Dependencies flow strictly downward, enforced on every test run by `test_strict_architectural_layer_dag` in [tests/test_coverage.py](../tests/test_coverage.py).

---

## 1. The Six Architectural Layers

| Layer | Modules | Responsibility |
|---|---|---|
| **5 — Surface** | `cli.py`, `server.py`, `resources.py`, `tools.py`, `integrations/langgraph.py`, `__main__.py` | `stdio` and `streamable-http` MCP serving, LangGraph bridge & Mermaid export, interactive `auth`/`status`/`tools` CLI |
| **4 — Operations & HTTP** | `ops/*`, `http_app.py` | 25 typed `@operation` definitions, client wait capping (`Call`), Starlette HTTP bearer-auth middleware & `/health` |
| **3 — Simulation Orchestration** | `sim_manager.py` | Single-flight expression locking, local concurrency semaphore, batch region/10-item chunking, cancellation-shielded `POST` recording |
| **2 — Package Root & Protocol Client** | `__init__.py`, `client/*` | Async `BrainClient` (`httpx2`), session cookie jar persistence, biometric inquiry state, `Retry-After` handling, SQLite ledger & JSON disk cache |
| **1 — Configuration & Domain Models** | `config.py`, `models/*` | `Settings` (with `.env` restriction for security-sensitive fields), `CompactModel`, simulation payload builders & response normalization |
| **0 — Kernel** | `errors.py`, `redaction.py`, `logging_setup.py`, `prompts.py`, `knowledge/` | Exception taxonomy (`for_agent()`), regex & registered secret scrubbing, HTML stripping, embedded FASTEXPR/settings docs |

An AST check parses every source file and fails CI if a lower layer ever imports a higher layer.

---

## 2. Anatomy of a Tool Call

Tracing `simulate_alpha` or `submit_alpha` end to end:

1. **Surface ([tools.py](../src/mcp_server_worldquantbrain/tools.py)):** inspects the op signature, injects `Call(runtime=..., client_wait_cap=...)`, and runs pre-call confirmation if `op.requires_submit_flag` and `WQB_SUBMIT_CONFIRM=elicit`.
2. **Operations ([ops/simulations.py](../src/mcp_server_worldquantbrain/ops/simulations.py)):** validates arguments via Pydantic `Field` bounds (`delay`, `decay`, `truncation`), constructs the canonical payload with `simulation_payload()`, and delegates to `SimulationManager.simulate()`.
3. **Orchestration ([sim_manager.py](../src/mcp_server_worldquantbrain/sim_manager.py)):** acquires the normalized expression lock, checks `Ledger.get()`, acquires a slot from the concurrency `Semaphore`, shields `_start()` from task cancellation so accepted `POST /simulations` IDs are always saved, and polls until completion or client deadline.
4. **Protocol Client ([client/base.py](../src/mcp_server_worldquantbrain/client/base.py)):** injects saved session cookies, adopts refreshed sessions from disk across processes via file `mtime`, refreshes expired `401` sessions once, respects `Retry-After`, and leaves `POST` retries off on ambiguous server/network failures.
5. **Redaction ([redaction.py](../src/mcp_server_worldquantbrain/redaction.py)):** any `BrainError` or unexpected exception passes through `scrub()` so passwords, basic/bearer headers, cookies, JWTs, and emails never reach agent logs or tool errors.

---

## 3. Session Sharing & Optional Extras

- **Multi-process sessions:** `SessionStore` writes session cookies atomically via temporary file + `0o600` permissions + `os.replace`. If the CLI `auth` command refreshes a cookie on disk, a running MCP server detects the new `mtime` before forcing a new login. After three rejected passwords in a row the server stops logging in until such a session appears.
- **Lightweight default install:** `pyproject.toml` puts `langchain`, `langchain-core`, and `langgraph` in `[project.optional-dependencies]`, verified by the `base-install` CI job so headless MCP deployments stay small and fast.

Next: [03 — State, Quota, and Safety](03-state-quota-and-safety.md)
