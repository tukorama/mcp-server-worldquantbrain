import json
from typing import Any

import httpx2

from mcp_server_worldquantbrain.client.base import BrainClient, default_terminal, parse_json
from mcp_server_worldquantbrain.client.cache import WEEK, JsonCache
from mcp_server_worldquantbrain.errors import ServerError
from mcp_server_worldquantbrain.models.simulation import OptionField, Quota, SimulationOptions

RUNNING = {"WAITING", "SIMULATING", "RUNNING", "QUEUED", "PENDING"}
SUCCESS = {"COMPLETE", "WARNING"}

RAW_OPTIONS_LIMIT = 20_000


def simulation_terminal(response: httpx2.Response) -> bool:
    body = parse_json(response)
    return (
        isinstance(body, dict) and body.get("status") not in RUNNING and default_terminal(response)
    )


def read_quota(response: httpx2.Response) -> Quota | None:
    values = {
        key.lower().removeprefix("x-ratelimit-"): value
        for key, value in response.headers.items()
        if key.lower().startswith("x-ratelimit-")
    }
    if not values:
        return None
    return Quota(
        limit=values.get("limit"), remaining=values.get("remaining"), reset=values.get("reset")
    )


async def start_simulation(
    client: BrainClient, payload: dict[str, Any] | list[dict[str, Any]]
) -> tuple[str, Quota | None]:
    response = await client.request("POST", "/simulations", json_body=payload)
    location = response.headers.get("location")
    if not location:
        raise ServerError(f"POST /simulations returned {response.status_code} without a Location.")
    return location.rstrip("/").rsplit("/", 1)[-1], read_quota(response)


def _values(choices: Any) -> list[str]:
    values = [item.get("value") if isinstance(item, dict) else item for item in choices]
    return [str(value) for value in values if value is not None]


def parse_options(payload: Any) -> SimulationOptions:
    try:
        children = payload["actions"]["POST"]["settings"]["children"]
        items = children.items()
    except (KeyError, TypeError, AttributeError):
        return SimulationOptions(parsed=False, raw=_trimmed(payload))
    fields: dict[str, OptionField] = {}
    for name, spec in items:
        if not isinstance(spec, dict) or "choices" not in spec:
            continue
        choices = spec["choices"]
        if isinstance(choices, list):
            fields[name] = OptionField(values=_values(choices))
        elif isinstance(choices, dict):
            by_context: dict[str, list[str]] = {}
            _collect_contexts(choices, [], by_context)
            union = sorted({value for values in by_context.values() for value in values})
            if not union:
                return SimulationOptions(parsed=False, raw=_trimmed(payload))
            fields[name] = OptionField(values=union, by_context=by_context)
    if not fields:
        return SimulationOptions(parsed=False, raw=_trimmed(payload))
    return SimulationOptions(parsed=True, fields=fields)


def _collect_contexts(node: Any, path: list[str], found: dict[str, list[str]]) -> None:
    # Dependent settings nest as parameter name, then value: {"instrumentType": {"EQUITY":
    # {"region": {"USA": [...]}}}} becomes the context "EQUITY:USA".
    if isinstance(node, list):
        found[":".join(path)] = _values(node)
    elif isinstance(node, dict):
        for key, values in node.items():
            if isinstance(values, dict):
                for value, child in values.items():
                    _collect_contexts(child, [*path, str(value)], found)
            elif isinstance(values, list):
                found[":".join([*path, str(key)])] = _values(values)


def _trimmed(payload: Any) -> Any:
    text = json.dumps(payload)
    return payload if len(text) <= RAW_OPTIONS_LIMIT else text[:RAW_OPTIONS_LIMIT]


async def simulation_options(client: BrainClient, cache: JsonCache) -> SimulationOptions:
    key = "OPTIONS /simulations"
    payload = cache.get(key, WEEK)
    if payload is None:
        payload = await client.request_json("OPTIONS", "/simulations")
        if payload:
            cache.set(key, payload)
    return parse_options(payload)
