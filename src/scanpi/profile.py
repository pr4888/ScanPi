"""ScanPi profile + feature-flag system.

Two distributions share one codebase:
  - lite: Pi 5 / Pi 4 / ARM SBC. Conservative defaults. Heavy stuff opt-in.
  - full: Ubuntu x86_64. Everything on by default.

Profile resolution order (first match wins):
  1. explicit path passed to load_profile()
  2. SCANPI_PROFILE env var (path or short name "lite"/"full")
  3. ~/scanpi/profile.toml
  4. autodetect: x86_64 + >=8 cores + >=16GB RAM -> full, else lite
  5. fall back to bundled lite.toml

Tools query feature_enabled() rather than reading TOML directly. This keeps
profile keys in one place and makes "your CPU, your funeral" opt-ins explicit.
"""
from __future__ import annotations

import logging
import os
import platform
import shutil
from pathlib import Path
from typing import Any

try:
    import tomllib  # py 3.11+
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

log = logging.getLogger(__name__)

_PROFILE: dict[str, Any] | None = None
_PROFILE_PATH: Path | None = None

# Defaults if no profile loads. Conservative — the lite values.
_DEFAULTS: dict[str, Any] = {
    "profile": {"name": "fallback", "target": "unknown"},
    "transcription": {
        "enabled": True,
        "mode": "single-source",
        "model": "tiny.en",
        "concurrent_streams": 1,
        "active_target": "op25",   # which source receives real-time transcription
    },
    "search": {
        "fts5": True,
        "semantic": False,           # OPT-IN on lite
        "embedding_model": "bge-small-en-v1.5",
        "embed_backfill_max": 5000,
    },
    "alerts": {
        "mqtt_enabled": True,
        "mqtt_url": "mqtt://localhost:1883",
        "watchlist_path": "~/scanpi/watchlist.yaml",
    },
    "geo": {
        "external_geocoder": True,
        "viewbox": [-72.50, 41.18, -71.85, 41.50],   # SE Connecticut
        "default_center": [41.3501, -72.0787],
        "pin_ttl_minutes": 5,
    },
    "recording": {
        "audio_archive": "squelched-flac",
        "iq_archive": False,                # OPT-IN on lite
        "iq_ring_seconds": 0,               # 60 on full, 0 on lite
    },
    "hackrf": {
        "enabled": True,
        "default_sample_rate": 8_000_000,
        "default_num_chans": 32,
    },
    "experimental": {
        "trunk_recorder": False,
        "cross_channel_correlation": False,
        "speaker_diarization": False,
        "auto_freq_discovery": False,
        "anomaly_detection": False,
    },
    "tailscale": {
        "serve_https": True,
        "funnel": False,                    # OPT-IN — needs auth in front
    },
}


def _autodetect_target() -> str:
    """Best-guess between 'lite' and 'full' based on hardware."""
    arch = platform.machine().lower()
    is_x86 = arch in ("x86_64", "amd64")
    cpu_count = os.cpu_count() or 1
    try:
        # Linux-only; on Windows dev boxes we don't autodetect 'full'.
        with open("/proc/meminfo") as fh:
            mem_kb = next(int(l.split()[1]) for l in fh if l.startswith("MemTotal:"))
        ram_gb = mem_kb / 1024 / 1024
    except OSError:
        ram_gb = 0
    if is_x86 and cpu_count >= 8 and ram_gb >= 16:
        return "full"
    return "lite"


def _bundled_profile_path(name: str) -> Path:
    """Path to a bundled profile shipped with the package."""
    here = Path(__file__).resolve().parent.parent.parent
    return here / "profiles" / f"{name}.toml"


