import pytest

pytest.importorskip("langchain")
pytest.importorskip("langgraph")

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langgraph.types import Command  # noqa: E402
from mcp import Client  # noqa: E402

from mcp_server_worldquantbrain.integrations import langgraph as integration  # noqa: E402
from mcp_server_worldquantbrain.ops import Runtime  # noqa: E402
from mcp_server_worldquantbrain.server import create_server  # noqa: E402
from tests.conftest import fixture  # noqa: E402

pytestmark = pytest.mark.anyio


def inject(monkeypatch, brain):
    original = Runtime.create
    monkeypatch.setattr(
        integration.Runtime,
        "create",
        classmethod(lambda cls, settings, **_: original(settings, transport=brain.transport)),
    )


async def test_langchain_schemas_match_the_mcp_tools(settings, brain):
    settings.allow_submit = True
    async with Client(create_server(settings, transport=brain.transport)) as session:
        mcp_tools = {tool.name: tool for tool in (await session.list_tools()).tools}
    tools = integration.get_brain_tools(settings, include_submit=True)
    assert {tool.name for tool in tools} == set(mcp_tools)
    for tool in tools:
        schema = tool.args_schema.model_json_schema()
        assert schema.get("properties", {}) == mcp_tools[tool.name].input_schema.get(
            "properties", {}
        ), tool.name
        assert tool.description == mcp_tools[tool.name].description


async def test_submit_is_excluded_unless_requested(settings):
    settings.allow_submit = True
    assert "submit_alpha" not in {t.name for t in integration.get_brain_tools(settings)}


async def test_unexpected_exceptions_reach_the_model_as_text(settings, brain, monkeypatch):
    inject(monkeypatch, brain)
    brain.add("GET", "/data-sets", [1, 2, 3])
    tool = next(t for t in integration.get_brain_tools(settings) if t.name == "list_datasets")
    assert "Internal error" in await tool.ainvoke({})


async def test_async_tool_returns_plain_json(settings, brain, monkeypatch):
    inject(monkeypatch, brain)
    brain.add("GET", "/alphas/qMPjAxnO", fixture("alpha_detail"))
    tool = next(t for t in integration.get_brain_tools(settings) if t.name == "get_alpha")
    result = await tool.ainvoke({"alpha_id": "qMPjAxnO"})
    assert result["in_sample"]["sharpe"] == 2.25


def test_sync_tool_works_too(settings, brain, monkeypatch):
    inject(monkeypatch, brain)
    brain.add("GET", "/alphas/qMPjAxnO", fixture("alpha_detail"))
    tool = next(t for t in integration.get_brain_tools(settings) if t.name == "get_alpha")
    assert tool.invoke({"alpha_id": "qMPjAxnO"})["id"] == "qMPjAxnO"


async def test_brain_errors_reach_the_model_as_text(settings, brain, monkeypatch):
    inject(monkeypatch, brain)
    tool = next(t for t in integration.get_brain_tools(settings) if t.name == "get_dataset")
    result = await tool.ainvoke({"dataset_id": "missing"})
    assert "does not exist" in result


def tool_call_model(name: str, args: dict):
    class BindableFake(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    messages = iter(
        [
            AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "call-1"}]),
            AIMessage(content="done"),
        ]
    )
    return BindableFake(messages=messages)


async def test_agent_pauses_before_submit_and_stops_on_reject(settings, brain, monkeypatch):
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


async def test_agent_without_submit_needs_no_checkpointer(settings, brain, monkeypatch):
    inject(monkeypatch, brain)
    brain.add("GET", "/alphas/a", fixture("alpha_detail"))
    agent = integration.create_brain_agent(
        tool_call_model("get_alpha", {"alpha_id": "a"}), settings=settings
    )
    state = await agent.ainvoke({"messages": [("user", "show a")]})
    assert state["messages"][-1].content == "done"
    assert len(brain.calls("GET", "/alphas/a")) == 1
