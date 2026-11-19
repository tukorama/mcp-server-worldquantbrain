from typing import Annotated

from pydantic import Field

from mcp_server_worldquantbrain.models.auth import AuthStatus
from mcp_server_worldquantbrain.ops.registry import Call, operation


@operation("Log in to BRAIN")
async def authenticate(
    call: Call,
    force: Annotated[
        bool, Field(description="Discard the saved session and log in again.")
    ] = False,
) -> AuthStatus:
    """Log in with the configured credentials, reusing a saved session when it is still valid.

    Tools log in on their own, so this is only needed to start or restart a biometric check. When
    state is biometric_required, have the user open biometric_url, then call
    complete_authentication.
    """
    return await call.client.authenticate(force=force)


@operation("Finish a biometric login")
async def complete_authentication(call: Call) -> AuthStatus:
    """Finish a login after the user completed the biometric check at biometric_url."""
    return await call.client.complete_authentication()


@operation("Check the BRAIN session", read_only=True)
async def auth_status(call: Call) -> AuthStatus:
    """Report whether a BRAIN session is active and when it expires. Never sends the password."""
    return await call.client.auth_status()
