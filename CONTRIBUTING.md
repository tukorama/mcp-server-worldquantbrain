# Contributing

```bash
uv sync --all-extras
uv run pytest
uv run ruff check && uv run ruff format --check && uv run pyright
```

To try the server interactively:

```bash
uv run mcp dev src/mcp_server_worldquantbrain/server.py
```

Adding a tool means adding one decorated function in `src/mcp_server_worldquantbrain/ops/`. The same
function becomes the MCP tool and the LangChain tool, and its docstring is what the model reads, so
say what it returns and when to use a sibling instead. Add a test in `tests/mcp/test_tools.py`.

Tests use scripted responses. `tests/live` hits the real API and only runs with `WQB_LIVE=1`.
`WQB_LIVE_SIMULATE=1` also spends one simulation. When a live run shows a response shape that
differs from a fixture, update the fixture in the same change.

Tool names, parameters, output fields, CLI flags and environment variables are the public API and
follow [Semantic Versioning](https://semver.org/). While the version is `0.y.z`, a minor release may
change them and a patch release will not. From `1.0.0`, removing or renaming one needs a major
release.
