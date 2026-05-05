"""FastAPI routes for the GEO tool.

All routes are mounted under `/tools/geo/api/<endpoint>` by app_v3.

GeoJSON output is returned as a `FeatureCollection` ready for Leaflet,
QGIS, ArcGIS, CesiumJS, or any GeoJSON-aware client.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request

if TYPE_CHECKING:
    from . import GeoTool

log = logging.getLogger(__name__)


# Crude duration parser: "5m", "30s", "24h", "2d" or a bare integer (seconds).
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_since(value: str) -> float:
    """Parse a since-string into seconds. Defaults to 5 minutes if unparseable."""
    if not value:
        return 300.0
    v = value.strip().lower()
    if v.isdigit():
        return float(v)
    try:
        if v[-1] in _UNITS:
            return float(v[:-1]) * _UNITS[v[-1]]
        return float(v)
    except (ValueError, IndexError):
        return 300.0


def _pin_to_feature(pin: dict) -> dict:
    return {
        "type": "Feature",
        "id": pin["id"],
        "geometry": {
            "type": "Point",
            "coordinates": [pin["lat"], pin["lon"]] if False else [pin["lon"], pin["lat"]],
        },
        "properties": {
            "id": pin["id"],
            "ts": pin["ts"],
            "source": pin["source"],
            "source_call_id": pin["source_call_id"],
            "channel_or_tg": pin["channel_or_tg"],
            "transcript_excerpt": pin["transcript_excerpt"],
            "label": pin["label"],
            "kind": pin["kind"],
            "confidence": pin["confidence"],
            "source_geocoder": pin["source_geocoder"],
            "expires_ts": pin["expires_ts"],
            "raw_match": pin.get("raw_match"),
        },
    }


def build_router(tool: "GeoTool") -> APIRouter:
    r = APIRouter()

    # ---- pins (live + history) ----------------------------------------

    @r.get("/pins")
    def pins_live(since: str = "5m", min_confidence: float = 0.0,
                  kind: str | None = None):
        """Live pins (default 5 min window, only-live filter)."""
        seconds = parse_since(since)
        since_ts = time.time() - seconds
        rows = tool.db.pins_since(
            since_ts=since_ts, kind=kind,
            only_live=True, min_confidence=min_confidence,
            limit=500,
        )
        return {
            "type": "FeatureCollection",
            "since_ts": since_ts,
            "now": time.time(),
            "count": len(rows),
            "features": [_pin_to_feature(p) for p in rows],
        }

    @r.get("/pins/all")
    def pins_history(since: str = "24h", until: float | None = None,
                     kind: str | None = None,
                     min_confidence: float = 0.0,
                     limit: int = 2000):
        """Historical pins — does NOT filter by live TTL."""
        seconds = parse_since(since)
        since_ts = time.time() - seconds
        rows = tool.db.pins_since(
            since_ts=since_ts, until_ts=until, kind=kind,
            only_live=False, min_confidence=min_confidence,
            limit=limit,
        )
        return {
            "type": "FeatureCollection",
            "since_ts": since_ts,
            "until_ts": until,
            "now": time.time(),
            "count": len(rows),
            "features": [_pin_to_feature(p) for p in rows],
        }

    @r.get("/pin/{pin_id}")
    def pin_detail(pin_id: int):
        pin = tool.db.get_pin(pin_id)
        if not pin:
            raise HTTPException(404, "pin not found")
        # Resolve audio URL based on origin tool
        audio_url = None
        if pin["source_call_id"] is not None:
            if pin["source"] == "gmrs":
                audio_url = f"/tools/gmrs/api/clip/{pin['source_call_id']}"
            elif pin["source"] == "op25":
                audio_url = f"/tools/op25/api/clip/{pin['source_call_id']}"
        pin["audio_url"] = audio_url
        return pin

    # ---- gazetteer ----------------------------------------------------

    @r.get("/gazetteer/search")
    def gazetteer_search(q: str = "", limit: int = 25):
        if not q.strip():
            return {"q": q, "places": []}
        return {"q": q, "places": tool.db.search_places(q, limit=limit)}

    @r.post("/gazetteer")
    async def gazetteer_add(request: Request):
        """Add a custom place to the gazetteer.

        Body: {"name": str, "kind": "town"|"street"|"route"|"landmark",
               "lat": float, "lon": float, "town": str?}.
        """
        # TODO auth — gateway should provide _check_token; not yet ported.
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid JSON body")
        for field in ("name", "kind", "lat", "lon"):
            if field not in body:
                raise HTTPException(400, f"missing field: {field}")
        try:
            place_id = tool.db.add_place(
                name=str(body["name"]),
                kind=str(body["kind"]),
                lat=float(body["lat"]),
                lon=float(body["lon"]),
                town=str(body.get("town", "")) or None,
                source="manual",
            )
        except Exception as e:
            raise HTTPException(500, f"add failed: {e}")
        return {"id": place_id, "ok": True}

    # ---- health -------------------------------------------------------

    @r.get("/geo/health")
    def health():
        return tool.health_payload()

    # Convenience alias mounted under tool prefix.
    @r.get("/health")
    def health_alias():
        return tool.health_payload()

    return r
