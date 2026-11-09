"""Use BRAIN from LangChain or LangGraph directly, without an MCP subprocess.

Needs the extra: `uv add "mcp-server-worldquantbrain[langgraph]"`.
"""

import asyncio
import inspect
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.tools import BaseTool, StructuredTool, ToolException
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import create_model

from mcp_server_worldquantbrain import knowledge
from mcp_server_worldquantbrain.config import Settings
from mcp_server_worldquantbrain.errors import BrainError
from mcp_server_worldquantbrain.ops import Call, Operation, Runtime, enabled_operations
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
