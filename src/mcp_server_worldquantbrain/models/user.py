from typing import Any

from mcp_server_worldquantbrain.models.base import CompactModel, Page


class UserProfile(CompactModel):
    id: str | None = None
    date_created: str | None = None
    verified: bool | None = None
    approved: bool | None = None
    genius_level: Any = None
    personal: dict[str, Any] | None = None
    redacted_fields: list[str] = []


class ActivitySummary(CompactModel):
    kind: str
    yesterday: Any = None
    current: Any = None
    previous: Any = None
    ytd: Any = None
    total: Any = None
    recent: list[dict[str, Any]] = []
    total_rows: int = 0


class Message(CompactModel):
    id: str | None = None
    type: str | None = None
    title: str | None = None
    text: str | None = None
    date_created: str | None = None
    read: bool | None = None
    truncated: bool = False


class MessagePage(Page[Message]):
    pass
