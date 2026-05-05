"""Geocoder for ScanPi GEO tool.

Two-tier resolution:
  1. Local gazetteer (instant, free, definitive for known places).
  2. Cache lookup in geo.db.cache (any prior Nominatim hit).
  3. Nominatim live request (rate-limited 1 req/sec, viewbox-biased to SE CT).

The `local` source is reserved for a future Photon/Pelias integration —
gated behind `feature_enabled("local_geocoder")`. We expose the hook but
do not ship a Photon client.

If `requests` (and stdlib `urllib`) both fail at import time we silently
degrade to cache-only.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db import GeoDB
    from .extractor import Candidate

log = logging.getLogger(__name__)


NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "ScanPi/0.4.0 (+https://github.com/pr4888/ScanPi)"

# Southeast Connecticut viewbox: lat 41.18-41.50, lon -72.50 to -71.85.
# Nominatim wants min_lon, max_lat, max_lon, min_lat (left,top,right,bottom).
VIEWBOX = (-72.50, 41.50, -71.85, 41.18)

# Floor on Nominatim spacing — TOS demands max 1 req/sec.
MIN_REQ_INTERVAL_S = 1.05


@dataclass
class GeocodeResult:
    display_name: str
    lat: float
    lon: float
    kind: str                  # street | town | route | intersection | landmark
    confidence: float          # 0.0 - 1.0
    source: str                # cache | nominatim | local | gazetteer

    def to_dict(self) -> dict:
        return {
            "display_name": self.display_name,
            "lat": self.lat,
            "lon": self.lon,
            "kind": self.kind,
            "confidence": self.confidence,
            "source": self.source,
        }


class _RateLimiter:
    """Lazy single-thread token gate, ≥ MIN_REQ_INTERVAL_S between calls."""
    def __init__(self, interval_s: float = MIN_REQ_INTERVAL_S):
        self._interval = interval_s
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.time()
            elapsed = now - self._last
            if elapsed < self._interval:
                time.sleep(self._interval - elapsed)
            self._last = time.time()


class Geocoder:
    """Resolve `Candidate` objects to lat/lon using gazetteer + cache + Nominatim."""

    def __init__(self, db: "GeoDB", *, viewbox=VIEWBOX,
                 user_agent: str = USER_AGENT,
                 enable_external: bool = True,
                 enable_local: bool = False):
        self._db = db
        self._viewbox = viewbox
        self._ua = user_agent
        self._enable_external = enable_external
        self._enable_local = enable_local
        self._rate = _RateLimiter()

    # --- public API -----------------------------------------------------

    def resolve(self, candidate: "Candidate") -> GeocodeResult | None:
        """Resolve one extractor candidate. Returns None if nothing landed."""
        # 1. Towns and routes — try gazetteer first; gazetteer wins for towns.
        if candidate.kind == "town":
            place = self._db.find_place(candidate.name, kind="town")
            if place:
                return GeocodeResult(
                    display_name=place["name"],
                    lat=place["lat"], lon=place["lon"],
                    kind="town", confidence=0.9,
                    source="gazetteer",
                )
            return self._geocode_external(
                candidate.name + ", CT",
                kind="town",
                base_confidence=candidate.confidence_hint,
            )

        if candidate.kind == "route":
            # Try gazetteer first — towns + routes are seeded.
            town = candidate.town
            place = self._db.find_place(candidate.name, town=town, kind="route")
            if not place:
                place = self._db.find_place(candidate.name, kind="route")
            if place:
                return GeocodeResult(
                    display_name=f"{place['name']}"
                                + (f", {place['town']}" if place["town"] else ""),
                    lat=place["lat"], lon=place["lon"],
                    kind="route",
                    confidence=0.7 if not town else 0.8,
                    source="gazetteer",
                )
            query = candidate.name + (f", {town}, CT" if town else ", CT")
            return self._geocode_external(query, kind="route",
                                           base_confidence=candidate.confidence_hint)

        if candidate.kind == "street":
            town = candidate.town
            # Town-scoped lookup first (only succeeds with a real match in
            # that town). If no town context, allow any-town gazetteer hit.
            place = None
            if town:
                place = self._db.find_place(candidate.name, town=town, kind="street")
            else:
                place = self._db.find_place(candidate.name, kind="street")
            if place:
                return GeocodeResult(
                    display_name=f"{place['name']}"
                                + (f", {place['town']}" if place["town"] else ""),
                    lat=place["lat"], lon=place["lon"],
                    kind="street",
                    confidence=0.7 if not town else 0.85,
                    source="gazetteer",
                )
            # Build best-effort Nominatim query — include number + town if present.
            parts: list[str] = []
            if candidate.number:
                parts.append(candidate.number)
            parts.append(candidate.name)
            if town:
                parts.append(town)
            parts.append("CT")
            return self._geocode_external(", ".join(parts), kind="street",
                                           base_confidence=candidate.confidence_hint)

        if candidate.kind == "intersection":
            town = candidate.town
            # If both sides exist in gazetteer, use midpoint.
            a = self._db.find_place(candidate.street, town=town)
            b = self._db.find_place(candidate.cross_street, town=town)
            if a and b:
                return GeocodeResult(
                    display_name=f"{a['name']} & {b['name']}"
                                + (f", {town}" if town else ""),
                    lat=(a["lat"] + b["lat"]) / 2.0,
                    lon=(a["lon"] + b["lon"]) / 2.0,
                    kind="intersection",
                    confidence=0.6,    # midpoint is approximate
                    source="gazetteer",
                )
            query = (
                f"{candidate.street} and {candidate.cross_street}"
                + (f", {town}" if town else "")
                + ", CT"
            )
            return self._geocode_external(query, kind="intersection",
                                           base_confidence=candidate.confidence_hint)

        if candidate.kind == "landmark":
            place = self._db.find_place(candidate.name, kind="landmark")
            if place:
                return GeocodeResult(
                    display_name=place["name"],
                    lat=place["lat"], lon=place["lon"],
                    kind="landmark", confidence=0.85,
                    source="gazetteer",
                )
            return self._geocode_external(candidate.name + ", CT",
                                           kind="landmark",
                                           base_confidence=candidate.confidence_hint)

        return None

    # --- external -------------------------------------------------------

    def _geocode_external(self, query: str, *, kind: str,
                          base_confidence: float) -> GeocodeResult | None:
        """Cache-then-Nominatim. Returns None if both fail."""
        # Cache hit?
        cached = self._db.cache_get(query)
        if cached and cached.get("payload"):
            res = self._first_payload_result(cached["payload"])
            if res:
                return GeocodeResult(
                    display_name=res["display_name"],
                    lat=res["lat"], lon=res["lon"],
                    kind=kind,
                    confidence=min(0.85, base_confidence + 0.1),
                    source="cache",
                )

        # Local Photon/Pelias — feature-flagged hook only.
        if self._enable_local:
            local = self._maybe_local_lookup(query)
            if local:
                return local

        if not self._enable_external:
            return None

        # Live Nominatim. Rate-limited.
        try:
            payload = self._nominatim(query)
        except Exception:
            log.exception("nominatim lookup failed for %r", query)
            return None
        # Cache regardless (even an empty list — saves a future hit).
        try:
            self._db.cache_put(query, payload)
        except Exception:
            log.exception("cache_put failed for %r", query)
        res = self._first_payload_result(payload)
        if not res:
            return None
        return GeocodeResult(
            display_name=res["display_name"],
            lat=res["lat"], lon=res["lon"],
            kind=kind,
            confidence=base_confidence,
            source="nominatim",
        )

    def _maybe_local_lookup(self, query: str) -> GeocodeResult | None:
        """Stub for a future local geocoder (Photon / Pelias).

        Plug in by reading `feature_enabled("local_geocoder")` from
        scanpi.profile and pointing this at your Photon HTTP endpoint.
        Return a `GeocodeResult(source="local", ...)` on success.
        """
        return None

    def _first_payload_result(self, payload) -> dict | None:
        if not payload:
            return None
        if isinstance(payload, dict) and "lat" in payload:
            try:
                return {
                    "display_name": payload.get("display_name", ""),
                    "lat": float(payload["lat"]), "lon": float(payload["lon"]),
                }
            except (KeyError, TypeError, ValueError):
                return None
        if isinstance(payload, list) and payload:
            try:
                first = payload[0]
                return {
                    "display_name": first.get("display_name", ""),
                    "lat": float(first["lat"]), "lon": float(first["lon"]),
                }
            except (KeyError, TypeError, ValueError):
                return None
        return None

    def _nominatim(self, query: str) -> list[dict] | None:
        """Hit Nominatim with viewbox bias. Returns parsed JSON list or None."""
        self._rate.wait()
        params = {
            "q": query,
            "format": "json",
            "addressdetails": "1",
            "limit": "1",
            "countrycodes": "us",
            "viewbox": ",".join(str(v) for v in self._viewbox),
            "bounded": "1",
        }
        url = NOMINATIM_URL + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "User-Agent": self._ua,
            "Accept-Language": "en",
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
        except Exception:
            log.warning("nominatim request failed for %r", query)
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            log.warning("nominatim returned non-json for %r", query)
            return None
