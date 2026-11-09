from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, SerializerFunctionWrapHandler, model_serializer

T = TypeVar("T")


class CompactModel(BaseModel):
    """Leaves nulls out of tool output, since every key costs agent context."""

    model_config = ConfigDict(extra="ignore")

    @model_serializer(mode="wrap")
    def _omit_none(self, handler: SerializerFunctionWrapHandler) -> Any:
        data = handler(self)
        return {key: value for key, value in data.items() if value is not None}


class Page(CompactModel, Generic[T]):
    count: int | None = None
    next_offset: int | None = None
    truncated_by_api: bool = False
    items: list[T] = []
