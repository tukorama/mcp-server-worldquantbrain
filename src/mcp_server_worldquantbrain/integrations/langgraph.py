"""Use BRAIN from LangChain or LangGraph directly, without an MCP subprocess.

Needs the extra: `uv add "mcp-server-worldquantbrain[langgraph]"`.
"""

import asyncio
import inspect
import threading
from collections.abc import Coroutine, Iterable
from typing import Any, TypeVar

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.tools import BaseTool, StructuredTool, ToolException
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import create_model

from mcp_server_worldquantbrain import knowledge
from mcp_server_worldquantbrain.config import Settings
from mcp_server_worldquantbrain.errors import BrainError
from mcp_server_worldquantbrain.ops import (
    OPERATIONS,
    Call,
    Operation,
    Runtime,
    enabled_operations,
)
from mcp_server_worldquantbrain.redaction import scrub

T = TypeVar("T")

BRAIN_SYSTEM_PROMPT = knowledge.AGENT_GUIDE


class _Bridge:
    """Runs every call on one background event loop.

    One loop means one HTTP client and one simulation semaphore, whether LangGraph invokes the
    tools from async code or from worker threads.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._loop = asyncio.new_event_loop()
        self._runtime: Runtime | None = None
        threading.Thread(target=self._loop.run_forever, daemon=True).start()

    async def _call(self, op: Operation, kwargs: dict[str, Any]) -> Any:
        if self._runtime is None:
            self._runtime = Runtime.create(self.settings)
        try:
            result = await op.fn(Call(runtime=self._runtime), **kwargs)
        except BrainError as exc:
            raise ToolException(scrub(exc.for_agent())) from exc
        except Exception as exc:
            raise ToolException(scrub(f"Internal error: {type(exc).__name__}: {exc}")) from exc
        return result.model_dump(mode="json")

    def submit(self, coroutine: Coroutine[Any, Any, T]) -> Any:
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)


def _args_schema(op: Operation) -> Any:
    signature = inspect.signature(op.fn, eval_str=True)
    fields: dict[str, Any] = {
        param.name: (
            param.annotation,
            ... if param.default is inspect.Parameter.empty else param.default,
        )
        for param in list(signature.parameters.values())[1:]
    }
    return create_model(f"{op.name}_input", **fields)


def _build_tool(op: Operation, bridge: _Bridge) -> BaseTool:
    def run(**kwargs: Any) -> Any:
        return bridge.submit(bridge._call(op, kwargs)).result()

    async def arun(**kwargs: Any) -> Any:
        return await asyncio.wrap_future(bridge.submit(bridge._call(op, kwargs)))

    return StructuredTool.from_function(
        func=run,
        coroutine=arun,
        name=op.name,
        description=op.description,
        args_schema=_args_schema(op),
        handle_tool_error=True,
    )


def get_brain_tools(
    settings: Settings | None = None, *, include_submit: bool = False
) -> list[BaseTool]:
    """Return LangChain tools for the enabled BRAIN operations.

    `submit_alpha` is irreversible and has no approval step of its own here, so it is left out
    unless you ask for it and have put your own human approval in the graph.
    `create_brain_agent` adds that approval for you.
    """
    resolved = settings or Settings()
    bridge = _Bridge(resolved)
    return [
        _build_tool(op, bridge)
        for op in enabled_operations(resolved)
        if include_submit or not op.requires_submit_flag
    ]


def create_brain_agent(
    model: Any,
    *,
    settings: Settings | None = None,
    tools: list[Any] | None = None,
    system_prompt: str = BRAIN_SYSTEM_PROMPT,
    middleware: list[Any] | None = None,
    checkpointer: Any = "memory",
) -> Any:
    """Build a LangGraph agent wired to BRAIN.

    When submission is enabled the graph pauses for human approval before `submit_alpha`. Resume
    it with `Command(resume=...)` from `langgraph.types`. Approvals need a checkpointer: the
    default keeps them in memory, or pass your own saver, or None if LangGraph Platform supplies
    one.
    """
    resolved = settings or Settings()
    stack = list(middleware or [])
    if resolved.allow_submit:
        stack.append(
            HumanInTheLoopMiddleware(
                interrupt_on={"submit_alpha": {"allowed_decisions": ["approve", "reject"]}},
                interrupt_mode="per_call",
            )
        )
    if checkpointer == "memory":
        checkpointer = InMemorySaver() if resolved.allow_submit else None
    return create_agent(
        model,
        [*get_brain_tools(resolved, include_submit=True), *(tools or [])],
        system_prompt=system_prompt,
        middleware=stack,
        checkpointer=checkpointer,
    )


_AREAS = {
    "auth": "Login",
    "simulations": "Simulate",
    "submit": "Submit",
    "alphas": "Alphas",
    "data": "Data",
    "users": "Account",
}
_ACCESS = {"read": "read-only", "write": "changes account state", "irreversible": "irreversible"}
_FLOWCHART = """\
---
config:
  flowchart:
    nodeSpacing: 24
    rankSpacing: 12
    padding: 8
