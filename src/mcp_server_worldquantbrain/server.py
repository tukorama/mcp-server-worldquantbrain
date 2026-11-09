from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp.server import MCPServer

from mcp_server_worldquantbrain import __version__, knowledge
from mcp_server_worldquantbrain.config import Settings
from mcp_server_worldquantbrain.logging_setup import configure_logging
from mcp_server_worldquantbrain.ops import Runtime
from mcp_server_worldquantbrain.prompts import register_prompts
from mcp_server_worldquantbrain.resources import register_resources
from mcp_server_worldquantbrain.tools import register_tools


def create_server(
    settings: Settings | None = None, *, transport: httpx2.AsyncBaseTransport | None = None
) -> MCPServer:
    resolved = settings or Settings()
    running: list[Runtime] = []

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[Runtime]:
        runtime = Runtime.create(resolved, transport=transport)
        running.append(runtime)
        try:
            yield runtime
        finally:
            running.remove(runtime)
            await runtime.aclose()

    server = MCPServer(
        "mcp-server-worldquantbrain",
        title="WorldQuant BRAIN",
        version=__version__,
        instructions=knowledge.AGENT_GUIDE,
        lifespan=lifespan,
        log_level=resolved.log_level,
    )
    configure_logging(resolved.log_level)
    register_tools(server, resolved)
    register_resources(server, lambda: running[-1])
    register_prompts(server)
    return server


def __getattr__(name: str) -> Any:
    # `mcp dev` and `mcp run` look for a module-level `server`.
    if name == "server":
        return create_server()
    raise AttributeError(name)
