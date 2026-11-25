# 03 — State, Quota, and Safety

Automated quantitative research can burn simulation quota, duplicate requests under concurrency, or submit unvetted alphas. This server places deterministic local controls in front of WorldQuant BRAIN.

---

## 1. Environment & Secret Boundaries

- **Trusted environment vs. `.env`:** `Settings` in [config.py](../src/mcp_server_worldquantbrain/config.py) blocks `WQB_BASE_URL`, `WQB_CREDENTIALS_FILE`, `WQB_SESSION_FILE`, `WQB_CACHE_DIR`, `WQB_ALLOW_SUBMIT`, `WQB_SUBMIT_*`, and `WQB_HTTP_BEARER_TOKEN` from being loaded via `.env` files in arbitrary working directories.
- **Private files on disk:** [client/session.py](../src/mcp_server_worldquantbrain/client/session.py) writes credentials and session state using atomic `mkstemp` (`0o600` on POSIX) and `os.replace`.
- **Non-local HTTP guard:** [http_app.py](../src/mcp_server_worldquantbrain/http_app.py) refuses to bind `streamable-http` on a non-loopback host without `WQB_HTTP_BEARER_TOKEN`.
- **Secret redaction & repo hygiene:** [redaction.py](../src/mcp_server_worldquantbrain/redaction.py) masks registered credentials (and base64 forms), `Authorization` headers, `cookie`, `password=`, JWTs, and email addresses, while [.gitleaks.toml](../.gitleaks.toml) checks commits in CI.

---

## 2. Write Non-Retry & Cancellation Shielding

In [client/retry.py](../src/mcp_server_worldquantbrain/client/retry.py), `RETRY_UNSAFE` ensures `POST` calls never retry on `5xx` statuses or mid-flight network timeouts once a socket connection is established (only known-unprocessed `429` or `ConnectError`/`ConnectTimeout` can pause/retry).

Inside `SimulationManager._start()` ([sim_manager.py](../src/mcp_server_worldquantbrain/sim_manager.py)), the initial `POST /simulations` and SQLite `ledger.record()` run inside `asyncio.shield(...)`. Even if an MCP client cancels a tool call mid-flight, recording the returned `simulation_id` completes before releasing the fingerprint lock so the next retry joins the running simulation instead of spending quota twice.

---

## 3. The SQLite Simulation Ledger

[client/ledger.py](../src/mcp_server_worldquantbrain/client/ledger.py) tracks every simulated expression + canonical settings JSON by SHA-256 fingerprint (`normalize_expression()` strips all whitespace before hashing):

```text
  WAITING / SIMULATING / RUNNING / QUEUED / PENDING ──▶ COMPLETE / WARNING (+ alpha_id)
                         │
                         └──▶ ERROR / STOPPED / FAILED / CANCELLED / UNKNOWN
```

Two hard invariants govern the ledger table:

1. **Closed status alphabet:** `ledger_status()` maps every status `record()` and `update_by_id()` receive onto `VALID_LEDGER_STATUSES`. A final status outside it, such as BRAIN's `FAIL`, is filed as `FAILED`, so a write never raises and a row never stays `WAITING`. Callers still get BRAIN's own status and message.
2. **Monotonic `alpha_id` preservation:** `ON CONFLICT` and `update_by_id()` (`COALESCE(?, alpha_id)` with `WHERE alpha_id IS NULL OR ? IS NOT NULL`) guarantee that a subsequent `WAITING` or `ERROR` rerun (`force=True`) never erases a previously recorded `alpha_id`. File mode is tightened to `0o600` on POSIX whenever the database is opened.

Server processes that share the cache directory share the ledger. Before checking and starting a fingerprint, `SimulationManager` reserves it in the `reservations` table, in the same `BEGIN IMMEDIATE` transaction that reads its row, so a second process waits and joins the simulation instead of posting it again. A held reservation is renewed every 20 seconds and expires a minute after its process dies. When the ledger cannot be read or written, simulations are refused instead of run unchecked, unless `force=True`.

---

## 4. Multi-Gate Submission Safety (`submit_alpha`)

`submit_alpha` in [ops/submit.py](../src/mcp_server_worldquantbrain/ops/submit.py) only executes after passing four independent barriers:

1. **Closed tool exposure:** omitted completely unless `WQB_ALLOW_SUBMIT=true`.
2. **Human confirmation:** requests explicit approval via MCP Elicitation (or LangGraph `interrupt()` when using `create_brain_agent`), failing closed when declined.
3. **Alpha status & pre-submission checks:** verifies `status == "UNSUBMITTED"`, runs `check_submission()`, and refuses on any `FAIL`/`ERROR` or unresolved non-correlation `PENDING` check.
4. **Fail-closed self-correlation gate:** calls `get_correlation(..., "self", threshold=WQB_SUBMIT_CORRELATION_GATE)` (`0.7` by default) and refuses if unavailable (`403`), still computing, `passes is None`, or at/above threshold.

Only after all four checks pass does it issue a single non-retried `POST /alphas/{id}/submit`.

Next: [04 — Hands-On Labs](04-hands-on-labs.md)
