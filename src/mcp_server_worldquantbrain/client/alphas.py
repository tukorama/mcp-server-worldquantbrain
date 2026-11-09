from typing import Any, Literal

from mcp_server_worldquantbrain.client.base import BrainClient
from mcp_server_worldquantbrain.client.paging import page_info, segment
from mcp_server_worldquantbrain.errors import NotFound, PollTimeout, ServerError
from mcp_server_worldquantbrain.models.alpha import (
    AlphaPage,
    AlphaSummary,
    Check,
    CheckResult,
    CorrelationResult,
    DatePnl,
    PnlSummary,
    RecordSet,
    RecordSetNames,
    YearPnl,
)

TOP_CORRELATIONS = 10
_TOP_COLUMNS = ("id", "name", "correlation", "sharpe", "fitness", "region", "universe")
MAX_DAILY_ROWS = 500
_SETTLED_RESULTS = {"PASS", "WARNING", "FAIL", "ERROR"}


def decode_records(payload: Any) -> tuple[list[str], list[dict[str, Any]]]:
    """Turn BRAIN's positional {schema, records} block into named rows."""
    if not isinstance(payload, dict):
        return [], []
    schema = payload.get("schema")
    properties = schema.get("properties") if isinstance(schema, dict) else None
    columns = [str(column.get("name")) for column in properties or []]
    rows = [
        dict(zip(columns, record, strict=False))
        for record in payload.get("records") or []
        if isinstance(record, list | tuple)
    ]
    return columns, rows


def find_checks(payload: Any) -> list[dict[str, Any]]:
    """Collect every check in the payload, since a failure may sit in any block."""
    if not isinstance(payload, dict):
        return []
    found: list[dict[str, Any]] = []
    for key, value in payload.items():
        if key == "checks" and isinstance(value, list):
            found.extend(check for check in value if isinstance(check, dict))
        else:
            found.extend(find_checks(value))
    return found


async def get_alpha(client: BrainClient, alpha_id: str, *, max_seconds: float = 60) -> AlphaSummary:
    response = await client.poll(f"/alphas/{segment(alpha_id)}", max_seconds=max_seconds)
    body = response.json()
    if not isinstance(body, dict):
        raise ServerError(f"GET /alphas/{alpha_id} returned an unexpected body.")
    return AlphaSummary.from_api(body)


async def list_alphas(client: BrainClient, query: list[tuple[str, Any]]) -> AlphaPage:
    body = await client.request_json("GET", "/users/self/alphas", params=query) or {}
    results = body.get("results") or []
    query_map = dict(query)
    next_offset, truncated = page_info(
        body.get("count"), int(query_map["offset"]), len(results), int(query_map["limit"])
    )
    return AlphaPage(
        count=body.get("count"),
        next_offset=next_offset,
        truncated_by_api=truncated,
        items=[AlphaSummary.from_api(item) for item in results],
    )


async def list_recordsets(client: BrainClient, alpha_id: str) -> RecordSetNames:
    body = await client.request_json("GET", f"/alphas/{segment(alpha_id)}/recordsets")
    names: list[str] = []
    entries = body.get("results", body) if isinstance(body, dict) else body
    for entry in entries or []:
        name = entry.get("name") or entry.get("id") if isinstance(entry, dict) else entry
        if name:
            names.append(str(name))
    return RecordSetNames(alpha_id=alpha_id, names=names)


async def _recordset_payload(
    client: BrainClient, alpha_id: str, name: str, max_seconds: float
) -> Any:
    url = f"/alphas/{segment(alpha_id)}/recordsets/{segment(name)}"
    try:
        response = await client.poll(url, max_seconds=max_seconds)
    except NotFound as exc:
        available = await list_recordsets(client, alpha_id)
        exc.hint = f"Available recordsets: {', '.join(available.names) or 'none'}."
        raise
    return response.json()


async def get_recordset(
    client: BrainClient, alpha_id: str, name: str, *, max_rows: int, max_seconds: float = 60
) -> RecordSet:
    columns, rows = decode_records(await _recordset_payload(client, alpha_id, name, max_seconds))
    return RecordSet(
        alpha_id=alpha_id,
        name=name,
        columns=columns,
        rows=rows[-max_rows:],
        total_rows=len(rows),
        truncated=len(rows) > max_rows,
    )


