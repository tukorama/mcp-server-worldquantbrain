import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

PENDING_TTL = timedelta(minutes=30)


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class SessionData:
    base_url: str
    cookies: list[dict[str, str]] = field(default_factory=list)
    user_id: str | None = None
    version: int = 1


@dataclass
class PendingInquiry:
    inquiry_url: str
    created_at: str
    cookies: list[dict[str, str]] = field(default_factory=list)

    def expired(self) -> bool:
        return utcnow() - datetime.fromisoformat(self.created_at) > PENDING_TTL


def write_private(path: Path, payload: Any) -> None:
    """Atomically write JSON readable only by the current user."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.pending_path = path.with_name(f"{path.stem}.pending{path.suffix}")

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def load(self) -> SessionData | None:
        data = self._read(self.path)
        if data is None or data.get("version") != 1:
            return None
        cookies = data.get("cookies")
        valid_cookies = isinstance(cookies, list) and all(
            isinstance(c, dict)
            and isinstance(c.get("name"), str)
            and isinstance(c.get("value"), str)
            for c in cookies
        )
        if not valid_cookies or not isinstance(data.get("base_url"), str):
            return None
        return SessionData(
            **{k: v for k, v in data.items() if k in SessionData.__dataclass_fields__}
        )

    def save(self, session: SessionData) -> None:
        write_private(self.path, asdict(session))

    def mtime(self) -> float | None:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return None

    def load_pending(self) -> PendingInquiry | None:
        data = self._read(self.pending_path)
        if data is None:
            return None
        try:
            pending = PendingInquiry(**data)
            expired = pending.expired()
        except (TypeError, ValueError):
            self.clear_pending()
            return None
        if expired:
            self.clear_pending()
            return None
        return pending

    def save_pending(self, url: str, cookies: list[dict[str, str]]) -> None:
        pending = PendingInquiry(url, utcnow().isoformat(), cookies)
        write_private(self.pending_path, asdict(pending))

    def clear_pending(self) -> None:
        self.pending_path.unlink(missing_ok=True)
