from typing import Any, Literal, cast

from pydantic import Field

from mcp_server_worldquantbrain.models.base import CompactModel, Page, as_dict, dict_items


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
        raw = as_dict(data)
        if not raw or (raw.get("sharpe") is None and raw.get("pnl") is None):
            return None
        return cls(
            sharpe=raw.get("sharpe"),
            fitness=raw.get("fitness"),
            turnover=raw.get("turnover"),
            returns=raw.get("returns"),
            drawdown=raw.get("drawdown"),
            margin=raw.get("margin"),
            pnl=raw.get("pnl"),
            book_size=raw.get("bookSize"),
            long_count=raw.get("longCount"),
            short_count=raw.get("shortCount"),
            start_date=raw.get("startDate"),
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
        settings = as_dict(data.get("settings"))
        raw_leg = data.get("regular") or data.get("combo")
        leg: dict[str, Any] = (
            cast("dict[str, Any]", raw_leg) if isinstance(raw_leg, dict) else {"code": raw_leg}
        )
        in_sample = as_dict(data.get("is"))
        checks = [Check.model_validate(check) for check in dict_items(in_sample.get("checks"))]
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
    yearly: list[YearPnl] = Field(default_factory=list[YearPnl])
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
    checks: list[Check] = Field(default_factory=list[Check])
    failed: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    pending: bool = False
    correlation_pending: list[str] = Field(default_factory=list)
    next_step: str | None = None


class CorrelationResult(CompactModel):
    alpha_id: str
    kind: Literal["self", "prod"]
    max_correlation: float | None = None
    min_correlation: float | None = None
    threshold: float
    passes: bool | None = None
    count: int = 0
    top: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])
    pending: bool = False
    next_step: str | None = None


class SubmitResult(CompactModel):
    alpha_id: str
    outcome: Literal["submitted", "refused", "rejected", "pending"]
    message: str | None = None
    checks: list[Check] | None = None
    next_step: str | None = None
