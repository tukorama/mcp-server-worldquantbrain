from typing import Any


class BrainError(Exception):
    """Base class. `hint` tells an agent what to do next."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        detail: Any = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.detail = detail
        self.hint = hint

    def for_agent(self) -> str:
        return f"{self.message} Next step: {self.hint}" if self.hint else self.message


class ConfigError(BrainError):
    pass


class AuthError(BrainError):
    pass


class BiometricRequired(BrainError):
    def __init__(self, url: str) -> None:
        super().__init__(
            "BRAIN wants a biometric check for this login.",
            status=401,
            hint=f"Ask the user to open {url} in a browser and finish the check, "
            "then call complete_authentication.",
        )
        self.url = url


class RateLimited(BrainError):
    def __init__(self, message: str, *, retry_after: float | None = None, **kwargs: Any) -> None:
        kwargs.setdefault(
            "hint",
            f"Wait about {int(retry_after)} seconds and retry." if retry_after else "Retry later.",
        )
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class PermissionDenied(BrainError):
    def __init__(self, message: str, *, body: Any = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.body = body


class BrainValidationError(BrainError):
    def __init__(self, message: str, *, fields: dict[str, Any] | None = None, **kwargs: Any):
        super().__init__(message, **kwargs)
        self.fields = fields or {}


class NotFound(BrainError):
    pass


class PollTimeout(BrainError):
    def __init__(self, location: str, *, elapsed: float, last_body: Any = None) -> None:
        super().__init__(f"{location} was still in progress after {int(elapsed)} seconds.")
        self.location = location
        self.elapsed = elapsed
        self.last_body = last_body


class ServerError(BrainError):
    pass
