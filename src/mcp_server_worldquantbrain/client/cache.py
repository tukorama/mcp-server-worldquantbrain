import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from mcp_server_worldquantbrain.client.session import write_private

logger = logging.getLogger(__name__)

WEEK = 7 * 24 * 3600
DAY = 24 * 3600


class JsonCache:
    """Small file cache for catalogues that rarely change. A global TTL of 0 turns it off."""

    def __init__(self, directory: Path, ttl_override: int | None = None) -> None:
        self.directory = directory
        self.ttl_override = ttl_override

    @property
    def enabled(self) -> bool:
        return self.ttl_override != 0

    def _path(self, key: str) -> Path:
        return self.directory / f"{hashlib.sha256(key.encode()).hexdigest()[:32]}.json"

    def get(self, key: str, ttl: int) -> Any | None:
        if not self.enabled:
            return None
        try:
            entry = json.loads(self._path(key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        limit = self.ttl_override if self.ttl_override is not None else ttl
        if not isinstance(entry, dict) or time.time() - entry.get("saved_at", 0) > limit:
            return None
        return entry.get("data")

    def set(self, key: str, data: Any) -> None:
        if not self.enabled:
            return
        try:
            write_private(self._path(key), {"saved_at": time.time(), "data": data})
        except OSError as exc:
            logger.warning("Could not write cache entry: %s", exc)
