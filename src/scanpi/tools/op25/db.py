"""SQLite event log for the OP25 P25 trunking tool."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS p25_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tgid        INTEGER NOT NULL,
    tg_name     TEXT,
    category    TEXT,
    rid         INTEGER,
    freq_mhz    REAL,
    start_ts    REAL NOT NULL,
    end_ts      REAL,
    duration_s  REAL,
    clip_path   TEXT,
    transcript  TEXT,
    transcript_status TEXT,
    alert_kind  TEXT,           -- NULL, or 'fire'|'violence'|'pursuit'|'medical'|'emergency'|'accident'
    alert_match TEXT             -- the actual keyword that fired
);
CREATE INDEX IF NOT EXISTS idx_p25_tgid  ON p25_calls(tgid);
CREATE INDEX IF NOT EXISTS idx_p25_start ON p25_calls(start_ts);
"""


class OP25DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    def connect(self):
        self._conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        # Migration for DBs from before alerts were added.
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(p25_calls)").fetchall()}
        if "alert_kind" not in cols:
            self._conn.execute("ALTER TABLE p25_calls ADD COLUMN alert_kind TEXT")
        if "alert_match" not in cols:
            self._conn.execute("ALTER TABLE p25_calls ADD COLUMN alert_match TEXT")
        # Indexes that depend on post-migration columns.
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_p25_alert ON p25_calls(alert_kind)"
        )

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if not self._conn:
            raise RuntimeError("OP25DB not connected")
        return self._conn

    def open_call(self, tgid: int, tg_name: str, category: str,
                  rid: int, freq_mhz: float, start_ts: float) -> int:
        cur = self.conn.execute(
            "INSERT INTO p25_calls (tgid, tg_name, category, rid, freq_mhz, start_ts) "
            "VALUES (?,?,?,?,?,?)",
            (tgid, tg_name, category, rid, freq_mhz, start_ts),
        )
        return cur.lastrowid

    def close_call(self, call_id: int, end_ts: float, clip_path: str | None = None):
        self.conn.execute(
            "UPDATE p25_calls SET end_ts = ?, duration_s = ? - start_ts, clip_path = ? "
            "WHERE id = ?",
            (end_ts, end_ts, clip_path, call_id),
        )

    def set_transcript(self, call_id: int, text: str | None, status: str,
                       alert_kind: str | None = None, alert_match: str | None = None):
        self.conn.execute(
            "UPDATE p25_calls SET transcript = ?, transcript_status = ?, "
            "alert_kind = ?, alert_match = ? WHERE id = ?",
            (text, status, alert_kind, alert_match, call_id),
        )

    def recent_alerts(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM p25_calls WHERE alert_kind IS NOT NULL "
            "ORDER BY start_ts DESC LIMIT ?", (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def alert_counts_24h(self) -> dict[str, int]:
        import time as _t
        since = _t.time() - 86400
        rows = self.conn.execute(
            "SELECT alert_kind, COUNT(*) AS n FROM p25_calls "
            "WHERE alert_kind IS NOT NULL AND start_ts >= ? GROUP BY alert_kind",
            (since,),
        ).fetchall()
        return {r["alert_kind"]: r["n"] for r in rows}

    def orphan_cleanup(self):
        cur = self.conn.execute(
            "UPDATE p25_calls SET end_ts = start_ts, duration_s = 0 WHERE end_ts IS NULL"
        )
        return cur.rowcount

    def recent(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM p25_calls ORDER BY start_ts DESC LIMIT ?", (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def talkgroup_stats(self, since_ts: float = 0.0) -> list[dict]:
        rows = self.conn.execute(
            "SELECT tgid, MAX(tg_name) AS tg_name, MAX(category) AS category, "
            "COUNT(*) AS call_count, SUM(duration_s) AS total_airtime_s, "
            "AVG(duration_s) AS avg_duration_s, MAX(end_ts) AS last_active "
            "FROM p25_calls WHERE end_ts IS NOT NULL AND start_ts >= ? "
            "GROUP BY tgid ORDER BY call_count DESC",
            (since_ts,),
        ).fetchall()
        return [dict(r) for r in rows]

    def last_call_end_ts(self) -> float | None:
        row = self.conn.execute(
            "SELECT MAX(end_ts) AS t FROM p25_calls WHERE end_ts IS NOT NULL"
        ).fetchone()
        return row["t"] if row and row["t"] else None

    def get_call(self, call_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM p25_calls WHERE id = ?", (call_id,),
        ).fetchone()
        return dict(row) if row else None

    def hourly_activity(self, hours: int = 24) -> list[dict]:
        """Return [{hour_bucket: int, call_count: int, airtime_s: float}, ...]
        hour_bucket 0 = (hours) ago, bucket hours-1 = now-ish.
        """
        import time as _t
        since = _t.time() - hours * 3600
        rows = self.conn.execute(
            "SELECT CAST((start_ts - ?) / 3600 AS INTEGER) AS hour_bucket, "
            "COUNT(*) AS call_count, COALESCE(SUM(duration_s), 0) AS airtime_s "
            "FROM p25_calls WHERE end_ts IS NOT NULL AND start_ts >= ? "
            "GROUP BY hour_bucket ORDER BY hour_bucket",
            (since, since),
        ).fetchall()
        return [dict(r) for r in rows]

    def all_calls(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM p25_calls ORDER BY start_ts DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def search(self, query: str, limit: int = 200) -> list[dict]:
        """Full-text style search across transcript, tg_name, category, alert_kind.

        Uses SQLite LIKE with % wildcards. Case-insensitive via LOWER().
        """
        q = query.strip()
        if not q:
            return []
        like = f"%{q.lower()}%"
        rows = self.conn.execute(
            "SELECT * FROM p25_calls WHERE "
            "LOWER(COALESCE(transcript,'')) LIKE ? OR "
            "LOWER(COALESCE(tg_name,''))    LIKE ? OR "
            "LOWER(COALESCE(category,''))   LIKE ? OR "
            "LOWER(COALESCE(alert_kind,'')) LIKE ? OR "
            "LOWER(COALESCE(alert_match,''))LIKE ? "
            "ORDER BY start_ts DESC LIMIT ?",
            (like, like, like, like, like, limit),
        ).fetchall()
        return [dict(r) for r in rows]
