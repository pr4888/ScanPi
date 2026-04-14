"""SQLite log for YARD Stick One ISM activity."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS ysone_bursts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    freq_hz     INTEGER NOT NULL,
    rssi_dbm    REAL,
    duration_ms REAL,
    modulation  TEXT,         -- 'ask_ook', 'fsk', 'msk', etc.
    bytes_hex   TEXT,         -- optional captured payload, hex-encoded
    note        TEXT          -- heuristic guess: 'weather', 'lora', 'keyfob', ''
);
CREATE INDEX IF NOT EXISTS idx_yb_ts    ON ysone_bursts(ts);
CREATE INDEX IF NOT EXISTS idx_yb_freq  ON ysone_bursts(freq_hz);

CREATE TABLE IF NOT EXISTS ysone_spectrum (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    freq_hz   INTEGER NOT NULL,
    rssi_dbm  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ys_ts    ON ysone_spectrum(ts);
CREATE INDEX IF NOT EXISTS idx_ys_freq  ON ysone_spectrum(freq_hz);
"""


class YSoneDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    def connect(self):
        self._conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if not self._conn:
            raise RuntimeError("YSoneDB not connected")
        return self._conn

    def log_burst(self, ts: float, freq_hz: int, rssi_dbm: float,
                  duration_ms: float = 0.0, modulation: str = "",
                  bytes_hex: str = "", note: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO ysone_bursts (ts, freq_hz, rssi_dbm, duration_ms, modulation, bytes_hex, note) "
            "VALUES (?,?,?,?,?,?,?)",
            (ts, freq_hz, rssi_dbm, duration_ms, modulation, bytes_hex, note),
        )
        return cur.lastrowid

    def log_spectrum(self, ts: float, freq_hz: int, rssi_dbm: float):
        self.conn.execute(
            "INSERT INTO ysone_spectrum (ts, freq_hz, rssi_dbm) VALUES (?,?,?)",
            (ts, freq_hz, rssi_dbm),
        )

    def prune_spectrum(self, keep_seconds: float):
        import time as _t
        self.conn.execute(
            "DELETE FROM ysone_spectrum WHERE ts < ?", (_t.time() - keep_seconds,),
        )

    def recent_bursts(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM ysone_bursts ORDER BY ts DESC LIMIT ?", (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def latest_spectrum(self) -> list[dict]:
        """Latest RSSI per frequency (from the most recent full sweep)."""
        rows = self.conn.execute(
            "SELECT freq_hz, rssi_dbm, MAX(ts) AS ts "
            "FROM ysone_spectrum GROUP BY freq_hz ORDER BY freq_hz"
        ).fetchall()
        return [dict(r) for r in rows]

    def burst_freq_histogram(self, hours: float = 24) -> list[dict]:
        import time as _t
        since = _t.time() - hours * 3600
        rows = self.conn.execute(
            "SELECT freq_hz, COUNT(*) AS n FROM ysone_bursts "
            "WHERE ts >= ? GROUP BY freq_hz ORDER BY n DESC",
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]

    def all_time_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM ysone_bursts").fetchone()
        return row["n"] if row else 0

    def last_burst_ts(self) -> float | None:
        row = self.conn.execute("SELECT MAX(ts) AS t FROM ysone_bursts").fetchone()
        return row["t"] if row and row["t"] else None
