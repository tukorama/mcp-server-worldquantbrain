from typing import Any, Literal

from pydantic import Field

from mcp_server_worldquantbrain.models.base import CompactModel, Page


class Check(CompactModel):
    name: str | None = None
    result: str | None = None
    limit: Any = None
    value: Any = None
    message: str | None = None


class AlphaStats(CompactModel):
    sharpe: float | None = None
    fitness: float | None = None
    turnover: float | None = None
    returns: float | None = None
    drawdown: float | None = None
    margin: float | None = None
    pnl: float | None = None
    book_size: float | None = None
    long_count: int | None = None
    short_count: int | None = None
    start_date: str | None = None

    @classmethod
    def from_api(cls, data: Any) -> "AlphaStats | None":
        if not isinstance(data, dict) or (data.get("sharpe") is None and data.get("pnl") is None):
            return None
        return cls(
            sharpe=data.get("sharpe"),
            fitness=data.get("fitness"),
            turnover=data.get("turnover"),
            returns=data.get("returns"),
            drawdown=data.get("drawdown"),
            margin=data.get("margin"),
            pnl=data.get("pnl"),
            book_size=data.get("bookSize"),
            long_count=data.get("longCount"),
            short_count=data.get("shortCount"),
            start_date=data.get("startDate"),
        )


class AlphaSummary(CompactModel):
    id: str
    type: str | None = None
    name: str | None = None
    status: str | None = None
    stage: str | None = None
    grade: str | None = None
    date_created: str | None = None
    date_submitted: str | None = None
    instrument_type: str | None = None
    region: str | None = None
    universe: str | None = None
    delay: int | None = None
    decay: int | None = None
    neutralization: str | None = None
    truncation: float | None = None
    expression: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    color: str | None = None
    category: str | None = None
    favorite: bool | None = None
    hidden: bool | None = None
    in_sample: AlphaStats | None = None
    out_of_sample: AlphaStats | None = None
    checks: list[Check] | None = None
    failed_checks: list[str] | None = None
    warning_checks: list[str] | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "AlphaSummary":
        settings = data.get("settings") or {}
        leg = data.get("regular") or data.get("combo") or {}
        if not isinstance(leg, dict):
            leg = {"code": leg}
        raw_in_sample = data.get("is")
        in_sample: dict[str, Any] = raw_in_sample if isinstance(raw_in_sample, dict) else {}
        checks = [
            Check.model_validate(check)
            for check in in_sample.get("checks") or []
            if isinstance(check, dict)
        ]
        return cls(
            id=str(data.get("id")),
            type=data.get("type"),
            name=data.get("name"),
            status=data.get("status"),
            stage=data.get("stage"),
            grade=data.get("grade"),
            date_created=data.get("dateCreated"),
            date_submitted=data.get("dateSubmitted"),
            instrument_type=settings.get("instrumentType"),
            region=settings.get("region"),
            universe=settings.get("universe"),
            delay=settings.get("delay"),
            decay=settings.get("decay"),
            neutralization=settings.get("neutralization"),
            truncation=settings.get("truncation"),
            expression=leg.get("code"),
            description=leg.get("description"),
            tags=data.get("tags") or None,
            color=data.get("color"),
            category=data.get("category"),
            favorite=data.get("favorite"),
            hidden=data.get("hidden"),
            in_sample=AlphaStats.from_api(in_sample),
            out_of_sample=AlphaStats.from_api(data.get("os")),
            checks=checks or None,
            failed_checks=[c.name or "" for c in checks if c.result in ("FAIL", "ERROR")] or None,
            warning_checks=[c.name or "" for c in checks if c.result == "WARNING"] or None,
        )


class AlphaPage(Page[AlphaSummary]):
    pass


class YearPnl(CompactModel):
    year: int
    pnl: float = Field(description="PnL earned during the year.")
    cumulative_pnl: float = Field(description="Cumulative PnL at the last record of the year.")


class DatePnl(CompactModel):
    date: str
    cumulative_pnl: float | None = None


class PnlSummary(CompactModel):
    alpha_id: str
    start_date: str | None = None
    end_date: str | None = None
    total_pnl: float | None = None
    total_rows: int = 0
    yearly: list[YearPnl] = []
    daily: list[DatePnl] | None = None
    truncated: bool = False


class RecordSetNames(CompactModel):
    alpha_id: str
    names: list[str]


class RecordSet(CompactModel):
    alpha_id: str
    name: str
    columns: list[str]
    rows: list[dict[str, Any]]
    total_rows: int
    truncated: bool = False


class CheckResult(CompactModel):
    alpha_id: str
    verdict: Literal["PASS", "WARNING", "FAIL", "PENDING"]
    checks: list[Check] = []
    failed: list[str] = []
    warnings: list[str] = []
    pending: bool = False
    correlation_pending: list[str] = []
    next_step: str | None = None


class CorrelationResult(CompactModel):
    alpha_id: str
    kind: Literal["self", "prod"]
    max_correlation: float | None = None
    min_correlation: float | None = None
    threshold: float
    passes: bool | None = None
    count: int = 0
    top: list[dict[str, Any]] = []
    pending: bool = False
    next_step: str | None = None


class SubmitResult(CompactModel):
    alpha_id: str
    outcome: Literal["submitted", "refused", "rejected", "pending"]
    message: str | None = None
    checks: list[Check] | None = None
    next_step: str | None = None
