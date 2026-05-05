"""SQLite store for the Alerts tool.

Owns ~/scanpi/alerts.db. Reads from gmrs.db / op25.db are read-only and
happen in the polling worker (see __init__.py).

Schema:
    alerts(id, ts, source, source_call_id, channel, severity, rules_matched,
           transcript, audio_url, ack_ts)
    watchlist_history(rule_name, hit_count, last_hit_ts)

`rules_matched` is a JSON array of rule names. `severity` is one of
'low' | 'medium' | 'high' | 'critical'.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    source          TEXT NOT NULL,          -- 'gmrs' | 'op25'
    source_call_id  INTEGER,                -- the originating row in gmrs.db / op25.db
    channel         TEXT,                   -- e.g. "Ch 16" or "TG 8851"
    severity        TEXT NOT NULL,          -- 'low'|'medium'|'high'|'critical'
    rules_matched   TEXT NOT NULL,          -- JSON array of rule names
    transcript      TEXT,
    audio_url       TEXT,
    ack_ts          REAL                    -- NULL until acknowledged
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts        ON alerts(ts);
CREATE INDEX IF NOT EXISTS idx_alerts_severity  ON alerts(severity);
CREATE INDEX IF NOT EXISTS idx_alerts_source    ON alerts(source);
CREATE INDEX IF NOT EXISTS idx_alerts_unique    ON alerts(source, source_call_id);

CREATE TABLE IF NOT EXISTS watchlist_history (
    rule_name     TEXT PRIMARY KEY,
    hit_count     INTEGER NOT NULL DEFAULT 0,
    last_hit_ts   REAL
);
"""


SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


class AlertsDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    def connect(self):
        self._conn = sqlite3.connect(
            str(self.path), isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if not self._conn:
            raise RuntimeError("AlertsDB not connected")
        return self._conn

    # --- writes --------------------------------------------------------

    def already_recorded(self, source: str, source_call_id: int | None) -> bool:
        """Idempotency guard — same (source, source_call_id) should only alert once."""
        if source_call_id is None:
            return False
        row = self.conn.execute(
            "SELECT id FROM alerts WHERE source = ? AND source_call_id = ? LIMIT 1",
            (source, source_call_id),
        ).fetchone()
        return row is not None

    def insert_alert(
        self,
        ts: float,
        source: str,
        source_call_id: int | None,
        channel: str | None,
        severity: str,
        rules_matched: list[str],
        transcript: str | None,
        audio_url: str | None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO alerts (ts, source, source_call_id, channel, severity, "
            "rules_matched, transcript, audio_url) VALUES (?,?,?,?,?,?,?,?)",
            (
                ts, source, source_call_id, channel, severity,
                json.dumps(rules_matched), transcript, audio_url,
            ),
        )
        return cur.lastrowid

    def bump_history(self, rule_name: str, ts: float):
        self.conn.execute(
            "INSERT INTO watchlist_history(rule_name, hit_count, last_hit_ts) "
            "VALUES (?, 1, ?) "
            "ON CONFLICT(rule_name) DO UPDATE SET "
            "hit_count = hit_count + 1, last_hit_ts = excluded.last_hit_ts",
            (rule_name, ts),
        )

    def acknowledge(self, alert_id: int) -> bool:
        cur = self.conn.execute(
            "UPDATE alerts SET ack_ts = ? WHERE id = ? AND ack_ts IS NULL",
            (time.time(), alert_id),
        )
        return cur.rowcount > 0

    # --- reads ---------------------------------------------------------

    def list_alerts(
        self,
        since_ts: float = 0.0,
        severity: str | None = None,
        source: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        sql = "SELECT * FROM alerts WHERE ts >= ?"
        params: list = [since_ts]
        if severity:
            sql += " AND severity = ?"
            params.append(severity)
        if source:
            sql += " AND source = ?"
            params.append(source)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        rows = self.conn.execute(sql, params).fetchall()
        return [_inflate(dict(r)) for r in rows]

    def get_alert(self, alert_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM alerts WHERE id = ?", (alert_id,)
        ).fetchone()
        return _inflate(dict(row)) if row else None

    def counts(self) -> dict[str, int]:
        out = {"total": 0, "unacked": 0,
               "low": 0, "medium": 0, "high": 0, "critical": 0}
        rows = self.conn.execute(
            "SELECT severity, COUNT(*) AS n, "
            "SUM(CASE WHEN ack_ts IS NULL THEN 1 ELSE 0 END) AS unacked "
            "FROM alerts GROUP BY severity"
        ).fetchall()
        for r in rows:
            out["total"] += r["n"]
            out["unacked"] += r["unacked"] or 0
            if r["severity"] in out:
                out[r["severity"]] = r["n"]
        return out

    def history(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM watchlist_history ORDER BY hit_count DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def _inflate(row: dict) -> dict:
    """Decode the JSON rules_matched field into a list."""
    raw = row.get("rules_matched")
    if isinstance(raw, str):
        try:
            row["rules_matched"] = json.loads(raw)
        except Exception:
            row["rules_matched"] = []
    elif raw is None:
        row["rules_matched"] = []
    return row
