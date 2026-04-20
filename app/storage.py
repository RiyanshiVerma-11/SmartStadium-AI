import json
import logging
from pathlib import Path
from typing import Any, List, Dict, Optional

import aiosqlite

logger = logging.getLogger("SmartStadium-Storage")

class Storage:
    """
    Asynchronous SQLite-based persistence layer for logging stadium events, AI decisions, and telemetry.
    Ensures all critical actions and sensor snapshots are recorded for audit and analysis without blocking the event loop.
    """
    def __init__(self, db_path: Path):
        """Initialize the storage layer with a specific database path."""
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    async def initialize(self) -> None:
        """Create necessary database tables if they do not already exist."""
        if not self._conn:
            self._conn = await aiosqlite.connect(self.db_path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA busy_timeout=5000;")
            # WAL can fail on some bind-mounted filesystems (common in Docker Desktop on Windows).
            # Fall back to DELETE journal mode to prevent startup/background I/O crashes.
            try:
                await self._conn.execute("PRAGMA journal_mode=WAL;")
            except aiosqlite.OperationalError:
                await self._conn.execute("PRAGMA journal_mode=DELETE;")
            
            await self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS event_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    event_type TEXT NOT NULL,
                    scenario TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ai_queries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    scenario TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    profile_json TEXT NOT NULL,
                    response_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS operator_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    action TEXT NOT NULL,
                    scenario TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS telemetry_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    scenario TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS announcement_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    scenario TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    audio_provider TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );
                """
            )
            # Performance Tuning: Add indexes for rapid retrieval by scenario and time
            await self._conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_event_log_scenario ON event_log(scenario);
                CREATE INDEX IF NOT EXISTS idx_telemetry_created ON telemetry_snapshots(created_at);
                CREATE INDEX IF NOT EXISTS idx_ai_queries_scenario ON ai_queries(scenario);
                """
            )
            await self._conn.commit()

    async def close(self) -> None:
        """Gracefully close the persistent database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def log_event(self, event_type: str, scenario: str, severity: str, summary: str, details: dict[str, Any]) -> None:
        """Log a high-level stadium event or scenario change."""
        await self._conn.execute(
            """
            INSERT INTO event_log (event_type, scenario, severity, summary, details_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event_type, scenario, severity, summary, json.dumps(details)),
        )
        await self._conn.commit()

    async def insert_ai_query(self, scenario: str, prompt: str, profile: dict[str, Any], response: dict[str, Any]) -> None:
        await self._conn.execute(
            """
            INSERT INTO ai_queries (scenario, prompt, profile_json, response_json)
            VALUES (?, ?, ?, ?)
            """,
            (scenario, prompt, json.dumps(profile), json.dumps(response)),
        )
        await self._conn.commit()

    async def log_operator_action(self, action: str, scenario: str, actor: str, details: dict[str, Any]) -> None:
        await self._conn.execute(
            """
            INSERT INTO operator_actions (action, scenario, actor, details_json)
            VALUES (?, ?, ?, ?)
            """,
            (action, scenario, actor, json.dumps(details)),
        )
        await self._conn.commit()

    async def insert_snapshot(self, snapshot: dict[str, Any]) -> None:
        await self._conn.execute(
            """
            INSERT INTO telemetry_snapshots (scenario, snapshot_json)
            VALUES (?, ?)
            """,
            (snapshot["scenario"], json.dumps(snapshot)),
        )
        # Prevent unbounded growth — keep only the most recent 200 snapshots
        await self._conn.execute(
            """
            DELETE FROM telemetry_snapshots
            WHERE id NOT IN (
                SELECT id FROM telemetry_snapshots ORDER BY id DESC LIMIT 200
            )
            """
        )
        await self._conn.commit()

    async def log_events_batch(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        await self._conn.executemany(
            """
            INSERT INTO event_log (event_type, scenario, severity, summary, details_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    event["event_type"],
                    event["scenario"],
                    event["severity"],
                    event["summary"],
                    json.dumps(event["details"]),
                )
                for event in events
            ],
        )
        await self._conn.commit()

    async def log_announcement(
        self,
        scenario: str,
        severity: str,
        message: str,
        audio_provider: str,
        details: dict[str, Any],
    ) -> None:
        await self._conn.execute(
            """
            INSERT INTO announcement_log (scenario, severity, message, audio_provider, details_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (scenario, severity, message, audio_provider, json.dumps(details)),
        )
        await self._conn.commit()

    async def get_dashboard_analytics(self, scenario: str) -> dict[str, Any]:
        async with self._conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM telemetry_snapshots WHERE scenario = ?) AS snapshot_count,
                (SELECT COUNT(*) FROM ai_queries WHERE scenario = ?) AS ai_query_count,
                (SELECT COUNT(*) FROM operator_actions WHERE scenario = ?) AS operator_action_count,
                (SELECT COUNT(*) FROM event_log WHERE scenario = ?) AS alert_count,
                (SELECT COUNT(*) FROM announcement_log WHERE scenario = ?) AS announcement_count
            """,
            (scenario, scenario, scenario, scenario, scenario),
        ) as cursor:
            stats = await cursor.fetchone()

        async with self._conn.execute(
            """
            SELECT created_at, severity, summary
            FROM event_log
            WHERE scenario = ?
            ORDER BY id DESC
            LIMIT 6
            """,
            (scenario,),
        ) as cursor:
            history_rows = await cursor.fetchall()

        async with self._conn.execute(
            """
            SELECT created_at, prompt, response_json
            FROM ai_queries
            WHERE scenario = ?
            ORDER BY id DESC
            LIMIT 5
            """,
            (scenario,),
        ) as cursor:
            query_rows = await cursor.fetchall()

        async with self._conn.execute(
            """
            SELECT created_at, severity, message, audio_provider
            FROM announcement_log
            WHERE scenario = ?
            ORDER BY id DESC
            LIMIT 5
            """,
            (scenario,),
        ) as cursor:
            announcement_rows = await cursor.fetchall()

        return {
            "totals": {
                "snapshots": stats["snapshot_count"],
                "ai_queries": stats["ai_query_count"],
                "operator_actions": stats["operator_action_count"],
                "alerts": stats["alert_count"],
                "announcements": stats["announcement_count"],
            },
            "recent_alerts": [
                {
                    "created_at": row["created_at"],
                    "severity": row["severity"],
                    "summary": row["summary"],
                }
                for row in history_rows
            ],
            "recent_queries": [
                {
                    "created_at": row["created_at"],
                    "prompt": row["prompt"],
                    "response_message": json.loads(row["response_json"]).get("message", ""),
                }
                for row in query_rows
            ],
            "recent_announcements": [
                {
                    "created_at": row["created_at"],
                    "severity": row["severity"],
                    "message": row["message"],
                    "audio_provider": row["audio_provider"],
                }
                for row in announcement_rows
            ],
        }
