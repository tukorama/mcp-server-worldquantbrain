from pathlib import Path
from typing import Any, cast

import pytest

pytest.importorskip("langchain")
pytest.importorskip("langgraph")

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command
from mcp import Client

from mcp_server_worldquantbrain.integrations import langgraph as integration
from mcp_server_worldquantbrain.ops import Runtime
from mcp_server_worldquantbrain.server import create_server
from tests.conftest import fixture

pytestmark = pytest.mark.anyio

README = Path(__file__).parents[2] / "README.md"


def inject(monkeypatch: Any, brain: Any) -> Any:
    original = Runtime.create

    def fake_create(_cls: Any, settings: Any, **_: Any) -> Runtime:
        return original(settings, transport=brain.transport)

    monkeypatch.setattr(integration.Runtime, "create", classmethod(fake_create))


async def test_langchain_schemas_match_the_mcp_tools(settings: Any, brain: Any) -> None:
    settings.allow_submit = True
    async with Client(create_server(settings, transport=brain.transport)) as session:
        mcp_tools = {tool.name: tool for tool in (await session.list_tools()).tools}
    tools = integration.get_brain_tools(settings, include_submit=True)
    assert {tool.name for tool in tools} == set(mcp_tools)
    for tool in tools:
        assert tool.args_schema is not None
        assert not isinstance(tool.args_schema, dict)
        schema_model = cast("type[Any]", tool.args_schema)
        schema = cast("dict[str, Any]", schema_model.model_json_schema())
        assert schema.get("properties", {}) == mcp_tools[tool.name].input_schema.get(
            "properties", {}
        ), tool.name
        assert tool.description == mcp_tools[tool.name].description


async def test_submit_is_excluded_unless_requested(settings: Any) -> None:
    settings.allow_submit = True
    assert "submit_alpha" not in {t.name for t in integration.get_brain_tools(settings)}


async def test_unexpected_exceptions_reach_the_model_as_text(
    settings: Any, brain: Any, monkeypatch: Any
) -> None:
    inject(monkeypatch, brain)
    brain.add("GET", "/data-sets", [1, 2, 3])
    tool = next(t for t in integration.get_brain_tools(settings) if t.name == "list_datasets")
    assert "Internal error" in await tool.ainvoke({})


async def test_async_tool_returns_plain_json(settings: Any, brain: Any, monkeypatch: Any) -> None:
    inject(monkeypatch, brain)
    brain.add("GET", "/alphas/qMPjAxnO", fixture("alpha_detail"))
    tool = next(t for t in integration.get_brain_tools(settings) if t.name == "get_alpha")
    result = await tool.ainvoke({"alpha_id": "qMPjAxnO"})
    assert result["in_sample"]["sharpe"] == 2.25


def test_sync_tool_works_too(settings: Any, brain: Any, monkeypatch: Any) -> None:
    inject(monkeypatch, brain)
    brain.add("GET", "/alphas/qMPjAxnO", fixture("alpha_detail"))
    tool = next(t for t in integration.get_brain_tools(settings) if t.name == "get_alpha")
    assert tool.invoke({"alpha_id": "qMPjAxnO"})["id"] == "qMPjAxnO"


async def test_brain_errors_reach_the_model_as_text(
    settings: Any, brain: Any, monkeypatch: Any
) -> None:
    inject(monkeypatch, brain)
    tool = next(t for t in integration.get_brain_tools(settings) if t.name == "get_dataset")
    result = await tool.ainvoke({"dataset_id": "missing"})
    assert "does not exist" in result


def tool_call_model(name: str, args: dict[str, Any]) -> Any:
    class BindableFake(GenericFakeChatModel):
        def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
            return self

    messages = iter(
        [
            AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "call-1"}]),
            AIMessage(content="done"),
        ]
    )
    return BindableFake(messages=messages)


async def test_agent_pauses_before_submit_and_stops_on_reject(
    settings: Any, brain: Any, monkeypatch: Any
) -> None:
    settings.allow_submit = True
    inject(monkeypatch, brain)
    agent = integration.create_brain_agent(
        tool_call_model("submit_alpha", {"alpha_id": "a"}), settings=settings
    )
    config = {"configurable": {"thread_id": "t1"}}
    state = await agent.ainvoke({"messages": [("user", "submit a")]}, config=config)
    assert state["__interrupt__"]
    assert brain.calls("POST", "/alphas/a/submit") == []
    interrupt = state["__interrupt__"][0]
    await agent.ainvoke(
        Command(resume={interrupt.id: {"type": "reject", "message": "no"}}), config=config
    )
    assert brain.calls("POST", "/alphas/a/submit") == []
    assert brain.calls("GET", "/alphas/a") == []


async def test_agent_without_submit_needs_no_checkpointer(
    settings: Any, brain: Any, monkeypatch: Any
) -> None:
    inject(monkeypatch, brain)
    brain.add("GET", "/alphas/a", fixture("alpha_detail"))
    agent = integration.create_brain_agent(
        tool_call_model("get_alpha", {"alpha_id": "a"}), settings=settings
    )
    state = await agent.ainvoke({"messages": [("user", "show a")]})
    assert state["messages"][-1].content == "done"
    assert len(brain.calls("GET", "/alphas/a")) == 1


def test_readme_graph_is_current(settings: Any) -> None:
    settings.allow_submit = True
    agent = integration.create_brain_agent(tool_call_model("get_alpha", {}), settings=settings)
    drawn = integration.draw_mermaid(agent)
    assert drawn in README.read_text(encoding="utf-8"), f"Paste this graph into README.md:\n{drawn}"


def test_graph_without_submit_has_no_approval_step(settings: Any) -> None:
    agent = integration.create_brain_agent(tool_call_model("get_alpha", {}), settings=settings)
    drawn = integration.draw_mermaid(agent)
    assert "simulate_alpha" in drawn
    for absent in ("human approval", "submit_alpha", "legend_irreversible"):
        assert absent not in drawn


def test_graph_draws_a_hand_built_graph_with_foreign_tools(settings: Any) -> None:
    def lookup_ticker(name: str) -> str:
        return name

    def end_node(state: MessagesState) -> MessagesState:
        return state

    def route_next(_state: MessagesState) -> str:
        return "stop"

    foreign = StructuredTool.from_function(
        lookup_ticker, name="lookup_ticker", description="Look up a ticker."
    )
    builder: Any = StateGraph(MessagesState)
    builder.add_node("end", end_node)
    builder.add_node("call", ToolNode([*integration.get_brain_tools(settings), foreign]))
    builder.add_edge(START, "end")
    builder.add_conditional_edges("end", route_next, {"more": "call", "stop": END})
    builder.add_edge("call", "end")
    drawn = integration.draw_mermaid(builder.compile())
    assert 'n1["end"]' in drawn
    assert 'n1 -.->|"more"| n2' in drawn
    assert 'tools_other_all["lookup_ticker"]' in drawn
    assert "update_alpha" in drawn
    assert "submit_alpha" not in drawn
