"""SQLite database — frequency catalog, recordings, transcripts."""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS frequencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    freq_hz INTEGER NOT NULL UNIQUE,
    freq_mhz REAL GENERATED ALWAYS AS (freq_hz / 1000000.0) STORED,
    bandwidth_hz INTEGER,
    mode TEXT,                    -- 'analog_fm', 'p25', 'dmr', 'nxdn', 'unknown'
    ctcss_tone REAL,             -- PL tone Hz if analog
    label TEXT,                  -- user or FCC-derived label
    fcc_callsign TEXT,
    fcc_licensee TEXT,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    observation_count INTEGER DEFAULT 1,
    avg_power_db REAL,
    peak_power_db REAL,
    activity_score REAL DEFAULT 0,  -- learned: 0-1 how active
    busy_hours TEXT,                 -- JSON array of busy hour indices
    classification_confidence REAL DEFAULT 0,
    enabled BOOLEAN DEFAULT 1,      -- user can disable scanning this freq
    notes TEXT
);

CREATE TABLE IF NOT EXISTS recordings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    freq_id INTEGER REFERENCES frequencies(id),
    freq_hz INTEGER NOT NULL,
    filepath TEXT NOT NULL,
    recorded_at REAL NOT NULL,
    duration_s REAL,
    size_bytes INTEGER,
    vad_confidence REAL,
    energy_db REAL,
    transcribed BOOLEAN DEFAULT 0,
    transcript TEXT,
    transcript_confidence REAL,
    keywords TEXT                 -- comma-separated extracted keywords
);

CREATE TABLE IF NOT EXISTS survey_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    freq_hz INTEGER NOT NULL,
    power_db REAL NOT NULL,
    noise_floor_db REAL
);

CREATE TABLE IF NOT EXISTS noise_floor (
    freq_hz INTEGER PRIMARY KEY,
    avg_power_db REAL NOT NULL,
    std_dev_db REAL NOT NULL,
    sample_count INTEGER DEFAULT 0,
    last_updated REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    event_type TEXT NOT NULL,     -- 'signal_detected', 'recording_started', 'classified', 'transcribed'
    freq_hz INTEGER,
    detail TEXT                   -- JSON blob
);