async def get_pnl(
    client: BrainClient,
    alpha_id: str,
    *,
    include_daily: bool,
    last_n: int | None,
    max_seconds: float = 60,
) -> PnlSummary:
    columns, rows = decode_records(await _recordset_payload(client, alpha_id, "pnl", max_seconds))
    date_column = next((c for c in columns if c.lower() == "date"), None)
    pnl_column = next((c for c in columns if "pnl" in c.lower()), None)
    if date_column is None or pnl_column is None:
        return PnlSummary(alpha_id=alpha_id, total_rows=len(rows))
    points = [(str(r[date_column]), r[pnl_column]) for r in rows if r.get(pnl_column) is not None]
    # The pnl recordset is cumulative, so a year's PnL is the change between year-end values.
    year_end: dict[int, float] = {}
    for date, value in points:
        year_end[int(date[:4])] = float(value)
    yearly: list[YearPnl] = []
    previous = 0.0
    for year in sorted(year_end):
        yearly.append(
            YearPnl(year=year, pnl=year_end[year] - previous, cumulative_pnl=year_end[year])
        )
        previous = year_end[year]
    daily = None
    truncated = False
    if include_daily or last_n:
        keep = last_n or MAX_DAILY_ROWS
        daily = [DatePnl(date=d, cumulative_pnl=v) for d, v in points[-keep:]]
        truncated = len(points) > keep
    return PnlSummary(
        alpha_id=alpha_id,
        start_date=points[0][0] if points else None,
        end_date=points[-1][0] if points else None,
        total_pnl=float(points[-1][1]) if points else None,
        total_rows=len(rows),
        yearly=yearly,
        daily=daily,
        truncated=truncated,
    )


async def update_alpha(client: BrainClient, alpha_id: str, body: dict[str, Any]) -> AlphaSummary:
    data = await client.request_json("PATCH", f"/alphas/{segment(alpha_id)}", json_body=body)
    if isinstance(data, dict) and data.get("id"):
        return AlphaSummary.from_api(data)
    return await get_alpha(client, alpha_id)


def summarize_checks(alpha_id: str, payload: Any) -> CheckResult:
    checks = [Check.model_validate(check) for check in find_checks(payload)]
    failed = [c.name or "" for c in checks if c.result in ("FAIL", "ERROR")]
    warnings = [c.name or "" for c in checks if c.result == "WARNING"]
    # An unknown or missing result is not a pass: treating it as one could let a bad alpha submit.
    unsettled = [c for c in checks if c.result not in _SETTLED_RESULTS]
    # BRAIN leaves correlation checks PENDING here even once it has answered, and the correlation
    # endpoints report them separately, so they do not hold the verdict open.
    correlation_pending = [
        c.name or ""
        for c in unsettled
        if c.result == "PENDING" and (c.name or "").endswith("_CORRELATION")
    ]
    pending = not checks or len(unsettled) > len(correlation_pending)
    verdict = "FAIL" if failed else "PENDING" if pending else "WARNING" if warnings else "PASS"
    next_step = None
    if verdict == "PENDING":
        next_step = "Call check_alpha_submission again in a minute."
    elif correlation_pending:
        next_step = "Call get_alpha_self_correlation for the correlation figure."
    return CheckResult(
        alpha_id=alpha_id,
        verdict=verdict,
        checks=checks,
        failed=failed,
        warnings=warnings,
        pending=pending and not failed,
        correlation_pending=correlation_pending,
        next_step=next_step,
    )


async def check_submission(
    client: BrainClient, alpha_id: str, *, wait_seconds: float
) -> CheckResult:
    try:
        response = await client.poll(f"/alphas/{segment(alpha_id)}/check", max_seconds=wait_seconds)
    except PollTimeout:
        return CheckResult(
            alpha_id=alpha_id,
            verdict="PENDING",
            pending=True,
            next_step="BRAIN is still computing the checks. Call check_alpha_submission again.",
        )
    return summarize_checks(alpha_id, response.json())


async def get_correlation(
    client: BrainClient,
    alpha_id: str,
    kind: Literal["self", "prod"],
    *,
    wait_seconds: float,
    threshold: float,
) -> CorrelationResult:
    try:
        response = await client.poll(
            f"/alphas/{segment(alpha_id)}/correlations/{kind}",
            max_seconds=wait_seconds,
        )
    except PollTimeout:
        return CorrelationResult(
            alpha_id=alpha_id,
            kind=kind,
            threshold=threshold,
            pending=True,
            next_step=f"BRAIN is still computing it. Call get_alpha_{kind}_correlation again.",
        )
    payload = response.json()
    _, rows = decode_records(payload)
    highest = payload.get("max") if isinstance(payload, dict) else None
    lowest = payload.get("min") if isinstance(payload, dict) else None
    if kind == "self":
        ranked = sorted(rows, key=lambda row: row.get("correlation") or 0, reverse=True)
        top = [{k: r[k] for k in _TOP_COLUMNS if r.get(k) is not None} for r in ranked]
        if highest is None and ranked:
            highest = ranked[0].get("correlation")
    else:
        top = [row for row in rows if row.get("alphas")][-TOP_CORRELATIONS:]
        if highest is None and top:
            highest = top[-1].get("max")
    passes: bool | None = None
    if highest is not None:
        passes = highest < threshold
    elif isinstance(payload, dict) and payload.get("records") == []:
        passes = True  # BRAIN answered with an empty list: nothing to correlate against.
    return CorrelationResult(
        alpha_id=alpha_id,
        kind=kind,
        max_correlation=highest,
        min_correlation=lowest,
        threshold=threshold,
        passes=passes,
        count=len(rows),
        top=top[:TOP_CORRELATIONS],
    )
