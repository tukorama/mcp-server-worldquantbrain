from typing import Annotated, Any

from pydantic import Field

from mcp_server_worldquantbrain.client import data as api
from mcp_server_worldquantbrain.models.data import (
    DataCategoryList,
    DataField,
    DataFieldPage,
    Dataset,
    DatasetPage,
    OperatorList,
)
from mcp_server_worldquantbrain.models.simulation import resolve_universe
from mcp_server_worldquantbrain.ops.registry import Call, Delay, Region, Universe, operation

InstrumentType = Annotated[str | None, Field(description="EQUITY unless configured otherwise.")]
Limit = Annotated[int, Field(ge=1, le=50)]
Offset = Annotated[int, Field(ge=0, lt=10000)]


def _market(
    call: Call,
    instrument_type: str | None,
    region: str | None,
    delay: int | None,
    universe: str | None,
) -> dict[str, Any]:
    settings = call.settings
    return {
        "instrumentType": instrument_type or settings.default_instrument_type,
        "region": region or settings.default_region,
        "delay": settings.default_delay if delay is None else delay,
        "universe": resolve_universe(settings, region, universe),
    }


def _clean(params: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in params.items() if value is not None}


@operation("List datasets", read_only=True)
async def list_datasets(
    call: Call,
    region: Region = None,
    delay: Delay = None,
    universe: Universe = None,
    instrument_type: InstrumentType = None,
    category: Annotated[
        str | None, Field(description="Category id from list_data_categories.")
    ] = None,
    search: str | None = None,
    theme: bool | None = None,
    order: Annotated[str | None, Field(description="e.g. -coverage or -valueScore.")] = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> DatasetPage:
    """List datasets available for a market. Cached for a day."""
    params = _market(call, instrument_type, region, delay, universe)
    params |= {
        "category": category,
        "search": search,
        "theme": None if theme is None else str(theme).lower(),
        "order": order,
        "limit": limit,
        "offset": offset,
    }
    return await api.list_datasets(call.client, call.runtime.cache, _clean(params))


@operation("Get a dataset", read_only=True)
async def get_dataset(call: Call, dataset_id: Annotated[str, Field(min_length=1)]) -> Dataset:
    """Return one dataset with its full description, for example dataset_id 'pv1'."""
    return await api.get_dataset(call.client, dataset_id)


@operation("List data fields", read_only=True)
async def list_datafields(
    call: Call,
    region: Region = None,
    delay: Delay = None,
    universe: Universe = None,
    dataset_id: Annotated[str | None, Field(description="Restrict to one dataset.")] = None,
    search: str | None = None,
    type: Annotated[str | None, Field(description="MATRIX, VECTOR, GROUP or UNIVERSE.")] = None,
    category: str | None = None,
    instrument_type: InstrumentType = None,
    order: Annotated[str | None, Field(description="e.g. -coverage or -alphaCount.")] = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> DataFieldPage:
    """Find the data fields usable in expressions.

    BRAIN stops paging at 10,000 rows, so when truncated_by_api is true narrow with dataset_id or
    search.
    """
    params = _market(call, instrument_type, region, delay, universe)
    params |= {
        "dataset.id": dataset_id,
        "search": search,
        "type": type,
        "category": category,
        "order": order,
        "limit": limit,
        "offset": offset,
    }
    return await api.list_datafields(call.client, _clean(params))


@operation("Get a data field", read_only=True)
async def get_datafield(call: Call, field_id: Annotated[str, Field(min_length=1)]) -> DataField:
    """Return one data field with its full description, for example field_id 'close'."""
    return await api.get_datafield(call.client, field_id)


@operation("List data categories", read_only=True)
async def list_data_categories(call: Call) -> DataCategoryList:
    """Return the data category tree. Cached for a day."""
    return await api.list_categories(call.client, call.runtime.cache)


@operation("List operators", read_only=True)
async def list_operators(
    call: Call,
    category: Annotated[str | None, Field(description="e.g. Arithmetic or Time Series.")] = None,
    search: Annotated[str | None, Field(description="Substring of name or definition.")] = None,
    verbose: Annotated[bool, Field(description="Include full descriptions.")] = False,
) -> OperatorList:
    """List the FASTEXPR operators this account can use. Cached for a week."""
    return await api.list_operators(
        call.client, call.runtime.cache, category=category, search=search, verbose=verbose
    )
