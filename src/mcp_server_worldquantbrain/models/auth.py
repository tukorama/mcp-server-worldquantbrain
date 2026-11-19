from typing import Literal

from mcp_server_worldquantbrain.models.base import CompactModel


class AuthStatus(CompactModel):
    state: Literal["authenticated", "biometric_required", "unauthenticated"]
    user_id: str | None = None
    expires_in_seconds: int | None = None
    biometric_url: str | None = None
    credentials_configured: bool | None = None
    next_step: str | None = None
