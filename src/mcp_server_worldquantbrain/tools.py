import inspect
import logging
from typing import Annotated, Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver import (
    AcceptedElicitation,
    Context,
    Elicit,
    ElicitationResult,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from mcp_server_worldquantbrain.config import Settings
from mcp_server_worldquantbrain.errors import BrainError
from mcp_server_worldquantbrain.models.alpha import SubmitResult
from mcp_server_worldquantbrain.ops import Call, Operation, Runtime, enabled_operations
from mcp_server_worldquantbrain.ops.registry import AlphaId
from mcp_server_worldquantbrain.redaction import scrub

logger = logging.getLogger(__name__)

# Claude Desktop cancels stdio tool calls after about a minute, so long waits there must end in
# PENDING with a resumable id instead of being killed mid-call.
SHORT_CALL_CLIENTS = ("desktop", "claude-ai")
SHORT_CALL_SECONDS = 45.0


def _wait_cap(ctx: Context) -> float | None:
    params = ctx.session.client_params
    if params is None:
        return None
    name = params.client_info.name.lower()
    if any(marker in name for marker in SHORT_CALL_CLIENTS):
        logger.info("Client %s has short tool timeouts, capping waits", name)
        return SHORT_CALL_SECONDS
    return None


def _make_call(ctx: Context) -> Call:
    async def report(fraction: float, message: str) -> None:
        await ctx.report_progress(fraction, 1.0, message)

    runtime = cast(Runtime, ctx.request_context.lifespan_context)
    return Call(runtime=runtime, report=report, wait_cap=_wait_cap(ctx))


async def _invoke(op: Operation, ctx: Context, kwargs: dict[str, Any]) -> Any:
    try:
        return await op.fn(_make_call(ctx), **kwargs)
    except BrainError as exc:
        raise ToolError(scrub(exc.for_agent())) from exc
    except Exception as exc:
        logger.exception("Unexpected failure in %s", op.name)
        raise ToolError(scrub(f"Internal error: {type(exc).__name__}: {exc}")) from exc


def _tool_function(op: Operation) -> Any:
    signature = inspect.signature(op.fn, eval_str=True)
    params = [
        param.replace(kind=inspect.Parameter.KEYWORD_ONLY)
        for param in list(signature.parameters.values())[1:]
    ]
    context = inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context)

    async def tool(ctx: Context, **kwargs: Any) -> Any:
        return await _invoke(op, ctx, kwargs)

    tool.__name__ = op.name
    tool.__doc__ = op.description
    tool.__signature__ = signature.replace(parameters=[*params, context])  # type: ignore[attr-defined]
    return tool


class SubmitConfirmation(BaseModel):
    confirm: bool = Field(default=False, description="Submit this alpha to BRAIN now?")


async def _ask_before_submit(alpha_id: str, ctx: Context) -> Any:
    capabilities = ctx.client_capabilities
    # On a resumed round the SDK must see the question again to apply the recorded answer.
    resuming = ctx.input_responses is not None or ctx.request_state is not None
    if not resuming and (capabilities is None or capabilities.elicitation is None):
        return SubmitConfirmation(confirm=True)
    return Elicit(
        f"Submit alpha {alpha_id} to WorldQuant BRAIN? This cannot be undone.", SubmitConfirmation
    )


async def _submit_with_confirmation(
    alpha_id: AlphaId,
    confirmation: Annotated[ElicitationResult[SubmitConfirmation], Resolve(_ask_before_submit)],
    ctx: Context,
) -> SubmitResult:
    if not isinstance(confirmation, AcceptedElicitation) or not confirmation.data.confirm:
        return SubmitResult(alpha_id=alpha_id, outcome="refused", message="The user declined.")
    settings = cast(Runtime, ctx.request_context.lifespan_context).settings
    operation = next(op for op in enabled_operations(settings) if op.name == "submit_alpha")
    return await _invoke(operation, ctx, {"alpha_id": alpha_id})


def register_tools(server: MCPServer, settings: Settings) -> None:
    for op in enabled_operations(settings):
        function = _tool_function(op)
        if op.name == "submit_alpha" and settings.submit_confirm == "elicit":
            function = _submit_with_confirmation
        server.add_tool(
            function,
            name=op.name,
            title=op.title,
            description=op.description,
            annotations=ToolAnnotations(
                read_only_hint=op.read_only,
                destructive_hint=op.destructive,
                idempotent_hint=op.idempotent,
                open_world_hint=True,
            ),
        )
