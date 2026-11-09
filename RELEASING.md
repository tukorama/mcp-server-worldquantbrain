# Releasing

Once, before the first release:

1. On PyPI, add a pending trusted publisher: project `mcp-server-worldquantbrain`, owner `tukorama`,
   repository `mcp-server-worldquantbrain`, workflow `release.yml`, environment `pypi`.
2. In the GitHub repository settings, create the `pypi` environment.
3. Optionally try a TestPyPI upload: `uv build --no-sources && uv publish --index testpypi`.

Every release:

1. Run the live suite: `WQB_LIVE=1 WQB_LIVE_SIMULATE=1 uv run pytest -m live`.
2. `uv version --bump patch` (or `minor`, `major`), and set the same version in `server.json`.
   Skip this for 0.1.0, which is already set.
3. Date the entry in `CHANGELOG.md` and commit.
4. `git tag -a v$(uv version --short) -m v$(uv version --short) && git push --follow-tags`.
5. After the workflow finishes, publish to the MCP Registry:
   `mcp-publisher login github && mcp-publisher publish`.
