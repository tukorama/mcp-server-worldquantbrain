import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, TypeVar

import httpx2
from pydantic import Field

from mcp_server_worldquantbrain.client.base import BrainClient
from mcp_server_worldquantbrain.client.cache import JsonCache
from mcp_server_worldquantbrain.client.ledger import Ledger
from mcp_server_worldquantbrain.config import Settings
from mcp_server_worldquantbrain.errors import ConfigError
from mcp_server_worldquantbrain.sim_manager import SimulationManager

Report = Callable[[float, str], Awaitable[None]]
F = TypeVar("F", bound=Callable[..., Awaitable[Any]])

AlphaId = Annotated[str, Field(min_length=1, description="BRAIN alpha id, for example 'qMPjAxnO'.")]
Region = Annotated[
    str | None, Field(description="Market region, e.g. USA. Empty uses the default.")
]
Universe = Annotated[
    str | None, Field(description="Universe, e.g. TOP3000. Empty uses the default.")
]
Delay = Annotated[int | None, Field(ge=0, le=1, description="Data delay, 0 or 1.")]


async def _no_report(_fraction: float, _message: str) -> None:
    return None


def _account_directory(settings: Settings) -> Path:
    # Catalogues follow the account's tier, and simulation ids only resolve for their owner.
    try:
        credentials = settings.credentials()
    except ConfigError:
        credentials = None
    if credentials is None:
        return settings.resolved_cache_dir
    account = hashlib.sha256(credentials.email.strip().lower().encode()).hexdigest()[:12]
    return settings.resolved_cache_dir / account


@dataclass
class Runtime:
    settings: Settings
    client: BrainClient
    cache: JsonCache
    ledger: Ledger
    simulations: SimulationManager

    @classmethod
    def create(
        cls, settings: Settings, *, transport: httpx2.AsyncBaseTransport | None = None
    ) -> "Runtime":
        client = BrainClient(settings, transport=transport)
        directory = _account_directory(settings)
        ledger = Ledger(directory / "ledger.sqlite3", enabled=settings.ledger)
        return cls(
            settings=settings,
            client=client,
            cache=JsonCache(directory / "catalog", settings.cache_ttl),
            ledger=ledger,
            simulations=SimulationManager(client, settings, ledger),
        )

    async def aclose(self) -> None:
        await self.client.aclose()


@dataclass
class Call:
    """What an operation sees of the current invocation."""

    runtime: Runtime
    report: Report = _no_report
    wait_cap: float | None = None
    started: float = field(default_factory=time.monotonic)
    _last_fraction: float = 0.0

    @property
    def settings(self) -> Settings:
        return self.runtime.settings

    @property
    def client(self) -> BrainClient:
        return self.runtime.client

    def limit_wait(self, seconds: float) -> float:
        """Shrink a wait to what is left of this call's time allowance."""
        if self.wait_cap is None:
            return seconds
        return min(seconds, max(self.wait_cap - (time.monotonic() - self.started), 0.0))

    async def progress(self, fraction: float, message: str) -> None:
        # MCP requires strictly increasing progress values, so stalls creep toward 1 without
        # ever reaching it.
        last = self._last_fraction
        value = fraction if last < fraction < 1 else last + (1 - last) * 0.01
        self._last_fraction = value
        await self.report(value, message)


@dataclass(frozen=True)
class Operation:
    fn: Callable[..., Awaitable[Any]]
    name: str
    title: str
    read_only: bool
    idempotent: bool
    destructive: bool
    requires_submit_flag: bool

    @property
    def description(self) -> str:
        return (self.fn.__doc__ or "").strip()


OPERATIONS: list[Operation] = []


def operation(
    title: str,
    *,
    read_only: bool = False,
    idempotent: bool = False,
    destructive: bool = False,
    requires_submit_flag: bool = False,
) -> Callable[[F], F]:
    def register(fn: F) -> F:
        OPERATIONS.append(
            Operation(
                fn=fn,
                name=fn.__name__,
                title=title,
                read_only=read_only,
                idempotent=idempotent or read_only,
                destructive=destructive,
                requires_submit_flag=requires_submit_flag,
            )
        )
        return fn

    return register


def enabled_operations(settings: Settings) -> list[Operation]:
    return [op for op in OPERATIONS if settings.allow_submit or not op.requires_submit_flag]
