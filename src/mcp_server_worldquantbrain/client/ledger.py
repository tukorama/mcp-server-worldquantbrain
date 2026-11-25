import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from mcp_server_worldquantbrain.client.session import utcnow
from mcp_server_worldquantbrain.errors import LedgerUnavailable
from mcp_server_worldquantbrain.models.base import as_dict

logger = logging.getLogger(__name__)

LedgerStatus = Literal[
    "WAITING",
    "SIMULATING",
    "RUNNING",
    "QUEUED",
    "PENDING",
    "COMPLETE",
    "WARNING",
    "ERROR",
    "STOPPED",
    "FAILED",
    "CANCELLED",
    "UNKNOWN",
]
VALID_LEDGER_STATUSES: frozenset[str] = frozenset(
    {
        "WAITING",
        "SIMULATING",
        "RUNNING",
        "QUEUED",
        "PENDING",
        "COMPLETE",
        "WARNING",
        "ERROR",
        "STOPPED",
        "FAILED",
        "CANCELLED",
        "UNKNOWN",
    }
)


# Other spellings of states the alphabet has. BRAIN writes CANCELED with one L.
_STATUS_SPELLINGS: dict[str, LedgerStatus] = {
    "FAIL": "FAILED",
    "CANCELED": "CANCELLED",
    "COMPLETED": "COMPLETE",
}


