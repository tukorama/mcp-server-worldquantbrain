from typing import Annotated, Literal

from pydantic import Field

from mcp_server_worldquantbrain.client import users as api
from mcp_server_worldquantbrain.models.user import ActivitySummary, MessagePage, UserProfile
from mcp_server_worldquantbrain.ops.registry import Call, operation


@operation("Get my profile", read_only=True)
async def get_user_profile(
    call: Call,
    include_pii: Annotated[
        bool, Field(description="Include name, email and phone. Only when the user asks.")
    ] = False,
) -> UserProfile:
    """Return the user's BRAIN profile. Personal fields are withheld by default."""
    return await api.get_profile(call.client, include_pii=include_pii)


@operation("Get my activity", read_only=True)
async def get_user_activity(
    call: Call,
    kind: Annotated[Literal["simulations", "submissions"], Field(description="Activity type.")],
) -> ActivitySummary:
    """Return simulation or submission counts: yesterday, month to date, year to date, total."""
    return await api.get_activity(call.client, kind)


@operation("List my messages", read_only=True)
async def list_messages(
    call: Call,
    type: Annotated[str | None, Field(description="ANNOUNCEMENT or NOTIFICATION.")] = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
    offset: Annotated[int, Field(ge=0, lt=10000)] = 0,
) -> MessagePage:
    """Return platform messages, newest first, as plain text capped at 2,000 characters each."""
    return await api.list_messages(call.client, message_type=type, limit=limit, offset=offset)
