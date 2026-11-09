from typing import Any

from mcp_server_worldquantbrain.client.alphas import decode_records
from mcp_server_worldquantbrain.client.base import BrainClient
from mcp_server_worldquantbrain.client.paging import page_info, segment
from mcp_server_worldquantbrain.models.user import (
    ActivitySummary,
    Message,
    MessagePage,
    UserProfile,
)
from mcp_server_worldquantbrain.redaction import html_to_text

_PUBLIC_KEYS = {"id", "dateCreated", "verified", "approved", "geniusLevel"}
_IGNORED_KEYS = {"settings", "onboarding", "dateVerified", "dateApproved"}
RECENT_ACTIVITY_ROWS = 30
MESSAGE_TEXT_LIMIT = 2000


async def get_profile(client: BrainClient, *, include_pii: bool) -> UserProfile:
    body = await client.request_json("GET", "/users/self") or {}
    personal = {k: v for k, v in body.items() if k not in _PUBLIC_KEYS | _IGNORED_KEYS}
    present = sorted(key for key, value in personal.items() if value not in (None, "", {}, []))
    return UserProfile(
        id=body.get("id"),
        date_created=body.get("dateCreated"),
        verified=body.get("verified"),
        approved=body.get("approved"),
        genius_level=body.get("geniusLevel"),
        personal={key: personal[key] for key in present} if include_pii else None,
        redacted_fields=[] if include_pii else present,
    )


async def get_activity(client: BrainClient, kind: str) -> ActivitySummary:
    body = await client.request_json("GET", f"/users/self/activities/{segment(kind)}") or {}
    _, rows = decode_records(body.get("records"))
    return ActivitySummary(
        kind=kind,
        yesterday=body.get("yesterday"),
        current=body.get("current"),
        previous=body.get("previous"),
        ytd=body.get("ytd"),
        total=body.get("total"),
        recent=rows[-RECENT_ACTIVITY_ROWS:],
        total_rows=len(rows),
    )


def _message(raw: dict[str, Any]) -> Message:
    text = html_to_text(str(raw.get("description") or ""))
    return Message(
        id=raw.get("id"),
        type=raw.get("type"),
        title=raw.get("title"),
        text=text[:MESSAGE_TEXT_LIMIT],
        date_created=raw.get("dateCreated"),
        read=raw.get("read"),
        truncated=len(text) > MESSAGE_TEXT_LIMIT,
    )


async def list_messages(
    client: BrainClient, *, message_type: str | None, limit: int, offset: int
) -> MessagePage:
    params: dict[str, Any] = {"limit": limit, "offset": offset, "order": "-dateCreated"}
    if message_type:
        params["type"] = message_type
    body = await client.request_json("GET", "/users/self/messages", params=params) or {}
    results = body.get("results") or []
    next_offset, truncated = page_info(body.get("count"), offset, len(results), limit)
    return MessagePage(
        count=body.get("count"),
        next_offset=next_offset,
        truncated_by_api=truncated,
        items=[_message(item) for item in results],
    )
