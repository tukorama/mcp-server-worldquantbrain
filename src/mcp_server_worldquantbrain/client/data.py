import json
from typing import Any

from mcp_server_worldquantbrain.client.base import BrainClient
from mcp_server_worldquantbrain.client.cache import DAY, WEEK, JsonCache
from mcp_server_worldquantbrain.client.paging import page_info, segment
from mcp_server_worldquantbrain.models.data import (
    DataCategory,
    DataCategoryList,
    DataField,
    DataFieldPage,
    Dataset,
    DatasetPage,
    Operator,
    OperatorList,
)


async def _cached(client: BrainClient, cache: JsonCache, url: str, ttl: int, params=None) -> Any:
    key = f"{url}?{json.dumps(params, sort_keys=True)}"
    cached = cache.get(key, ttl)
    if cached is not None:
        return cached
    data = await client.request_json("GET", url, params=params)
    if data:
        cache.set(key, data)
    return data


async def list_datasets(
    client: BrainClient, cache: JsonCache, params: dict[str, Any]
) -> DatasetPage:
    body = await _cached(client, cache, "/data-sets", DAY, params) or {}
    results = body.get("results") or []
    next_offset, truncated = page_info(
        body.get("count"), params["offset"], len(results), params["limit"]
    )
    return DatasetPage(
        count=body.get("count"),
        next_offset=next_offset,
        truncated_by_api=truncated,
        items=[Dataset.from_api(item) for item in results],
    )


async def get_dataset(client: BrainClient, dataset_id: str) -> Dataset:
    body = await client.request_json("GET", f"/data-sets/{segment(dataset_id)}")
    return Dataset.from_api(body or {"id": dataset_id}, full=True)


async def list_datafields(client: BrainClient, params: dict[str, Any]) -> DataFieldPage:
    body = await client.request_json("GET", "/data-fields", params=params) or {}
    results = body.get("results") or []
    next_offset, truncated = page_info(
        body.get("count"), params["offset"], len(results), params["limit"]
    )
    return DataFieldPage(
        count=body.get("count"),
        next_offset=next_offset,
        truncated_by_api=truncated,
        items=[DataField.from_api(item) for item in results],
    )


async def get_datafield(client: BrainClient, field_id: str) -> DataField:
    body = await client.request_json("GET", f"/data-fields/{segment(field_id)}")
    return DataField.from_api(body or {"id": field_id}, full=True)


async def list_categories(client: BrainClient, cache: JsonCache) -> DataCategoryList:
    body = await _cached(client, cache, "/data-categories", DAY) or []
    return DataCategoryList(items=[DataCategory.from_api(item) for item in body])


async def list_operators(
    client: BrainClient,
    cache: JsonCache,
    *,
    category: str | None = None,
    search: str | None = None,
    verbose: bool = False,
) -> OperatorList:
    body = await _cached(client, cache, "/operators", WEEK) or []
    needle = search.lower() if search else None
    items: list[Operator] = []
    for raw in body:
        if category and str(raw.get("category", "")).lower() != category.lower():
            continue
        haystack = f"{raw.get('name', '')} {raw.get('definition', '')} {raw.get('description', '')}"
        if needle and needle not in haystack.lower():
            continue
        items.append(
            Operator(
                name=str(raw.get("name")),
                category=raw.get("category"),
                definition=raw.get("definition"),
                scope=raw.get("scope") if verbose else None,
                level=raw.get("level") if verbose else None,
                description=raw.get("description") if verbose else None,
                documentation=raw.get("documentation") if verbose else None,
            )
        )
    return OperatorList(count=len(items), items=items)