CREATE INDEX IF NOT EXISTS idx_recordings_freq ON recordings(freq_id);
CREATE INDEX IF NOT EXISTS idx_recordings_time ON recordings(recorded_at);
CREATE INDEX IF NOT EXISTS idx_recordings_transcript ON recordings(transcript) WHERE transcript IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_survey_time ON survey_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_survey_freq ON survey_snapshots(freq_hz);
CREATE INDEX IF NOT EXISTS idx_activity_time ON activity_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_freq_mode ON frequencies(mode);
CREATE INDEX IF NOT EXISTS idx_freq_active ON frequencies(activity_score DESC);
"""


class ScanPiDB:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def connect(self):
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    @contextmanager
    def cursor(self):
        c = self._conn.cursor()
        try:
            yield c
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # --- Frequency Catalog ---

    def upsert_frequency(self, freq_hz: int, power_db: float,
                         bandwidth_hz: int | None = None,
                         mode: str | None = None) -> int:
        now = time.time()
        with self.cursor() as c:
            c.execute("""
                INSERT INTO frequencies (freq_hz, avg_power_db, peak_power_db,
                                         bandwidth_hz, mode, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(freq_hz) DO UPDATE SET
                    last_seen = ?,
                    observation_count = observation_count + 1,
                    avg_power_db = (avg_power_db * observation_count + ?) / (observation_count + 1),
                    peak_power_db = MAX(peak_power_db, ?),
                    bandwidth_hz = COALESCE(?, bandwidth_hz),
                    mode = COALESCE(?, mode)
            """, (freq_hz, power_db, power_db, bandwidth_hz, mode, now, now,
                  now, power_db, power_db, bandwidth_hz, mode))
            c.execute("SELECT id FROM frequencies WHERE freq_hz = ?", (freq_hz,))
            return c.fetchone()[0]

    def get_frequencies(self, enabled_only: bool = False,
                        mode: str | None = None,
                        min_score: float = 0) -> list[dict]:
        q = "SELECT * FROM frequencies WHERE 1=1"
        params = []
        if enabled_only:
            q += " AND enabled = 1"
        if mode:
            q += " AND mode = ?"
            params.append(mode)
        if min_score > 0:
            q += " AND activity_score >= ?"
            params.append(min_score)
        q += " ORDER BY activity_score DESC, last_seen DESC"
        with self.cursor() as c:
            c.execute(q, params)
            return [dict(r) for r in c.fetchall()]

    def get_scan_queue(self, limit: int = 50) -> list[dict]:
        """Get frequencies to scan, prioritized by activity + recency."""
        with self.cursor() as c:
            c.execute("""
                SELECT * FROM frequencies
                WHERE enabled = 1
                ORDER BY activity_score DESC, last_seen DESC
                LIMIT ?
            """, (limit,))
            return [dict(r) for r in c.fetchall()]

    def update_activity_score(self, freq_id: int, score: float):
        with self.cursor() as c:
            c.execute("UPDATE frequencies SET activity_score = ? WHERE id = ?",
                      (score, freq_id))

    def classify_frequency(self, freq_hz: int, mode: str,
                           confidence: float, bandwidth_hz: int | None = None,
                           ctcss_tone: float | None = None):
        with self.cursor() as c:
            c.execute("""
                UPDATE frequencies SET
                    mode = ?, classification_confidence = ?,
                    bandwidth_hz = COALESCE(?, bandwidth_hz),
                    ctcss_tone = COALESCE(?, ctcss_tone)
                WHERE freq_hz = ?
            """, (mode, confidence, bandwidth_hz, ctcss_tone, freq_hz))

    def label_frequency(self, freq_hz: int, label: str):
        with self.cursor() as c:
            c.execute("UPDATE frequencies SET label = ? WHERE freq_hz = ?",
                      (label, freq_hz))

    # --- Noise Floor ---

    def update_noise_floor(self, freq_hz: int, power_db: float):
        now = time.time()
        with self.cursor() as c:
            c.execute("""
                INSERT INTO noise_floor (freq_hz, avg_power_db, std_dev_db, sample_count, last_updated)
                VALUES (?, ?, 0, 1, ?)
                ON CONFLICT(freq_hz) DO UPDATE SET
                    avg_power_db = (avg_power_db * sample_count + ?) / (sample_count + 1),
                    sample_count = sample_count + 1,
                    last_updated = ?
            """, (freq_hz, power_db, now, power_db, now))

    def get_noise_floor(self, freq_hz: int) -> float | None:
        with self.cursor() as c:
            c.execute("SELECT avg_power_db FROM noise_floor WHERE freq_hz = ?", (freq_hz,))
            row = c.fetchone()
            return row[0] if row else None

    # --- Recordings ---

    def add_recording(self, freq_id: int, freq_hz: int, filepath: str,
                      duration_s: float, size_bytes: int,
                      vad_confidence: float = 0, energy_db: float = 0) -> int:
        with self.cursor() as c:
            c.execute("""
                INSERT INTO recordings (freq_id, freq_hz, filepath, recorded_at,
                                        duration_s, size_bytes, vad_confidence, energy_db)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (freq_id, freq_hz, filepath, time.time(),
                  duration_s, size_bytes, vad_confidence, energy_db))
            return c.lastrowid

    def get_recordings(self, freq_id: int | None = None,
                       limit: int = 50, offset: int = 0,
                       search: str | None = None) -> list[dict]:
        q = "SELECT r.*, f.label, f.mode, f.freq_mhz FROM recordings r LEFT JOIN frequencies f ON r.freq_id = f.id WHERE 1=1"
        params = []
        if freq_id is not None:
            q += " AND r.freq_id = ?"
            params.append(freq_id)
        if search:
            q += " AND r.transcript LIKE ?"
            params.append(f"%{search}%")
        q += " ORDER BY r.recorded_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self.cursor() as c:
            c.execute(q, params)
            return [dict(r) for r in c.fetchall()]

    def set_transcript(self, recording_id: int, transcript: str,
                       confidence: float, keywords: str = ""):
        with self.cursor() as c:
            c.execute("""
                UPDATE recordings SET
                    transcribed = 1, transcript = ?,
                    transcript_confidence = ?, keywords = ?
                WHERE id = ?
            """, (transcript, confidence, keywords, recording_id))

    def get_untranscribed(self, limit: int = 10) -> list[dict]:
        with self.cursor() as c:
            c.execute("""
                SELECT r.*, f.label, f.mode FROM recordings r
                LEFT JOIN frequencies f ON r.freq_id = f.id
                WHERE r.transcribed = 0 AND r.duration_s > 1.0
                ORDER BY r.recorded_at DESC LIMIT ?
            """, (limit,))
            return [dict(r) for r in c.fetchall()]

    # --- Activity Log ---

    def log_event(self, event_type: str, freq_hz: int | None = None,
                  detail: str | None = None):
        with self.cursor() as c:
            c.execute("""
                INSERT INTO activity_log (timestamp, event_type, freq_hz, detail)
                VALUES (?, ?, ?, ?)
            """, (time.time(), event_type, freq_hz, detail))

    # --- Stats ---

    def get_stats(self) -> dict:
        with self.cursor() as c:
            stats = {}
            c.execute("SELECT COUNT(*) FROM frequencies")
            stats["total_frequencies"] = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM frequencies WHERE mode IS NOT NULL")
            stats["classified"] = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM recordings")
            stats["total_recordings"] = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM recordings WHERE transcribed = 1")
            stats["transcribed"] = c.fetchone()[0]
            c.execute("SELECT COALESCE(SUM(size_bytes), 0) FROM recordings")
            stats["storage_bytes"] = c.fetchone()[0]
            c.execute("SELECT COUNT(DISTINCT freq_hz) FROM recordings WHERE recorded_at > ?",
                      (time.time() - 3600,))
            stats["active_last_hour"] = c.fetchone()[0]
            return stats
