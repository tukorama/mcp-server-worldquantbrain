"""Run a BRAIN research agent.

    uv run --extra langgraph --with langchain-anthropic python examples/langgraph_agent.py
    uv run --extra langgraph --with "langchain[mcp]" --with langchain-anthropic \
        python examples/langgraph_agent.py --mcp

Log in once first with `mcp-server-worldquantbrain auth`, and set ANTHROPIC_API_KEY.
"""

import argparse
import asyncio
from pathlib import Path

MODEL = "anthropic:claude-sonnet-5"
QUESTION = "Find a simple momentum alpha for USA TOP3000, simulate it, and report its stats."
PROJECT = str(Path(__file__).resolve().parent.parent)


async def native() -> None:
    from mcp_server_worldquantbrain.integrations.langgraph import create_brain_agent

    agent = create_brain_agent(MODEL)
    config = {"configurable": {"thread_id": "example"}}
    result = await agent.ainvoke({"messages": [("user", QUESTION)]}, config=config)
    print(result["messages"][-1].content)


async def over_mcp() -> None:
    from langchain.agents import create_agent
    from langchain.mcp import MCPAdapter

    config = {
        "mcpServers": {
            "brain": {
                "command": "uvx",
                "args": ["--from", PROJECT, "mcp-server-worldquantbrain"],
                # The subprocess only inherits HOME, PATH and a few others. Pass WQB_* here if you
                # do not use the default credentials file. Timeouts are in milliseconds.
                "timeout": 1_200_000,
            }
        }
    }
    async with MCPAdapter(config) as adapter:
        agent = create_agent(MODEL, await adapter.list_tools())
        result = await agent.ainvoke({"messages": [("user", QUESTION)]})
    print(result["messages"][-1].content)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mcp", action="store_true", help="use the MCP server instead")
    asyncio.run(over_mcp() if parser.parse_args().mcp else native())
