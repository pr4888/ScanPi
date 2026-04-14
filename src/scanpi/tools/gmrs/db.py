"""SQLite event log for the GMRS monitor.

Schema is intentionally minimal. Each squelch-open is a tx_event row.
Channel stats roll up via SQL views.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS tx_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel     INTEGER NOT NULL,
    freq_hz     INTEGER NOT NULL,
    start_ts    REAL NOT NULL,
    end_ts      REAL,
    duration_s  REAL,
    peak_rssi   REAL,           -- dBFS
    avg_rssi    REAL,
    ctcss_hz    REAL,           -- detected subtone, NULL = carrier squelch
    ctcss_code  INTEGER,        -- 1-38 or NULL
    clip_path   TEXT,           -- optional audio clip, first 5s
    transcript  TEXT,            -- Whisper output; NULL until transcribed
    transcript_status TEXT,      -- 'pending' / 'ok' / 'failed' / NULL
    alert_kind  TEXT,            -- see src/scanpi/alerts.py
    alert_match TEXT
);

CREATE INDEX IF NOT EXISTS idx_tx_channel   ON tx_events(channel);
CREATE INDEX IF NOT EXISTS idx_tx_start     ON tx_events(start_ts);
CREATE INDEX IF NOT EXISTS idx_tx_ctcss     ON tx_events(ctcss_code);

CREATE VIEW IF NOT EXISTS v_channel_stats AS
SELECT
    channel,
    COUNT(*)                              AS tx_count,
    SUM(duration_s)                       AS total_airtime_s,
    AVG(duration_s)                       AS avg_duration_s,
    MAX(end_ts)                           AS last_active,
    AVG(peak_rssi)                        AS avg_peak_rssi
FROM tx_events
WHERE end_ts IS NOT NULL
GROUP BY channel;
"""


class GmrsDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    def connect(self):
        self._conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        # Lightweight migration for pre-transcription / pre-alerts DBs.
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(tx_events)").fetchall()}
        if "transcript" not in cols:
            self._conn.execute("ALTER TABLE tx_events ADD COLUMN transcript TEXT")
        if "transcript_status" not in cols:
            self._conn.execute("ALTER TABLE tx_events ADD COLUMN transcript_status TEXT")
        if "alert_kind" not in cols:
            self._conn.execute("ALTER TABLE tx_events ADD COLUMN alert_kind TEXT")
        if "alert_match" not in cols:
            self._conn.execute("ALTER TABLE tx_events ADD COLUMN alert_match TEXT")

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if not self._conn:
            raise RuntimeError("GmrsDB not connected")
        return self._conn

    def open_event(self, channel: int, freq_hz: int, start_ts: float, rssi: float) -> int:
        cur = self.conn.execute(
            "INSERT INTO tx_events (channel, freq_hz, start_ts, peak_rssi, avg_rssi) VALUES (?,?,?,?,?)",
            (channel, freq_hz, start_ts, rssi, rssi),
        )
        return cur.lastrowid

    def update_event_rssi(self, event_id: int, rssi: float):
        self.conn.execute(
            "UPDATE tx_events SET peak_rssi = MAX(peak_rssi, ?), "
            "avg_rssi = (avg_rssi + ?) / 2.0 WHERE id = ?",
            (rssi, rssi, event_id),
        )

    def close_event(self, event_id: int, end_ts: float, ctcss_hz: float | None = None,
                    ctcss_code: int | None = None, clip_path: str | None = None):
        self.conn.execute(
            "UPDATE tx_events SET end_ts = ?, duration_s = ? - start_ts, "
            "ctcss_hz = ?, ctcss_code = ?, clip_path = ? WHERE id = ?",
            (end_ts, end_ts, ctcss_hz, ctcss_code, clip_path, event_id),
        )

    def channel_stats(self, since_ts: float = 0.0) -> list[dict]:
        rows = self.conn.execute(
            "SELECT channel, COUNT(*) AS tx_count, "
            "SUM(duration_s) AS total_airtime_s, AVG(duration_s) AS avg_duration_s, "
            "MAX(end_ts) AS last_active, AVG(peak_rssi) AS avg_peak_rssi "
            "FROM tx_events WHERE end_ts IS NOT NULL AND start_ts >= ? "
            "GROUP BY channel ORDER BY tx_count DESC",
            (since_ts,),
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_events(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM tx_events ORDER BY start_ts DESC LIMIT ?", (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_transcript(self, event_id: int, text: str | None, status: str,
                       alert_kind: str | None = None, alert_match: str | None = None):
        self.conn.execute(
            "UPDATE tx_events SET transcript = ?, transcript_status = ?, "
            "alert_kind = ?, alert_match = ? WHERE id = ?",
            (text, status, alert_kind, alert_match, event_id),
        )

    def recent_alerts(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM tx_events WHERE alert_kind IS NOT NULL "
            "ORDER BY start_ts DESC LIMIT ?", (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def last_event_end_ts(self) -> float | None:
        row = self.conn.execute(
            "SELECT MAX(end_ts) AS t FROM tx_events WHERE end_ts IS NOT NULL"
        ).fetchone()
        return row["t"] if row and row["t"] else None

    def hourly_activity(self, channel: int, hours: int = 24) -> list[dict]:
        since = time.time() - hours * 3600
        rows = self.conn.execute(
            "SELECT CAST((start_ts - ?) / 3600 AS INTEGER) AS hour_bucket, "
            "COUNT(*) AS tx_count, SUM(duration_s) AS airtime_s "
            "FROM tx_events WHERE channel = ? AND start_ts >= ? "
            "GROUP BY hour_bucket ORDER BY hour_bucket",
            (since, channel, since),
        ).fetchall()
        return [dict(r) for r in rows]

    def hourly_all(self, hours: int = 24) -> list[dict]:
        """Hourly activity across all channels (for sparkline)."""
        since = time.time() - hours * 3600
        rows = self.conn.execute(
            "SELECT CAST((start_ts - ?) / 3600 AS INTEGER) AS hour_bucket, "
            "COUNT(*) AS tx_count, COALESCE(SUM(duration_s), 0) AS airtime_s "
            "FROM tx_events WHERE end_ts IS NOT NULL AND start_ts >= ? "
            "GROUP BY hour_bucket ORDER BY hour_bucket",
            (since, since),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_event(self, event_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM tx_events WHERE id = ?", (event_id,),
        ).fetchone()
        return dict(row) if row else None

    def all_events(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM tx_events ORDER BY start_ts DESC"
        ).fetchall()
        return [dict(r) for r in rows]
