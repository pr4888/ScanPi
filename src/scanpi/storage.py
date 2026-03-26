"""Storage management — retention, disk monitoring, USB auto-mount."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from .config import ScanConfig
from .db import ScanPiDB

log = logging.getLogger("scanpi.storage")


class StorageManager:
    """Manages recordings storage, retention, and USB expansion."""

    def __init__(self, cfg: ScanConfig, db: ScanPiDB):
        self.cfg = cfg
        self.db = db
        self._overflow_path: Path | None = None

    def get_usage(self) -> dict:
        """Get storage usage info."""
        primary = self.cfg.recordings_dir
        usage = shutil.disk_usage(str(primary))

        result = {
            "primary_path": str(primary),
            "primary_total_gb": usage.total / (1024 ** 3),
            "primary_used_gb": usage.used / (1024 ** 3),
            "primary_free_gb": usage.free / (1024 ** 3),
            "primary_pct": (usage.used / usage.total) * 100 if usage.total > 0 else 0,
            "recordings_count": 0,
            "recordings_size_gb": 0,
            "overflow_mounted": False,
            "overflow_path": None,
            "overflow_free_gb": 0,
        }

        # Count recordings
        stats = self.db.get_stats()
        result["recordings_count"] = stats["total_recordings"]
        result["recordings_size_gb"] = stats["storage_bytes"] / (1024 ** 3)

        # Check overflow/USB mount
        overflow = self._find_overflow()
        if overflow:
            ov_usage = shutil.disk_usage(str(overflow))
            result["overflow_mounted"] = True
            result["overflow_path"] = str(overflow)
            result["overflow_free_gb"] = ov_usage.free / (1024 ** 3)

        return result

    def _find_overflow(self) -> Path | None:
        """Find mounted USB/external storage."""
        candidates = [
            Path("/mnt/scanpi"),
            Path("/media") / os.environ.get("USER", "pi"),
        ]

        for base in candidates:
            if base.is_dir():
                # Check if it's a mount point with actual storage
                if base == Path("/mnt/scanpi") and base.is_mount():
                    return base
                # Check /media/user/* for USB drives
                if base.parent == Path("/media"):
                    for sub in base.iterdir():
                        if sub.is_dir() and sub.is_mount():
                            scanpi_dir = sub / "scanpi"
                            scanpi_dir.mkdir(exist_ok=True)
                            return scanpi_dir

        # If overflow_dir is configured and exists
        if self.cfg.overflow_dir and self.cfg.overflow_dir.is_dir():
            return self.cfg.overflow_dir

        return None

    def enforce_retention(self):
        """Delete recordings older than retention period."""
        cutoff = time.time() - (self.cfg.retention_days * 86400)
        recordings = self.db.get_recordings(limit=10000)

        deleted = 0
        for rec in recordings:
            if rec["recorded_at"] < cutoff:
                filepath = Path(rec["filepath"])
                if filepath.exists():
                    filepath.unlink()
                    deleted += 1

        if deleted > 0:
            log.info(f"Retention cleanup: deleted {deleted} recordings older than {self.cfg.retention_days} days")

        # Also clean DB entries for missing files
        for rec in recordings:
            if not Path(rec["filepath"]).exists():
                # Leave in DB for transcript history but mark filepath as gone
                pass

    def enforce_capacity(self):
        """If storage exceeds max_storage_gb, move old recordings to overflow or delete."""
        stats = self.db.get_stats()
        used_gb = stats["storage_bytes"] / (1024 ** 3)

        if used_gb <= self.cfg.max_storage_gb:
            return

        overflow = self._find_overflow()
        recordings = self.db.get_recordings(limit=10000)
        # Sort oldest first
        oldest = sorted(recordings, key=lambda r: r["recorded_at"])

        moved = 0
        for rec in oldest:
            if used_gb <= self.cfg.max_storage_gb * 0.8:  # shrink to 80%
                break
            filepath = Path(rec["filepath"])
            if not filepath.exists():
                continue

            if overflow:
                # Move to overflow
                dest = overflow / filepath.name
                shutil.move(str(filepath), str(dest))
                log.info(f"Moved to overflow: {filepath.name}")
            else:
                # No overflow — delete oldest
                filepath.unlink()
                log.info(f"Capacity cleanup: deleted {filepath.name}")

            used_gb -= rec["size_bytes"] / (1024 ** 3)
            moved += 1

        if moved > 0:
            log.info(f"Capacity enforcement: processed {moved} recordings")

    def auto_mount_usb(self) -> bool:
        """Attempt to auto-mount USB drive to /mnt/scanpi."""
        if not self.cfg.auto_mount_usb:
            return False

        mnt = Path("/mnt/scanpi")
        if mnt.is_mount():
            return True

        # Find unmounted USB block devices
        try:
            result = subprocess.run(
                ["lsblk", "-rno", "NAME,TYPE,MOUNTPOINT"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().split("\n"):
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "part":
                    if len(parts) == 2:  # unmounted partition
                        dev = f"/dev/{parts[0]}"
                        mnt.mkdir(parents=True, exist_ok=True)
                        mount_result = subprocess.run(
                            ["sudo", "mount", dev, str(mnt)],
                            capture_output=True, timeout=10,
                        )
                        if mount_result.returncode == 0:
                            log.info(f"Auto-mounted {dev} to {mnt}")
                            return True
        except Exception as e:
            log.debug(f"Auto-mount attempt: {e}")

        return False

    def maintenance(self):
        """Run all storage maintenance tasks."""
        self.auto_mount_usb()
        self.enforce_retention()
        self.enforce_capacity()
