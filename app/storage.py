import json
import sqlite3
from pathlib import Path
from typing import Any


class Storage:
    """
    SQLite-based persistence layer for logging stadium events, AI decisions, and telemetry.
    Ensures all critical actions and sensor snapshots are recorded for audit and analysis.
    """
    def __init__(self, db_path: Path):
        """Initialize the storage layer with a specific database path."""
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self) -> None:
        """Create necessary database tables if they do not already exist."""
        with self._connect() as conn:
            conn.executescript(
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
                """
            )

    def log_event(self, event_type: str, scenario: str, severity: str, summary: str, details: dict[str, Any]) -> None:
        """Log a high-level stadium event or scenario change."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO event_log (event_type, scenario, severity, summary, details_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (event_type, scenario, severity, summary, json.dumps(details)),
            )

    def insert_ai_query(self, scenario: str, prompt: str, profile: dict[str, Any], response: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO ai_queries (scenario, prompt, profile_json, response_json)
                VALUES (?, ?, ?, ?)
                """,
                (scenario, prompt, json.dumps(profile), json.dumps(response)),
            )

    def log_operator_action(self, action: str, scenario: str, actor: str, details: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO operator_actions (action, scenario, actor, details_json)
                VALUES (?, ?, ?, ?)
                """,
                (action, scenario, actor, json.dumps(details)),
            )

    def insert_snapshot(self, snapshot: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO telemetry_snapshots (scenario, snapshot_json)
                VALUES (?, ?)
                """,
                (snapshot["scenario"], json.dumps(snapshot)),
            )
            # Prevent unbounded growth — keep only the most recent 200 snapshots
            conn.execute(
                """
                DELETE FROM telemetry_snapshots
                WHERE id NOT IN (
                    SELECT id FROM telemetry_snapshots ORDER BY id DESC LIMIT 200
                )
                """
            )


    def get_dashboard_analytics(self, scenario: str) -> dict[str, Any]:
        with self._connect() as conn:
            stats = conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM telemetry_snapshots WHERE scenario = ?) AS snapshot_count,
                    (SELECT COUNT(*) FROM ai_queries WHERE scenario = ?) AS ai_query_count,
                    (SELECT COUNT(*) FROM operator_actions WHERE scenario = ?) AS operator_action_count,
                    (SELECT COUNT(*) FROM event_log WHERE scenario = ?) AS alert_count
                """,
                (scenario, scenario, scenario, scenario),
            ).fetchone()

            history_rows = conn.execute(
                """
                SELECT created_at, severity, summary
                FROM event_log
                WHERE scenario = ?
                ORDER BY id DESC
                LIMIT 6
                """,
                (scenario,),
            ).fetchall()

            query_rows = conn.execute(
                """
                SELECT created_at, prompt, response_json
                FROM ai_queries
                WHERE scenario = ?
                ORDER BY id DESC
                LIMIT 5
                """,
                (scenario,),
            ).fetchall()

        return {
            "totals": {
                "snapshots": stats["snapshot_count"],
                "ai_queries": stats["ai_query_count"],
                "operator_actions": stats["operator_action_count"],
                "alerts": stats["alert_count"],
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
        }