def _deep_merge(base: dict, override: dict) -> dict:
    """Override base with override; nested dicts are merged, not replaced."""
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_profile(path: str | Path | None = None, *, autosave: bool = True) -> dict[str, Any]:
    """Load a profile, set as the active profile, return it.

    On first call the resolved profile is also written to ~/scanpi/profile.toml
    (for visibility/editing) unless `autosave=False`.
    """
    global _PROFILE, _PROFILE_PATH

    candidates: list[Path] = []
    if path:
        candidates.append(Path(path).expanduser())
    env = os.environ.get("SCANPI_PROFILE")
    if env:
        if env in ("lite", "full"):
            candidates.append(_bundled_profile_path(env))
        else:
            candidates.append(Path(env).expanduser())
    candidates.append(Path("~/scanpi/profile.toml").expanduser())

    chosen: Path | None = None
    for c in candidates:
        if c.exists():
            chosen = c
            break

    if chosen is None:
        target = _autodetect_target()
        bundled = _bundled_profile_path(target)
        if bundled.exists():
            chosen = bundled
            log.info("autodetected profile: %s (-> %s)", target, bundled)
        else:
            log.warning("no profile found; using built-in defaults")
            _PROFILE = _DEFAULTS.copy()
            _PROFILE_PATH = None
            return _PROFILE

    with open(chosen, "rb") as fh:
        loaded = tomllib.load(fh)
    merged = _deep_merge(_DEFAULTS, loaded)
    _PROFILE = merged
    _PROFILE_PATH = chosen
    log.info("loaded profile %r from %s", merged.get("profile", {}).get("name", "?"), chosen)

    if autosave:
        user_copy = Path("~/scanpi/profile.toml").expanduser()
        try:
            if not user_copy.exists() and chosen != user_copy:
                user_copy.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(chosen, user_copy)
                log.info("seeded user profile: %s", user_copy)
        except OSError as e:
            log.debug("autosave skipped: %s", e)

    return merged


def get_profile() -> dict[str, Any]:
    """Return the current profile, loading defaults if not yet initialized."""
    if _PROFILE is None:
        return load_profile()
    return _PROFILE


def feature_enabled(key: str, *, default: bool = False) -> bool:
    """Top-level feature switch lookup.

    Recognizes common key names regardless of which TOML section they live in.
    Env override: SCANPI_FEATURE_<UPPERCASE_KEY>=1 forces on, =0 forces off.
    """
    env = os.environ.get(f"SCANPI_FEATURE_{key.upper()}")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")

    p = get_profile()

    # Explicit feature aliases (most common queries)
    aliases = {
        "semantic_search": ("search", "semantic"),
        "fts5_search": ("search", "fts5"),
        "mqtt_alerts": ("alerts", "mqtt_enabled"),
        "external_geocoder": ("geo", "external_geocoder"),
        "iq_archive": ("recording", "iq_archive"),
        "trunk_recorder": ("experimental", "trunk_recorder"),
        "cross_channel_correlation": ("experimental", "cross_channel_correlation"),
        "speaker_diarization": ("experimental", "speaker_diarization"),
        "auto_freq_discovery": ("experimental", "auto_freq_discovery"),
        "anomaly_detection": ("experimental", "anomaly_detection"),
        "multi_stream_transcription": ("transcription", "concurrent_streams"),
        "multi_band_hackrf": ("hackrf", "enabled"),
        "tailscale_funnel": ("tailscale", "funnel"),
    }
    if key in aliases:
        section, field = aliases[key]
        val = p.get(section, {}).get(field, default)
        if isinstance(val, int) and not isinstance(val, bool):
            return val > 1 if key == "multi_stream_transcription" else val > 0
        return bool(val)

    # Generic dotted lookup: "search.semantic" → p["search"]["semantic"]
    if "." in key:
        section, field = key.split(".", 1)
        return bool(p.get(section, {}).get(field, default))

    # Last resort: top-level boolean lookup
    return bool(p.get(key, default))


def get(key: str, default: Any = None) -> Any:
    """Dotted-path config getter. e.g. get('transcription.model')."""
    env_key = "SCANPI_" + key.upper().replace(".", "_")
    if env_key in os.environ:
        return os.environ[env_key]
    p = get_profile()
    parts = key.split(".")
    cur: Any = p
    for part in parts:
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def active_transcription_target() -> str:
    """Which source currently gets real-time transcription.
    Returns: 'gmrs', 'op25', 'hackrf:<channel>', 'all', or 'none'.
    """
    return str(get("transcription.active_target", "op25"))


def set_active_transcription_target(target: str) -> None:
    """Update the active target at runtime. Does NOT write to disk —
    the value is held in the in-memory profile only. Use the API endpoint
    to persist if desired.
    """
    p = get_profile()
    p.setdefault("transcription", {})["active_target"] = target
    log.info("transcription target set to: %s", target)
