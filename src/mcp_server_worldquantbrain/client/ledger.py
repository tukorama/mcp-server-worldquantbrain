import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp_server_worldquantbrain.client.session import utcnow

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS simulations (
    fingerprint TEXT PRIMARY KEY,
    simulation_id TEXT NOT NULL,
    status TEXT NOT NULL,
    alpha_id TEXT,
    expression TEXT NOT NULL,
    settings TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def normalize_expression(expression: str) -> str:
    return re.sub(r"\s+", "", expression)


def fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        {
            "type": payload.get("type"),
            "settings": payload.get("settings"),
            "regular": normalize_expression(str(payload.get("regular", ""))),
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class LedgerEntry:
    simulation_id: str
    status: str
    alpha_id: str | None


class Ledger:
    """Remembers which simulations already ran, so an agent cannot burn quota repeating them."""

    def __init__(self, path: Path, *, enabled: bool = True) -> None:
        self.path = path
        self.enabled = enabled

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute(_SCHEMA)
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, key: str) -> LedgerEntry | None:
        if not self.enabled:
            return None
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT simulation_id, status, alpha_id FROM simulations WHERE fingerprint = ?",
                    (key,),
                ).fetchone()
        except (sqlite3.Error, OSError) as exc:
            logger.warning("Ledger read failed: %s", exc)
            return None
        return LedgerEntry(*row) if row else None

    def record(
        self, payload: dict[str, Any], simulation_id: str, status: str, alpha_id: str | None = None
    ) -> None:
        if not self.enabled:
            return
        try:
            with self._connection() as connection:
                connection.execute(
                    # A run that has not produced an alpha must not erase one that did, or a
                    # failed force=True rerun would cost the earlier result.
                    "INSERT INTO simulations VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(fingerprint) DO UPDATE SET "
                    "simulation_id = excluded.simulation_id, status = excluded.status, "
                    "alpha_id = excluded.alpha_id, updated_at = excluded.updated_at "
                    "WHERE excluded.alpha_id IS NOT NULL OR simulations.alpha_id IS NULL",
                    (
                        fingerprint(payload),
                        simulation_id,
                        status,
                        alpha_id,
                        str(payload.get("regular", "")),
                        json.dumps(payload.get("settings"), sort_keys=True),
                        utcnow().isoformat(),
                    ),
                )
        except (sqlite3.Error, OSError) as exc:
            logger.warning("Ledger write failed: %s", exc)

    def update_by_id(self, simulation_id: str, status: str, alpha_id: str | None) -> None:
        if not self.enabled:
            return
        try:
            with self._connection() as connection:
                connection.execute(
                    "UPDATE simulations SET status = ?, alpha_id = ?, updated_at = ? "
                    "WHERE simulation_id = ?",
                    (status, alpha_id, utcnow().isoformat(), simulation_id),
                )
        except (sqlite3.Error, OSError) as exc:
            logger.warning("Ledger update failed: %s", exc)

    def forget(self, key: str) -> None:
        if not self.enabled:
            return
        try:
            with self._connection() as connection:
                connection.execute("DELETE FROM simulations WHERE fingerprint = ?", (key,))
        except (sqlite3.Error, OSError) as exc:
            logger.warning("Ledger delete failed: %s", exc)
