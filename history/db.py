"""Persistence layer for command and action history.

A small SQLite-backed log of mutating operations, stored at
``~/.compose-mind/history.db``. Diagnostic (read-only) tools are never logged.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

# Mutating tools whose operations are worth recording (for undo / audit).
# Diagnostic tools are intentionally excluded — there is nothing to undo.
MUTATION_TOOLS = frozenset(
    {
        "restart_service",
        "scale_service",
        "stop_service",
        "stop_all",
    }
)

DEFAULT_DB_DIR = Path.home() / ".compose-mind"
DEFAULT_DB_PATH = DEFAULT_DB_DIR / "history.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    TEXT    NOT NULL,
    intent       TEXT    NOT NULL,
    tool_name    TEXT    NOT NULL,
    tool_args    TEXT    NOT NULL,
    before_state TEXT    NOT NULL,
    result       TEXT    NOT NULL,
    success      INTEGER NOT NULL,
    undone       INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass
class OperationRecord:
    """A single logged mutation."""

    id: int
    timestamp: datetime
    intent: str
    tool_name: str
    tool_args: dict
    before_state: dict
    result: dict
    success: bool
    undone: bool


def _dumps(value: Optional[dict]) -> str:
    """Serialize a dict to JSON, tolerating None and non-trivial types."""
    return json.dumps(value or {}, default=str)


def _loads(text: Optional[str]) -> dict:
    """Deserialize JSON text back to a dict, tolerating None/empty."""
    if not text:
        return {}
    try:
        loaded = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


class HistoryDB:
    """SQLite-backed log of mutating operations."""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _row_to_record(self, row: sqlite3.Row) -> OperationRecord:
        return OperationRecord(
            id=row["id"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            intent=row["intent"],
            tool_name=row["tool_name"],
            tool_args=_loads(row["tool_args"]),
            before_state=_loads(row["before_state"]),
            result=_loads(row["result"]),
            success=bool(row["success"]),
            undone=bool(row["undone"]),
        )

    def save(
        self,
        intent: str,
        tool_name: str,
        tool_args: dict,
        before_state: dict,
        result: dict,
        success: bool,
    ) -> int:
        """Persist a mutation and return its record id.

        Non-mutating tools are silently skipped and return ``-1``.
        """
        if tool_name not in MUTATION_TOOLS:
            return -1

        timestamp = datetime.now().isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO operations (
                    timestamp, intent, tool_name, tool_args,
                    before_state, result, success, undone
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    timestamp,
                    intent,
                    tool_name,
                    _dumps(tool_args),
                    _dumps(before_state),
                    _dumps(result),
                    1 if success else 0,
                ),
            )
            return int(cursor.lastrowid)

    def get_last_undoable(self) -> Optional[OperationRecord]:
        """Return the most recent successful mutation not yet undone."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM operations
                WHERE success = 1 AND undone = 0
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        return self._row_to_record(row) if row else None

    def mark_undone(self, record_id: int) -> None:
        """Mark a record as undone."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE operations SET undone = 1 WHERE id = ?",
                (record_id,),
            )

    def get_all(self, limit: int = 50) -> list[OperationRecord]:
        """Return the most recent operations, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM operations ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def clear(self) -> None:
        """Delete all recorded operations."""
        with self._connect() as conn:
            conn.execute("DELETE FROM operations")
