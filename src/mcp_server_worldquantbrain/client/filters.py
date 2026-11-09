from typing import Any

# BRAIN puts the comparison inside the parameter name. `is.sharpe>=1.25` on the wire is the name
# `is.sharpe>` with the value `1.25`, so a plain params list encodes it correctly
# (`is.sharpe%3E=1.25`), including `+` in timestamps.
GTE = ">"
LTE = "<"


def alpha_query(
    *,
    limit: int,
    offset: int,
    order: str | None = None,
    status: str | None = None,
    region: str | None = None,
    universe: str | None = None,
    delay: int | None = None,
    alpha_type: str | None = None,
    min_sharpe: float | None = None,
    min_fitness: float | None = None,
    max_turnover: float | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    name: str | None = None,
    tag: str | None = None,
    color: str | None = None,
    category: str | None = None,
    hidden: bool | None = None,
    favorite: bool | None = None,
    extra_params: dict[str, str] | None = None,
) -> list[tuple[str, Any]]:
    candidates: list[tuple[str, Any]] = [
        ("limit", limit),
        ("offset", offset),
        ("order", order),
        ("status", status),
        ("settings.region", region),
        ("settings.universe", universe),
        ("settings.delay", delay),
        ("type", alpha_type),
        (f"is.sharpe{GTE}", min_sharpe),
        (f"is.fitness{GTE}", min_fitness),
        (f"is.turnover{LTE}", max_turnover),
        (f"dateCreated{GTE}", created_after),
        (f"dateCreated{LTE}", created_before),
        ("name", name),
        ("tag", tag),
        ("color", color),
        ("category", category),
        ("hidden", _flag(hidden)),
        ("favorite", _flag(favorite)),
    ]
    query = [(key, value) for key, value in candidates if value is not None]
    query.extend((extra_params or {}).items())
    return query


def _flag(value: bool | None) -> str | None:
    return None if value is None else str(value).lower()
