from typing import Any

from mcp_server_worldquantbrain.models.base import CompactModel, Page

DESCRIPTION_LIMIT = 300


def _named(value: Any) -> str | None:
    return value.get("name") or value.get("id") if isinstance(value, dict) else value


def _short(text: Any, full: bool) -> str | None:
    if not isinstance(text, str) or full or len(text) <= DESCRIPTION_LIMIT:
        return text if isinstance(text, str) else None
    return text[:DESCRIPTION_LIMIT].rstrip() + "..."


class Dataset(CompactModel):
    id: str
    name: str | None = None
    description: str | None = None
    category: str | None = None
    subcategory: str | None = None
    region: str | None = None
    delay: int | None = None
    universe: str | None = None
    coverage: float | None = None
    value_score: float | None = None
    field_count: int | None = None
    alpha_count: int | None = None
    user_count: int | None = None
    pyramid_multiplier: float | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any], *, full: bool = False) -> "Dataset":
        return cls(
            id=str(data.get("id")),
            name=data.get("name"),
            description=_short(data.get("description"), full),
            category=_named(data.get("category")),
            subcategory=_named(data.get("subcategory")),
            region=data.get("region"),
            delay=data.get("delay"),
            universe=data.get("universe"),
            coverage=data.get("coverage"),
            value_score=data.get("valueScore"),
            field_count=data.get("fieldCount", data.get("valueFieldCount")),
            alpha_count=data.get("alphaCount"),
            user_count=data.get("userCount"),
            pyramid_multiplier=data.get("pyramidMultiplier"),
        )


class DataField(CompactModel):
    id: str
    description: str | None = None
    type: str | None = None
    dataset_id: str | None = None
    dataset: str | None = None
    category: str | None = None
    subcategory: str | None = None
    region: str | None = None
    delay: int | None = None
    universe: str | None = None
    coverage: float | None = None
    alpha_count: int | None = None
    user_count: int | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any], *, full: bool = False) -> "DataField":
        raw_dataset = data.get("dataset")
        dataset: dict[str, Any] = raw_dataset if isinstance(raw_dataset, dict) else {}
        return cls(
            id=str(data.get("id")),
            description=_short(data.get("description"), full),
            type=data.get("type"),
            dataset_id=dataset.get("id"),
            dataset=dataset.get("name"),
            category=_named(data.get("category")),
            subcategory=_named(data.get("subcategory")),
            region=data.get("region"),
            delay=data.get("delay"),
            universe=data.get("universe"),
            coverage=data.get("coverage"),
            alpha_count=data.get("alphaCount"),
            user_count=data.get("userCount"),
        )


class DatasetPage(Page[Dataset]):
    pass


class DataFieldPage(Page[DataField]):
    pass


class DataCategory(CompactModel):
    id: str
    name: str | None = None
    dataset_count: int | None = None
    field_count: int | None = None
    children: list["DataCategory"] | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "DataCategory":
        return cls(
            id=str(data.get("id")),
            name=data.get("name"),
            dataset_count=data.get("datasetCount"),
            field_count=data.get("fieldCount"),
            children=[cls.from_api(child) for child in data.get("children") or []] or None,
        )


class DataCategoryList(CompactModel):
    items: list[DataCategory]


class Operator(CompactModel):
    name: str
    category: str | None = None
    definition: str | None = None
    scope: list[str] | str | None = None
    level: str | None = None
    description: str | None = None
    documentation: str | None = None


class OperatorList(CompactModel):
    count: int
    items: list[Operator]
