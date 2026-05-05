"""Profile + runtime transcription target API.

Mounted at /v1/profile/* — exposes the active profile and lets users switch
which source receives real-time transcription (single-source mode).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import profile as _profile

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/profile", tags=["profile"])


class TranscriptionTargetUpdate(BaseModel):
    target: str  # "gmrs" | "op25" | "hackrf:<channel>" | "all" | "none"


@router.get("")
def get_active_profile() -> dict:
    """Return the active profile + key feature flags."""
    p = _profile.get_profile()
    flags = {
        "fts5_search":        _profile.feature_enabled("fts5_search"),
        "semantic_search":    _profile.feature_enabled("semantic_search"),
        "mqtt_alerts":        _profile.feature_enabled("mqtt_alerts"),
        "external_geocoder":  _profile.feature_enabled("external_geocoder"),
        "iq_archive":         _profile.feature_enabled("iq_archive"),
        "trunk_recorder":     _profile.feature_enabled("trunk_recorder"),
        "multi_band_hackrf":  _profile.feature_enabled("multi_band_hackrf"),
        "tailscale_funnel":   _profile.feature_enabled("tailscale_funnel"),
    }
    return {
        "name": p.get("profile", {}).get("name"),
        "target": p.get("profile", {}).get("target"),
        "description": p.get("profile", {}).get("description"),
        "transcription": p.get("transcription", {}),
        "features": flags,
    }


@router.get("/transcription/target")
def get_transcription_target() -> dict:
    return {
        "target": _profile.active_transcription_target(),
        "options": ["gmrs", "op25", "all", "none"],
        "mode": _profile.get("transcription.mode", "single-source"),
        "model": _profile.get("transcription.model", "tiny.en"),
        "concurrent_streams": _profile.get("transcription.concurrent_streams", 1),
    }


@router.post("/transcription/target")
def set_transcription_target(update: TranscriptionTargetUpdate) -> dict:
    """Switch which source receives real-time transcription.

    On lite the recommended values are 'gmrs', 'op25', or 'none'. Setting
    to 'all' on lite is allowed but will queue whisper jobs and may starve
    the channelizer; you've been warned.
    """
    valid_prefixes = ("gmrs", "op25", "hackrf:", "all", "none")
    if not (update.target in ("gmrs", "op25", "all", "none")
            or update.target.startswith("hackrf:")):
        raise HTTPException(400, f"target must be one of {valid_prefixes}")
    _profile.set_active_transcription_target(update.target)
    return {"target": update.target, "applied": True}
