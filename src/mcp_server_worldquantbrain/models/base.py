from typing import Any, Generic, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer

T = TypeVar("T")


def as_dict(value: Any) -> dict[str, Any]:
    """Return `value` typed as a string-keyed dict when it is a dict, else empty."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def require_dict(value: Any) -> dict[str, Any]:
    """Return `value` as a dict, {} when None, or raise TypeError on non-object JSON."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return cast("dict[str, Any]", value)
    raise TypeError(f"Expected JSON object, got {type(value).__name__}")


def as_list(value: Any) -> list[Any]:
    """Return `value` typed as a list when it is a list, else empty."""
    return cast("list[Any]", value) if isinstance(value, list) else []


def dict_items(value: Any) -> list[dict[str, Any]]:
    """Return dict elements from a list payload, ignoring non-dict items."""
    if not isinstance(value, list):
        return []
    return [cast("dict[str, Any]", item) for item in as_list(value) if isinstance(item, dict)]


class CompactModel(BaseModel):
    """Leaves nulls out of tool output, since every key costs agent context."""

    model_config = ConfigDict(extra="ignore")

    @model_serializer(mode="wrap")
    def _omit_none(self, handler: SerializerFunctionWrapHandler) -> Any:
        data = handler(self)
        return {key: value for key, value in data.items() if value is not None}


def _empty_list() -> list[Any]:
    return []


class Page(CompactModel, Generic[T]):
    count: int | None = None
    next_offset: int | None = None
    truncated_by_api: bool = False
    items: list[T] = Field(default_factory=_empty_list)
