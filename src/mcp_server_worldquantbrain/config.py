import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from mcp_server_worldquantbrain.errors import ConfigError
from mcp_server_worldquantbrain.redaction import register_secret

APP_DIR = "mcp-server-worldquantbrain"

# A .env file comes along with whatever directory the client happens to start in, so it must not
# be able to send stored credentials elsewhere or switch on irreversible actions.
_NOT_FROM_DOTENV = {
    "base_url",
    "credentials_file",
    "session_file",
    "cache_dir",
    "allow_submit",
    "submit_confirm",
    "submit_correlation_gate",
    "http_bearer_token",
}


def config_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_DIR


def cache_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / APP_DIR


@dataclass(frozen=True)
class Credentials:
    email: str
    password: str = field(repr=False)


class _RestrictedDotEnv(PydanticBaseSettingsSource):
    def __init__(
        self, settings_cls: type[BaseSettings], source: PydanticBaseSettingsSource
    ) -> None:
        super().__init__(settings_cls)
        self.source = source

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self.source.get_field_value(field, field_name)

    def __call__(self) -> dict[str, Any]:
        return {key: val for key, val in self.source().items() if key not in _NOT_FROM_DOTENV}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WQB_", env_file=".env", extra="ignore", env_ignore_empty=True
    )

    email: str | None = None
    password: SecretStr | None = None
    credentials_file: Path | None = None

    base_url: str = "https://api.worldquantbrain.com"
    session_file: Path | None = None
    cache_dir: Path | None = None
    cache_ttl: int | None = None
    ledger: bool = True

    allow_submit: bool = False
    submit_confirm: Literal["elicit", "none"] = "elicit"
    submit_correlation_gate: float = 0.7

    max_concurrent_simulations: int = Field(default=2, ge=1)
    simulation_timeout: int = Field(default=900, gt=0)
    http_timeout: float = Field(default=30, gt=0)
    poll_max_interval: float = Field(default=30, ge=1)

    http_bearer_token: SecretStr | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    default_instrument_type: str = "EQUITY"
    default_region: str = "USA"
    default_universe: str = "TOP3000"
    default_delay: int = 1
    default_decay: int = 0
    default_neutralization: str = "INDUSTRY"
    default_truncation: float = 0.08
    default_pasteurization: str = "ON"
    default_unit_handling: str = "VERIFY"
    default_nan_handling: str = "OFF"
    default_test_period: str = ""
    default_max_trade: str = "OFF"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            _RestrictedDotEnv(settings_cls, dotenv_settings),
            file_secret_settings,
        )

    @field_validator("log_level", mode="before")
    @classmethod
    def _uppercase_log_level(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @field_validator("base_url")
    @classmethod
    def _base_url_is_safe(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme != "https" and parts.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("WQB_BASE_URL must use https, since credentials are sent to it")
        return value.rstrip("/")

    @property
    def resolved_session_file(self) -> Path:
        return (self.session_file or config_dir() / "session.json").expanduser()

    @property
    def resolved_cache_dir(self) -> Path:
        return (self.cache_dir or cache_dir()).expanduser()

    @property
    def resolved_credentials_file(self) -> Path:
        return (self.credentials_file or config_dir() / "credentials.json").expanduser()

    def credentials(self) -> Credentials | None:
        if self.email and self.password:
            credentials = Credentials(self.email, self.password.get_secret_value())
        else:
            credentials = self._credentials_from_file()
        if credentials:
            register_secret(credentials.password)
            register_secret(f"{credentials.email}:{credentials.password}")
        return credentials

    def _credentials_from_file(self) -> Credentials | None:
        path = self.resolved_credentials_file
        if not path.exists():
            if self.credentials_file:
                raise ConfigError(f"WQB_CREDENTIALS_FILE points at a missing file: {path}")
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"Could not read {path}: {type(exc).__name__}") from exc
        if isinstance(data, list) and len(data) == 2:
            return Credentials(str(data[0]), str(data[1]))
        if isinstance(data, dict) and data.get("email") and data.get("password"):
            return Credentials(str(data["email"]), str(data["password"]))
        raise ConfigError(
            f'{path} must contain ["email", "password"] or {{"email": ..., "password": ...}}.'
        )

    def require_credentials(self) -> Credentials:
        credentials = self.credentials()
        if credentials is None:
            raise ConfigError(
                "No BRAIN credentials configured.",
                hint="Run `mcp-server-worldquantbrain auth`, or set WQB_EMAIL and WQB_PASSWORD, "
                "or point WQB_CREDENTIALS_FILE at a JSON file "
                f"(default {self.resolved_credentials_file}).",
            )
        return credentials
