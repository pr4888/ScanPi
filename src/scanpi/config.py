"""Configuration management — loads config.toml + sensible defaults."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DATA_DIR = Path.home() / "scanpi"
DEFAULT_CONFIG = DEFAULT_DATA_DIR / "config.toml"


@dataclass
class BandRange:
    name: str
    start_mhz: float
    end_mhz: float
    enabled: bool = True


@dataclass
class ScanConfig:
    # Paths
    data_dir: Path = field(default_factory=lambda: DEFAULT_DATA_DIR)
    db_path: Path | None = None  # defaults to data_dir/scanpi.db
    recordings_dir: Path | None = None  # defaults to data_dir/recordings
    overflow_dir: Path | None = None  # /mnt/scanpi if mounted

    # SDR
    sdr_device: int = 0
    sdr_gain: str = "auto"
    sdr_ppm: int = 0
    sample_rate: int = 2_400_000

    # Survey
    survey_bands: list[BandRange] = field(default_factory=lambda: [
        BandRange("VHF-Lo", 30, 50),
        BandRange("VHF-Hi", 138, 174),
        BandRange("UHF", 400, 475),
        BandRange("800MHz", 806, 870),
        BandRange("900MHz", 896, 940),
    ])
    survey_interval_min: int = 60  # re-survey every N minutes
    noise_floor_days: int = 2  # days to build baseline
    detection_threshold_db: float = 8.0  # above noise floor

    # Scanner
    dwell_time_s: float = 5.0  # default dwell per frequency
    adaptive_dwell: bool = True  # adjust based on activity
    scan_ratio: float = 0.8  # 80% scan, 20% survey
    squelch_level: int = 0  # 0 = auto

    # Recording
    vad_enabled: bool = True
    vad_threshold: float = 0.5
    energy_threshold_db: float = -35.0
    max_recording_s: int = 300  # 5 min max per recording
    min_recording_s: float = 0.5  # ignore < 0.5s

    # Transcription
    transcribe_enabled: bool = True
    transcribe_on_idle: bool = True  # queue for idle CPU
    transcribe_model: str = "tiny.en"  # whisper.cpp model
    transcribe_threads: int = 2

    # Storage
    retention_days: int = 30
    max_storage_gb: float = 32.0
    auto_mount_usb: bool = True

    # Web
    host: str = "0.0.0.0"
    port: int = 8080

    def __post_init__(self):
        if self.db_path is None:
            self.db_path = self.data_dir / "scanpi.db"
        if self.recordings_dir is None:
            self.recordings_dir = self.data_dir / "recordings"

    @classmethod
    def load(cls, path: Path | None = None) -> ScanConfig:
        """Load config from TOML file, falling back to defaults."""
        cfg = cls()
        path = path or DEFAULT_CONFIG
        if path.exists():
            with open(path, "rb") as f:
                data = tomllib.load(f)
            _apply_toml(cfg, data)
        # Ensure directories exist
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        cfg.recordings_dir.mkdir(parents=True, exist_ok=True)
        return cfg

    def save(self, path: Path | None = None):
        """Save current config to TOML."""
        path = path or DEFAULT_CONFIG
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# ScanPi Configuration",
            "# Edit this file or use the web UI settings page",
            "",
            "[sdr]",
            f'device = {self.sdr_device}',
            f'gain = "{self.sdr_gain}"',
            f'ppm = {self.sdr_ppm}',
            "",
            "[survey]",
            f'interval_min = {self.survey_interval_min}',
            f'detection_threshold_db = {self.detection_threshold_db}',
            "",
            "[scanner]",
            f'dwell_time_s = {self.dwell_time_s}',
            f'adaptive_dwell = {str(self.adaptive_dwell).lower()}',
            "",
            "[recording]",
            f'vad_enabled = {str(self.vad_enabled).lower()}',
            f'vad_threshold = {self.vad_threshold}',
            f'energy_threshold_db = {self.energy_threshold_db}',
            "",
            "[transcription]",
            f'enabled = {str(self.transcribe_enabled).lower()}',
            f'model = "{self.transcribe_model}"',
            "",
            "[storage]",
            f'retention_days = {self.retention_days}',
            f'max_storage_gb = {self.max_storage_gb}',
            f'auto_mount_usb = {str(self.auto_mount_usb).lower()}',
            "",
            "[web]",
            f'host = "{self.host}"',
            f'port = {self.port}',
            "",
            "# Band ranges for survey",
            "[[survey.bands]]",
        ]
        for band in self.survey_bands:
            lines.extend([
                f'[[survey.bands]]',
                f'name = "{band.name}"',
                f'start_mhz = {band.start_mhz}',
                f'end_mhz = {band.end_mhz}',
                f'enabled = {str(band.enabled).lower()}',
                "",
            ])
        path.write_text("\n".join(lines))


def _apply_toml(cfg: ScanConfig, data: dict):
    """Apply TOML dict values onto config dataclass."""
    sdr = data.get("sdr", {})
    if "device" in sdr:
        cfg.sdr_device = sdr["device"]
    if "gain" in sdr:
        cfg.sdr_gain = str(sdr["gain"])
    if "ppm" in sdr:
        cfg.sdr_ppm = sdr["ppm"]

    survey = data.get("survey", {})
    if "interval_min" in survey:
        cfg.survey_interval_min = survey["interval_min"]
    if "detection_threshold_db" in survey:
        cfg.detection_threshold_db = survey["detection_threshold_db"]

    scanner = data.get("scanner", {})
    if "dwell_time_s" in scanner:
        cfg.dwell_time_s = scanner["dwell_time_s"]
    if "adaptive_dwell" in scanner:
        cfg.adaptive_dwell = scanner["adaptive_dwell"]

    rec = data.get("recording", {})
    if "vad_enabled" in rec:
        cfg.vad_enabled = rec["vad_enabled"]
    if "vad_threshold" in rec:
        cfg.vad_threshold = rec["vad_threshold"]
    if "energy_threshold_db" in rec:
        cfg.energy_threshold_db = rec["energy_threshold_db"]

    tx = data.get("transcription", {})
    if "enabled" in tx:
        cfg.transcribe_enabled = tx["enabled"]
    if "model" in tx:
        cfg.transcribe_model = tx["model"]

    stor = data.get("storage", {})
    if "retention_days" in stor:
        cfg.retention_days = stor["retention_days"]
    if "max_storage_gb" in stor:
        cfg.max_storage_gb = stor["max_storage_gb"]

    web = data.get("web", {})
    if "host" in web:
        cfg.host = web["host"]
    if "port" in web:
        cfg.port = web["port"]

    # Band ranges
    bands = survey.get("bands", [])
    if bands:
        cfg.survey_bands = [
            BandRange(
                name=b["name"],
                start_mhz=b["start_mhz"],
                end_mhz=b["end_mhz"],
                enabled=b.get("enabled", True),
            )
            for b in bands
        ]
