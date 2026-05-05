"""Audio-retention / disk-budget manager for ScanPi tools.

Each tool instantiates one `RetentionManager` for its audio directory. A
background thread prunes clips older than `max_age_days` OR beyond a
total-size budget (`max_total_mb`), oldest-first. DB rows are NOT deleted —
only the `clip_path` column is NULLed so the metadata + transcript remain
searchable after the WAV is gone.

Named `retention` (not `storage`) to avoid collision with the v0.2 legacy
`storage.py` that handled the old Scanner tool.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


@dataclass
class RetentionConfig:
    audio_dir: Path
    max_age_days: float = 7.0
    max_total_mb: float = 1024.0   # 1 GB per tool by default
    check_interval_s: float = 900  # 15 min


class RetentionManager:
    def __init__(self, cfg: RetentionConfig, on_deleted: Callable[[list[str]], None]):
        self.cfg = cfg
        self._on_deleted = on_deleted
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_usage_mb = 0.0

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="retention-mgr", daemon=True)
        self._thread.start()
        log.info("retention started for %s (max_age=%.1fd, budget=%.0f MB)",
                 self.cfg.audio_dir, self.cfg.max_age_days, self.cfg.max_total_mb)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None

    def _run(self):
        first = True
        while not self._stop.wait(10 if first else self.cfg.check_interval_s):
            first = False
            try:
                self._rotate_once()
            except Exception:
                log.exception("rotation crashed")

    def _rotate_once(self):
        d = self.cfg.audio_dir
        if not d.exists():
            return
        files: list[tuple[float, int, Path]] = []
        for p in d.rglob("*.wav"):
            try:
                st = p.stat()
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, p))
        files.sort(key=lambda t: t[0])
        now = time.time()
        max_age_s = self.cfg.max_age_days * 86400
        max_bytes = int(self.cfg.max_total_mb * 1024 * 1024)
        total = sum(sz for _, sz, _ in files)
        self._last_usage_mb = total / 1024 / 1024
        to_delete: list[Path] = []
        for mtime, _sz, path in files:
            if (now - mtime) > max_age_s:
                to_delete.append(path)
        if total > max_bytes:
            running = total
            for _mtime, sz, path in files:
                if path in to_delete:
                    running -= sz
                    continue
                if running <= max_bytes:
                    break
                to_delete.append(path)
                running -= sz
        if not to_delete:
            return
        deleted_paths: list[str] = []
        for p in to_delete:
            try:
                p.unlink()
                deleted_paths.append(str(p))
            except Exception:
                log.exception("delete failed: %s", p)
        log.info("rotation: pruned %d files (was %.1f MB)",
                 len(deleted_paths), total / 1024 / 1024)
        try:
            self._on_deleted(deleted_paths)
        except Exception:
            log.exception("on_deleted callback failed")
        # Clean empty parent dirs
        for p in to_delete:
            try:
                parent = p.parent
                while parent != d and parent.exists():
                    if any(parent.iterdir()):
                        break
                    parent.rmdir()
                    parent = parent.parent
            except Exception:
                pass

    @property
    def last_usage_mb(self) -> float:
        return self._last_usage_mb
