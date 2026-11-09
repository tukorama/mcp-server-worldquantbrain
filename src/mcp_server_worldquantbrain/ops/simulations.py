from typing import Annotated

from pydantic import Field

from mcp_server_worldquantbrain.client import simulations as api
from mcp_server_worldquantbrain.errors import BrainValidationError
from mcp_server_worldquantbrain.models.simulation import (
    AlphaSpec,
    BatchResult,
    SimulationOptions,
    SimulationResult,
    simulation_payload,
)
from mcp_server_worldquantbrain.ops.registry import Call, Delay, Region, Universe, operation

Wait = Annotated[
    bool, Field(description="True blocks until done or timeout; False returns the id at once.")
]
Timeout = Annotated[
    int | None,
    Field(ge=1, le=3600, description="Seconds to block. Empty uses WQB_SIMULATION_TIMEOUT."),
]
Force = Annotated[bool, Field(description="Run again even if the ledger has this exact alpha.")]


def _timeout(call: Call, requested: int | None) -> float:
    return call.limit_wait(requested or call.settings.simulation_timeout)


@operation("Simulate an alpha")
async def simulate_alpha(
    call: Call,
    expression: Annotated[str, Field(min_length=1, description="FASTEXPR expression.")],
    region: Region = None,
    universe: Universe = None,
    delay: Delay = None,
    decay: Annotated[int | None, Field(ge=0, description="Smoothing window in days.")] = None,
    neutralization: Annotated[
        str | None, Field(description="NONE, MARKET, SECTOR, INDUSTRY or SUBINDUSTRY.")
    ] = None,
    truncation: Annotated[float | None, Field(ge=0, le=1, description="Max weight.")] = None,
    pasteurization: Annotated[str | None, Field(description="ON or OFF.")] = None,
    nan_handling: Annotated[str | None, Field(description="ON or OFF.")] = None,
    unit_handling: Annotated[str | None, Field(description="VERIFY.")] = None,
    test_period: Annotated[str | None, Field(description="ISO duration, e.g. P2Y.")] = None,
    max_trade: Annotated[str | None, Field(description="ON or OFF.")] = None,
    instrument_type: Annotated[str | None, Field(description="EQUITY.")] = None,
    wait: Wait = True,
    timeout_seconds: Timeout = None,
    force: Force = False,
) -> SimulationResult:
    """Backtest one FASTEXPR expression and return its alpha with stats and checks.

    A simulation usually takes 1 to 10 minutes. If it outlasts the timeout the result has status
    PENDING and a simulation_id: call get_simulation_status to resume, never simulate again.
    Identical expression and settings return the earlier result (from_ledger) without spending
    quota. ERROR or FAIL results carry BRAIN's message, usually a syntax or field problem.
    """
    payload = simulation_payload(
        call.settings,
        expression,
        instrument_type=instrument_type,
        region=region,
        universe=universe,
        delay=delay,
        decay=decay,
        neutralization=neutralization,
        truncation=truncation,
        pasteurization=pasteurization,
        nan_handling=nan_handling,
        unit_handling=unit_handling,
        test_period=test_period,
        max_trade=max_trade,
    )
    return await call.runtime.simulations.simulate(
        payload,
        wait=wait,
        timeout=_timeout(call, timeout_seconds),
        force=force,
        progress=call.progress,
    )


@operation("Simulate several alphas")
async def simulate_alphas_batch(
    call: Call,
    alphas: Annotated[list[AlphaSpec], Field(min_length=1, max_length=30)],
    wait: Wait = True,
    timeout_seconds: Timeout = None,
    force: Force = False,
) -> BatchResult:
    """Backtest up to 30 expressions, sent to BRAIN in groups of at most 10.

    All expressions must share one delay; different regions are sent as separate groups.
    Multi-simulation needs a Consultant account. Expressions already in the ledger are returned
    without spending quota. not_started lists anything that could not be sent; resubmit those later.
    """
    payloads = [
        simulation_payload(
            call.settings,
            spec.expression,
            region=spec.region,
            universe=spec.universe,
            delay=spec.delay,
            decay=spec.decay,
            neutralization=spec.neutralization,
            truncation=spec.truncation,
        )
        for spec in alphas
    ]
    delays = {payload["settings"]["delay"] for payload in payloads}
    if len(delays) > 1:
        raise BrainValidationError(
            f"A batch must use one delay, got {sorted(delays)}.",
            hint="Split the expressions by delay.",
        )
    return await call.runtime.simulations.simulate_batch(
        payloads,
        wait=wait,
        timeout=_timeout(call, timeout_seconds),
        force=force,
        progress=call.progress,
    )


@operation("Get simulation status", read_only=True)
async def get_simulation_status(
    call: Call,
    simulation_id: Annotated[str, Field(min_length=1, description="Id from simulate_alpha.")],
    wait_seconds: Annotated[
        int, Field(ge=0, le=3600, description="Seconds to wait for completion; 0 checks once.")
    ] = 0,
) -> SimulationResult:
    """Check or resume a simulation. A finished one includes its alpha; a batch lists children."""
    return await call.runtime.simulations.status(
        simulation_id, wait_seconds=call.limit_wait(wait_seconds), progress=call.progress
    )


@operation("List valid simulation settings", read_only=True)
async def get_simulation_options(call: Call) -> SimulationOptions:
    """List the regions, universes, delays and neutralizations this account may simulate.

    by_context maps parent settings such as 'EQUITY:USA' to the values valid for them. Cached for
    a week.
    """
    return await api.simulation_options(call.client, call.runtime.cache)