---
flowchart TB
"""
# Fills are translucent and text has no color, so the diagram reads on light and dark pages.
_STYLES = [
    "classDef default fill:#64748b,fill-opacity:0.06,stroke:#64748b",
    "classDef read fill:#64748b,fill-opacity:0.14,stroke:#64748b",
    "classDef write fill:#d97706,fill-opacity:0.16,stroke:#d97706",
    "classDef irreversible,approval fill:#dc2626,fill-opacity:0.16,stroke:#dc2626,stroke-width:2px",
    "classDef external fill-opacity:0,stroke-dasharray:5 4",
    "classDef box fill:#64748b,fill-opacity:0.05,stroke:#64748b,stroke-opacity:0.45",
    "classDef row fill:none,stroke:none",
]


def _access(op: Operation) -> str:
    if op.destructive:
        return "irreversible"
    return "read" if op.read_only else "write"


def _subgraph(node_id: str, title: str, direction: str, body: list[str]) -> list[str]:
    return [
        f'subgraph {node_id}["{title}"]',
        f"    direction {direction}",
        *(f"    {line}" for line in body),
        "end",
    ]


def _in_order(ids: list[str]) -> list[str]:
    # Mermaid versions disagree on how to order unlinked subgraphs, so invisible links pin it.
    return [" ~~~ ".join(ids)] if len(ids) > 1 else []


def _approval_tools(node: Any) -> list[str]:
    # A middleware hook is a bound method, and the approval middleware keeps its tools on itself.
    middleware = getattr(getattr(node.data, "func", None), "__self__", None)
    interrupt_on = getattr(middleware, "interrupt_on", None)
    return list(interrupt_on) if isinstance(interrupt_on, dict) else []


def _graph_lines(drawn: Any) -> list[str]:
    # Numbered ids keep node names clear of Mermaid keywords and of the ids used around them.
    ids = {name: f"n{index}" for index, name in enumerate(drawn.nodes)}
    lines: list[str] = []
    approvals: set[str] = set()
    for name, node in drawn.nodes.items():
        if name in ("__start__", "__end__"):
            lines.append(f"{ids[name]}([{name.strip('_')}])")
        elif guarded := _approval_tools(node):
            approvals.add(name)
            label = f"human approval<br/>before {', '.join(guarded)}"
            lines.append(f'{ids[name]}{{{{"{label}"}}}}:::approval')
        else:
            lines.append(f'{ids[name]}["{name}"]')
    for edge in drawn.edges:
        label = edge.data
        if label is None and edge.source in approvals:
            label = {"tools": "approved", "model": "rejected"}.get(edge.target)
        arrow = "-.->" if edge.conditional else "-->"
        link = arrow if label is None else f'{arrow}|"{label}"|'
        lines.append(f"{ids[edge.source]} {link} {ids[edge.target]}")
    return lines


def _tool_boxes(names: Iterable[str], operations: dict[str, Operation]) -> dict[str, list[str]]:
    """One Mermaid subgraph per tool area by id, holding a node per access level."""
    areas: dict[str, dict[str, list[str]]] = {area: {} for area in _AREAS}
    for name in names:
        op = operations.get(name)
        area = op.fn.__module__.rpartition(".")[2] if op else "other"
        areas.setdefault(area, {}).setdefault(_access(op) if op else "", []).append(name)
    boxes = {}
    for area, levels in areas.items():
        nodes = [
            f'tools_{area}_{level or "all"}["{"<br/>".join(levels[level])}"]'
            + (f":::{level}" if level else "")
            for level in (*_ACCESS, "")
            if level in levels
        ]
        if nodes:
            title = _AREAS.get(area, area.capitalize())
            boxes[f"tools_{area}"] = _subgraph(f"tools_{area}", title, "LR", nodes)
    return boxes


def draw_mermaid(graph: Any) -> str:
    """Draw a compiled graph as a Mermaid flowchart.

    The graph is drawn as LangGraph reports it. The BRAIN tools are listed under it, grouped by
    area and colored by what they can change, and an approval step names the tools it guards.
    """
    drawn = graph.get_graph()
    operations = {op.name: op for op in OPERATIONS}
    tools = dict.fromkeys(
        name
        for node in drawn.nodes.values()
        for name in getattr(node.data, "tools_by_name", None) or ()
    )
    lines = _subgraph("agent", "Agent graph", "LR", _graph_lines(drawn))
    boxes = ["agent"]
    rows: list[str] = []
    areas = _tool_boxes(tools, operations)
    if areas:
        ids = list(areas)
        grid: list[str] = []
        for start in range(0, len(ids), 3):
            row = ids[start : start + 3]
            rows.append(f"tools_row{start // 3}")
            body = [line for box in row for line in areas[box]]
            grid += _subgraph(rows[-1], " ", "LR", body + _in_order(row))
        lines += _subgraph("tools", "Tools", "TB", grid + _in_order(rows))
        lines.append("agent -- tool calls --> tools")
        boxes += ["tools", *ids]
    used = {_access(operations[name]) for name in tools if name in operations}
    if used:
        key = [
            f'legend_{level}["{text}"]:::{level}'
            for level, text in _ACCESS.items()
            if level in used
        ]
        lines += _subgraph("legend", "Legend", "TB", key)
        lines += ['brain["BRAIN API"]:::external', "tools -- HTTPS --> brain", "tools ~~~ legend"]
        boxes.append("legend")
    lines += _STYLES
    lines.append(f"class {','.join(boxes)} box")
    if rows:
        lines.append(f"class {','.join(rows)} row")
    return _FLOWCHART + "".join(f"    {line}\n" for line in lines)
