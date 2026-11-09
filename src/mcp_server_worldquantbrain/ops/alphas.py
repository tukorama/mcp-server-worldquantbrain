from typing import Annotated, Any

from pydantic import Field

from mcp_server_worldquantbrain.client import alphas as api
from mcp_server_worldquantbrain.client.filters import alpha_query
from mcp_server_worldquantbrain.errors import BrainValidationError
from mcp_server_worldquantbrain.models.alpha import (
    AlphaPage,
    AlphaSummary,
    CheckResult,
    CorrelationResult,
    PnlSummary,
    RecordSet,
    RecordSetNames,
)
from mcp_server_worldquantbrain.ops.registry import (
    AlphaId,
    Call,
    Delay,
    Region,
    Universe,
    operation,
)

CORRELATION_LIMIT = 0.7


@operation("Get an alpha", read_only=True)
async def get_alpha(call: Call, alpha_id: AlphaId) -> AlphaSummary:
    """Return an alpha's expression, settings, in-sample stats and submission checks."""
    return await api.get_alpha(call.client, alpha_id, max_seconds=call.limit_wait(60))


@operation("List my alphas", read_only=True)
async def list_alphas(
    call: Call,
    status: Annotated[
        str | None, Field(description="UNSUBMITTED, ACTIVE or DECOMMISSIONED.")
    ] = None,
    order: Annotated[
        str | None, Field(description="Sort key, e.g. -dateCreated or -is.sharpe.")
    ] = "-dateCreated",
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
    offset: Annotated[int, Field(ge=0, lt=10000)] = 0,
    region: Region = None,
    universe: Universe = None,
    delay: Delay = None,
    alpha_type: Annotated[str | None, Field(description="REGULAR or SUPER.")] = None,
    min_sharpe: float | None = None,
    min_fitness: float | None = None,
    max_turnover: float | None = None,
    created_after: Annotated[str | None, Field(description="ISO 8601 timestamp.")] = None,
    created_before: Annotated[str | None, Field(description="ISO 8601 timestamp.")] = None,
    name: Annotated[str | None, Field(description="Exact alpha name.")] = None,
    tag: str | None = None,
    color: str | None = None,
    category: str | None = None,
    hidden: bool | None = None,
    favorite: bool | None = None,
    extra_params: Annotated[
        dict[str, str] | None, Field(description="Raw query parameters passed through to BRAIN.")
    ] = None,
) -> AlphaPage:
    """Search the user's alphas. Use next_offset to page; BRAIN stops at offset 10,000."""
    query = alpha_query(
        limit=limit,
        offset=offset,
        order=order,
        status=status,
        region=region,
        universe=universe,
        delay=delay,
        alpha_type=alpha_type,
        min_sharpe=min_sharpe,
        min_fitness=min_fitness,
        max_turnover=max_turnover,
        created_after=created_after,
        created_before=created_before,
        name=name,
        tag=tag,
        color=color,
        category=category,
        hidden=hidden,
        favorite=favorite,
        extra_params=extra_params,
    )
    return await api.list_alphas(call.client, query)


@operation("Get alpha PnL", read_only=True)
async def get_alpha_pnl(
    call: Call,
    alpha_id: AlphaId,
    include_daily: Annotated[bool, Field(description="Add daily rows, at most 500.")] = False,
    last_n: Annotated[int | None, Field(ge=1, le=500, description="Only the last N days.")] = None,
) -> PnlSummary:
    """Summarize an alpha's PnL by year, with optional recent daily cumulative values."""
    return await api.get_pnl(
        call.client,
        alpha_id,
        include_daily=include_daily,
        last_n=last_n,
        max_seconds=call.limit_wait(60),
    )


@operation("List alpha recordsets", read_only=True)
async def list_alpha_recordsets(call: Call, alpha_id: AlphaId) -> RecordSetNames:
    """List the recordset names available for an alpha, for use with get_alpha_recordset."""
    return await api.list_recordsets(call.client, alpha_id)


@operation("Get an alpha recordset", read_only=True)
async def get_alpha_recordset(
    call: Call,
    alpha_id: AlphaId,
    name: Annotated[str, Field(min_length=1, description="Recordset, e.g. yearly-stats.")],
    max_rows: Annotated[int, Field(ge=1, le=500, description="Keep the last N rows.")] = 200,
) -> RecordSet:
    """Return one recordset as named rows. Prefer get_alpha_pnl for PnL."""
    return await api.get_recordset(
        call.client, alpha_id, name, max_rows=max_rows, max_seconds=call.limit_wait(60)
    )


@operation("Update alpha properties", idempotent=True)
async def update_alpha(
    call: Call,
    alpha_id: AlphaId,
    name: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    color: Annotated[str | None, Field(description="RED, YELLOW, GREEN, BLUE or PURPLE.")] = None,
    category: str | None = None,
    favorite: bool | None = None,
    hidden: bool | None = None,
) -> AlphaSummary:
    """Change an alpha's name, description, tags, color, category, favorite or hidden flag."""
    body: dict[str, Any] = {
        key: value
        for key, value in {
            "name": name,
            "tags": tags,
            "color": color,
            "category": category,
            "favorite": favorite,
            "hidden": hidden,
        }.items()
        if value is not None
    }
    if description is not None:
        body["regular"] = {"description": description}
    if not body:
        raise BrainValidationError("Nothing to update.", hint="Pass at least one property.")
    return await api.update_alpha(call.client, alpha_id, body)


@operation("Run submission checks", read_only=True)
async def check_alpha_submission(
    call: Call,
    alpha_id: AlphaId,
    wait_seconds: Annotated[int, Field(ge=0, le=900)] = 120,
) -> CheckResult:
    """Run BRAIN's pre-submission checks. verdict is PASS, WARNING, FAIL or PENDING."""
    return await api.check_submission(
        call.client, alpha_id, wait_seconds=call.limit_wait(wait_seconds)
    )


@operation("Get self-correlation", read_only=True)
async def get_alpha_self_correlation(
    call: Call,
    alpha_id: AlphaId,
    wait_seconds: Annotated[int, Field(ge=0, le=900)] = 300,
) -> CorrelationResult:
    """Return the alpha's highest correlation with the user's submitted alphas (limit 0.7)."""
    return await api.get_correlation(
        call.client,
        alpha_id,
        "self",
        wait_seconds=call.limit_wait(wait_seconds),
        threshold=CORRELATION_LIMIT,
    )


@operation("Get production correlation", read_only=True)
async def get_alpha_prod_correlation(
    call: Call,
    alpha_id: AlphaId,
    wait_seconds: Annotated[int, Field(ge=0, le=900)] = 300,
) -> CorrelationResult:
    """Return the alpha's correlation with the production pool. Some account tiers get a 403."""
    return await api.get_correlation(
        call.client,
        alpha_id,
        "prod",
        wait_seconds=call.limit_wait(wait_seconds),
        threshold=CORRELATION_LIMIT,
    )