def ledger_status(status: str) -> LedgerStatus:
    """Map a status BRAIN reported onto the closed simulation state alphabet.

    A known spelling such as FAIL maps onto its state. Polling only stops on a final state, so any
    other value is filed as a failure: final, and free to run again.
    """
    if status in VALID_LEDGER_STATUSES:
        return cast("LedgerStatus", status)
    return _STATUS_SPELLINGS.get(status, "FAILED")


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
# A row here says a process is checking and starting this fingerprint, so other processes sharing
# the file wait for it instead of starting it again.
_RESERVATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS reservations (
    fingerprint TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    expires_at REAL NOT NULL
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
    def _connection(self) -> Generator[sqlite3.Connection, None, None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        if os.name == "posix" and self.path.exists():
            self.path.chmod(0o600)
        try:
            connection.execute(_SCHEMA)
            connection.execute(_RESERVATIONS_SCHEMA)
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _entry(connection: sqlite3.Connection, key: str) -> LedgerEntry | None:
        row = connection.execute(
            "SELECT simulation_id, status, alpha_id FROM simulations WHERE fingerprint = ?",
            (key,),
        ).fetchone()
        return LedgerEntry(*row) if row else None

    def get(self, key: str) -> LedgerEntry | None:
        if not self.enabled:
            return None
        try:
            with self._connection() as connection:
                return self._entry(connection, key)
        except (sqlite3.Error, OSError) as exc:
            logger.warning("Ledger read failed: %s", exc)
            return None

    def claim(
        self, keys: Sequence[str], owner: str, seconds: float
    ) -> dict[str, LedgerEntry] | None:
        """Reserve `keys` for `owner` and read their rows, in one transaction.

        Returns None while another owner holds an unexpired reservation on any of them. Raises
        LedgerUnavailable when the file cannot be used, since going ahead without the check could
        spend quota on a simulation that already ran.
        """
        if not self.enabled:
            return {}
        now = time.time()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                for key in keys:
                    held = connection.execute(
                        """
                        SELECT 1 FROM reservations
                        WHERE fingerprint = ? AND owner != ? AND expires_at > ?
                        """,
                        (key, owner, now),
                    ).fetchone()
                    if held:
                        return None
                connection.executemany(
                    """
                    INSERT INTO reservations VALUES (?, ?, ?)
                    ON CONFLICT(fingerprint) DO UPDATE SET
                        owner = excluded.owner,
                        expires_at = excluded.expires_at
                    """,
                    [(key, owner, now + seconds) for key in keys],
                )
                entries = {key: self._entry(connection, key) for key in keys}
        except (sqlite3.Error, OSError) as exc:
            raise LedgerUnavailable(
                f"The simulation ledger {self.path} cannot be used: {exc}.",
                hint=(
                    "Nothing was submitted, since without the ledger a repeat could spend quota "
                    + "twice. Fix or move the file, set WQB_LEDGER=false to run without duplicate "
                    + "detection, or pass force=True."
                ),
            ) from exc
        return {key: entry for key, entry in entries.items() if entry is not None}

    def renew(self, keys: Sequence[str], owner: str, seconds: float) -> None:
        self._write_many(
            "UPDATE reservations SET expires_at = ? WHERE fingerprint = ? AND owner = ?",
            [(time.time() + seconds, key, owner) for key in keys],
        )

    def release(self, keys: Sequence[str], owner: str) -> None:
        self._write_many(
            "DELETE FROM reservations WHERE fingerprint = ? AND owner = ?",
            [(key, owner) for key in keys],
        )

    def _write_many(self, statement: str, rows: list[tuple[Any, ...]]) -> None:
        if not self.enabled:
            return
        try:
            with self._connection() as connection:
                connection.executemany(statement, rows)
        except (sqlite3.Error, OSError) as exc:
            logger.warning("Ledger reservation update failed: %s", exc)

    def members(self, simulation_id: str) -> dict[str, dict[str, Any]]:
        """Return the payloads filed under `simulation_id` by fingerprint: a batch's members."""
        if not self.enabled:
            return {}
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT fingerprint, expression, settings FROM simulations
                    WHERE simulation_id = ?
                    """,
                    (simulation_id,),
                ).fetchall()
            return {
                key: {"regular": expression, "settings": as_dict(json.loads(settings))}
                for key, expression, settings in rows
            }
        except (sqlite3.Error, OSError, ValueError) as exc:
            logger.warning("Ledger read failed: %s", exc)
            return {}

    def record(
        self,
        payload: dict[str, Any],
        simulation_id: str,
        status: str,
        alpha_id: str | None = None,
        *,
        key: str | None = None,
    ) -> None:
        """File a simulation under `key`, the payload's fingerprint unless given."""
        if not self.enabled:
            return
        normalized_status = ledger_status(status)
        try:
            with self._connection() as connection:
                connection.execute(
                    # A run that has not produced an alpha must not erase one that did, or a
                    # failed force=True rerun would cost the earlier result.
                    """
                    INSERT INTO simulations VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(fingerprint) DO UPDATE SET
                        simulation_id = excluded.simulation_id,
                        status = excluded.status,
                        alpha_id = excluded.alpha_id,
                        updated_at = excluded.updated_at
                    WHERE excluded.alpha_id IS NOT NULL OR simulations.alpha_id IS NULL
                    """,
                    (
                        key or fingerprint(payload),
                        simulation_id,
                        normalized_status,
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
        normalized_status = ledger_status(status)
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    UPDATE simulations
                    SET status = ?, alpha_id = COALESCE(?, alpha_id), updated_at = ?
                    WHERE simulation_id = ? AND (alpha_id IS NULL OR ? IS NOT NULL)
                    """,
                    (normalized_status, alpha_id, utcnow().isoformat(), simulation_id, alpha_id),
                )
        except (sqlite3.Error, OSError) as exc:
            logger.warning("Ledger update failed: %s", exc)

    def forget(self, key: str, simulation_id: str | None = None) -> None:
        """Drop the row for `key`; given `simulation_id`, only while the row still points at it."""
        if not self.enabled:
            return
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    DELETE FROM simulations
                    WHERE fingerprint = ? AND (? IS NULL OR simulation_id = ?)
                    """,
                    (key, simulation_id, simulation_id),
                )
        except (sqlite3.Error, OSError) as exc:
            logger.warning("Ledger delete failed: %s", exc)
