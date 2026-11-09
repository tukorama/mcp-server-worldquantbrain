from collections.abc import Callable

from mcp.server import MCPServer

from mcp_server_worldquantbrain import knowledge
from mcp_server_worldquantbrain.client import alphas, data, simulations
from mcp_server_worldquantbrain.ops.registry import Runtime


def _add_doc(server: MCPServer, topic: str) -> None:
    def read_doc() -> str:
        return knowledge.read(topic)

    read_doc.__name__ = topic.replace("-", "_")
    server.resource(
        f"brain://docs/{topic}",
        name=topic,
        description=f"BRAIN reference: {topic.replace('-', ' ')}.",
        mime_type="text/markdown",
    )(read_doc)


def register_resources(server: MCPServer, runtime: Callable[[], Runtime]) -> None:
    # The SDK does not inject Context into static resources, hence the `runtime` getter.
    @server.resource(
        "brain://operators", description="FASTEXPR operators.", mime_type="application/json"
    )
    async def operators() -> str:
        current = runtime()
        result = await data.list_operators(current.client, current.cache)
        return result.model_dump_json()

    @server.resource(
        "brain://simulation-options",
        description="Valid simulation settings for this account.",
        mime_type="application/json",
    )
    async def simulation_options() -> str:
        current = runtime()
        result = await simulations.simulation_options(current.client, current.cache)
        return result.model_dump_json()

    @server.resource(
        "brain://alphas/{alpha_id}", description="One alpha.", mime_type="application/json"
    )
    async def alpha(alpha_id: str) -> str:
        result = await alphas.get_alpha(runtime().client, alpha_id)
        return result.model_dump_json()

    for topic in knowledge.TOPICS:
        _add_doc(server, topic)
