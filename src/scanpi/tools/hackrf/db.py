"""SQLite event log for the HackRF wideband channelizer.

Schema mirrors the GMRS tx_events table for consistency, except the channel
identifier is a string label (e.g. "GMRS-1", "MARINE-16") because HackRF
profiles may cover heterogeneous bands. We also persist the configured
freq_hz so historical events stay correct after a profile swap.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS tx_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel     TEXT NOT NULL,             -- channel name from the profile
    freq_hz     INTEGER NOT NULL,
    start_ts    REAL NOT NULL,
    end_ts      REAL,
    duration_s  REAL,
    peak_rssi   REAL,                      -- dBFS
    avg_rssi    REAL,
    clip_path   TEXT,                      -- WAV on disk
    transcript  TEXT,
    transcript_status TEXT,                -- 'pending' / 'ok' / 'failed'
    alert_kind  TEXT,
    alert_match TEXT
);

CREATE INDEX IF NOT EXISTS idx_hk_channel ON tx_events(channel);
CREATE INDEX IF NOT EXISTS idx_hk_start   ON tx_events(start_ts);

CREATE TABLE IF NOT EXISTS profile_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    loaded_ts    REAL NOT NULL,
    profile_id   TEXT,
    center_hz    INTEGER,
    sample_rate  INTEGER,
    num_chans    INTEGER,
    channel_count INTEGER,
    source_path  TEXT
);
"""


class HackrfDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    def connect(self):
        self._conn = sqlite3.connect(str(self.path), isolation_level=None,
                                      check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if not self._conn:
            raise RuntimeError("HackrfDB not connected")
        return self._conn

    # ---------- events ----------

    def open_event(self, channel: str, freq_hz: int, start_ts: float, rssi: float) -> int:
        cur = self.conn.execute(
            "INSERT INTO tx_events (channel, freq_hz, start_ts, peak_rssi, avg_rssi) "
            "VALUES (?,?,?,?,?)",
            (channel, freq_hz, start_ts, rssi, rssi),
        )
        return cur.lastrowid

    def update_event_rssi(self, event_id: int, rssi: float):
        self.conn.execute(
            "UPDATE tx_events SET peak_rssi = MAX(peak_rssi, ?), "
            "avg_rssi = (avg_rssi + ?) / 2.0 WHERE id = ?",
            (rssi, rssi, event_id),
        )

    def close_event(self, event_id: int, end_ts: float, clip_path: str | None = None):
        self.conn.execute(
            "UPDATE tx_events SET end_ts = ?, duration_s = ? - start_ts, clip_path = ? "
            "WHERE id = ?",
            (end_ts, end_ts, clip_path, event_id),
        )

    def set_transcript(self, event_id: int, text: str | None, status: str,
                       alert_kind: str | None = None, alert_match: str | None = None):
        self.conn.execute(
            "UPDATE tx_events SET transcript = ?, transcript_status = ?, "
            "alert_kind = ?, alert_match = ? WHERE id = ?",
            (text, status, alert_kind, alert_match, event_id),
        )

    def get_event(self, event_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM tx_events WHERE id = ?", (event_id,),
        ).fetchone()
        return dict(row) if row else None

    def recent_events(self, channel: str | None = None, since_ts: float = 0.0,
                       limit: int = 50) -> list[dict]:
        if channel:
            rows = self.conn.execute(
                "SELECT * FROM tx_events WHERE channel = ? AND start_ts >= ? "
                "ORDER BY start_ts DESC LIMIT ?",
                (channel, since_ts, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM tx_events WHERE start_ts >= ? "
                "ORDER BY start_ts DESC LIMIT ?",
                (since_ts, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def channel_stats(self, since_ts: float = 0.0) -> list[dict]:
        rows = self.conn.execute(
            "SELECT channel, COUNT(*) AS tx_count, "
            "SUM(duration_s) AS total_airtime_s, AVG(duration_s) AS avg_duration_s, "
            "MAX(end_ts) AS last_active, AVG(peak_rssi) AS avg_peak_rssi, "
            "MIN(freq_hz) AS freq_hz "
            "FROM tx_events WHERE end_ts IS NOT NULL AND start_ts >= ? "
            "GROUP BY channel ORDER BY tx_count DESC",
            (since_ts,),
        ).fetchall()
        return [dict(r) for r in rows]

    def last_event_end_ts(self) -> float | None:
        row = self.conn.execute(
            "SELECT MAX(end_ts) AS t FROM tx_events WHERE end_ts IS NOT NULL"
        ).fetchone()
        return row["t"] if row and row["t"] else None

    def all_time_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM tx_events").fetchone()
        return row["n"] if row else 0

    # ---------- profile log ----------

    def log_profile_load(self, profile_id: str, center_hz: int, sample_rate: int,
                          num_chans: int, channel_count: int, source_path: str | None):
        self.conn.execute(
            "INSERT INTO profile_log (loaded_ts, profile_id, center_hz, sample_rate, "
            "num_chans, channel_count, source_path) VALUES (?,?,?,?,?,?,?)",
            (time.time(), profile_id, center_hz, sample_rate, num_chans, channel_count,
             source_path),
        )
