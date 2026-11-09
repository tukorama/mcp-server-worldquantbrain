from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from mcp_server_worldquantbrain.config import Settings
from mcp_server_worldquantbrain.errors import BrainValidationError
from mcp_server_worldquantbrain.models.alpha import AlphaSummary
from mcp_server_worldquantbrain.models.base import CompactModel

MAX_TRADE_REQUIRED_REGIONS = {"ASI", "JPN", "HKG", "KOR", "TWN"}


class AlphaSpec(BaseModel):
    """One expression in a batch. Unset settings fall back to the server defaults."""

    model_config = ConfigDict(extra="forbid")

    expression: str = Field(min_length=1, description="FASTEXPR expression.")
    region: str | None = None
    universe: str | None = None
    delay: int | None = None
    decay: int | None = None
    neutralization: str | None = None
    truncation: float | None = None


def resolve_universe(settings: Settings, region: str | None, universe: str | None) -> str:
    if universe:
        return universe
    if not region or region == settings.default_region:
        return settings.default_universe
    raise BrainValidationError(
        f"Universe is required for region {region}.",
        hint="Call get_simulation_options to see the universes valid for that region.",
    )


def simulation_payload(
    settings: Settings,
    expression: str,
    *,
    instrument_type: str | None = None,
    region: str | None = None,
    universe: str | None = None,
    delay: int | None = None,
    decay: int | None = None,
    neutralization: str | None = None,
    truncation: float | None = None,
    pasteurization: str | None = None,
    nan_handling: str | None = None,
    unit_handling: str | None = None,
    test_period: str | None = None,
    max_trade: str | None = None,
) -> dict[str, Any]:
    resolved_region = region or settings.default_region
    resolved_max_trade = max_trade or (
        "ON" if resolved_region in MAX_TRADE_REQUIRED_REGIONS else settings.default_max_trade
    )
    api_settings: dict[str, Any] = {
        "instrumentType": instrument_type or settings.default_instrument_type,
        "region": resolved_region,
        "universe": resolve_universe(settings, region, universe),
        "delay": settings.default_delay if delay is None else delay,
        "decay": settings.default_decay if decay is None else decay,
        "neutralization": neutralization or settings.default_neutralization,
        "truncation": settings.default_truncation if truncation is None else truncation,
        "pasteurization": pasteurization or settings.default_pasteurization,
        "unitHandling": unit_handling or settings.default_unit_handling,
        "nanHandling": nan_handling or settings.default_nan_handling,
        "maxTrade": resolved_max_trade,
        "language": "FASTEXPR",
        "visualization": False,
    }
    period = settings.default_test_period if test_period is None else test_period
    if period:
        api_settings["testPeriod"] = period
    return {"type": "REGULAR", "settings": api_settings, "regular": expression}


class Quota(CompactModel):
    limit: str | None = None
    remaining: str | None = None
    reset: str | None = None


class SimulationResult(CompactModel):
    simulation_id: str
    status: str
    alpha_id: str | None = None
    message: str | None = None
    progress: float | None = None
    alpha: AlphaSummary | None = None
    children: list["SimulationResult"] | None = None
    elapsed_seconds: float | None = None
    quota: Quota | None = None
    from_ledger: bool = False
    next_step: str | None = None


class BatchResult(CompactModel):
    status: str
    batch_ids: list[str] = []
    results: list[SimulationResult] = []
    not_started: list[str] = []
    message: str | None = None
    next_step: str | None = None


class OptionField(CompactModel):
    values: list[str] = []
    by_context: dict[str, list[str]] | None = None


class SimulationOptions(CompactModel):
    parsed: bool
    fields: dict[str, OptionField] = {}
    raw: Any | None = None
