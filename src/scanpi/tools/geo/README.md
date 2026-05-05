# GEO — Geo-pinning for transcripts

Pulls street/town/route mentions out of the GMRS and OP25 transcripts that the
other ScanPi tools have already produced, geocodes them, and pins them on a
Leaflet map.

## What this tool does

1. A worker thread polls `~/scanpi/gmrs.db` and `~/scanpi/op25.db` (read-only,
   `?mode=ro`) every 15 s for new completed transcripts.
2. Each transcript runs through a regex-driven extractor that finds:
   - numbered street addresses (`123 Main Street`)
   - bare street names (matched against `streets_seed.csv` + Nominatim cache)
   - state and federal route numbers (`Route 27`, `I-95`, `Interstate 95`)
   - intersections (`intersection of X and Y`, `X at Y`)
   - towns (whole-word matches against `towns_seed.csv`)
3. Each candidate is resolved through a 3-tier geocoder:
   1. **Local gazetteer** — instant, ships with the tool, covers
      southeastern CT towns + a starter set of major roads/streets.
   2. **Cache** — every Nominatim result is cached forever in
      `geo.db.cache` keyed on the literal query string.
   3. **Nominatim live** — viewbox-biased to lat 41.18-41.50,
      lon -72.50 to -71.85, rate-limited to 1 req/sec with the
      contact User-Agent `ScanPi/0.4.0 (+https://github.com/pr4888/ScanPi)`.
4. Results land in `geo.db.pins`. Pins have an `expires_ts` (default
   5 min). The "live" view only shows un-expired pins; history retains
   them forever.

## Lite vs Full

Both profiles default `external_geocoder` ON because cache hits are free
and the rate limiter prevents abuse. The only meaningful flag is
`local_geocoder` — when ON the geocoder will defer to a clean integration
hook for a self-hosted Photon or Pelias instance. This repo ships the
hook only; bring your own backend.

## Files

| File | Purpose |
|------|---------|
| `__init__.py` | `GeoTool(Tool)` — lifecycle + worker thread |
| `extractor.py` | Regex extractor for street / town / route / intersection candidates |
| `geocoder.py` | Gazetteer → cache → Nominatim resolver |
| `db.py` | SQLite schema + helpers (`gazetteer`, `cache`, `pins`) |
| `api.py` | FastAPI APIRouter (`/pins`, `/pins/all`, `/pin/{id}`, `/gazetteer*`, `/geo/health`) |
| `page.html` | Leaflet UI: live + historical layers, time slider, dark CARTO tiles |
| `data/towns_seed.csv` | CT town centroids (Groton, NL, Stonington, Mystic, …) |
| `data/streets_seed.csv` | Starter street list (~50 entries) |

## Config keys

Pass via `GeoTool(config={...})` at registration time.

| key | default | purpose |
|-----|---------|---------|
| `data_dir` | `~/scanpi` | parent for `geo.db` and cursor files |
| `gmrs_db` | `<data_dir>/gmrs.db` | read-only source DB |
| `op25_db` | `<data_dir>/op25.db` | read-only source DB |
| `poll_interval_s` | 15 | how often to scan source DBs |
| `pin_ttl_s` | 300 | how long a pin stays "live" (5 min) |
| `excerpt_max` | 240 | char cap on transcript excerpts written into pins |
| `user_agent` | `ScanPi/0.4.0 (+https://github.com/pr4888/ScanPi)` | required by Nominatim TOS |

Profile features (read via `scanpi.profile.feature_enabled` or
`SCANPI_FEATURE_<NAME>` env var fallback):

| feature | default | effect |
|---------|---------|--------|
| `external_geocoder` | on | enables Nominatim live lookups (cache stays on either way) |
| `local_geocoder` | off | reserves a hook for future Photon / Pelias |

## Endpoints

| Route | Method | Purpose |
|-------|--------|---------|
| `/tools/geo/api/pins?since=5m&min_confidence=0.0&kind=` | GET | Live (un-expired) pins as GeoJSON |
| `/tools/geo/api/pins/all?since=24h&kind=&min_confidence=` | GET | Historical pins (any TTL) |
| `/tools/geo/api/pin/{id}` | GET | Pin detail incl. linked audio URL |
| `/tools/geo/api/gazetteer/search?q=` | GET | Substring search of gazetteer |
| `/tools/geo/api/gazetteer` | POST | Add a custom place (`{name, kind, lat, lon, town?}`) |
| `/tools/geo/api/geo/health` | GET | Counts + cursors + cache stats |

## How to add a custom place

Two paths:

1. Edit `data/streets_seed.csv` (or `towns_seed.csv`) and delete
   `~/scanpi/geo.db` so the seed re-runs on next start. Or, add via
   `INSERT INTO gazetteer ... source='manual'` and the tool will keep
   it across restarts.
2. POST to `/tools/geo/api/gazetteer` with JSON
   `{"name": "Coast Guard Academy", "kind": "landmark",
     "lat": 41.3725, "lon": -72.0995, "town": "New London"}`.

## Attribution

- Map tiles: © [OpenStreetMap](https://www.openstreetmap.org/copyright)
  contributors, © [CARTO](https://carto.com/attributions). The dark theme
  uses the `dark_all` CARTO basemap.
- Geocoding: [Nominatim](https://nominatim.openstreetmap.org/) — please
  respect the [usage policy](https://operations.osmfoundation.org/policies/nominatim/)
  if you raise the rate limit or remove the cache.
- Map library: [Leaflet](https://leafletjs.com/) 1.9.4 (BSD-2-Clause).

## Operational notes

- The worker uses cursor files (`~/scanpi/geo_cursor_gmrs.txt`,
  `~/scanpi/geo_cursor_op25.txt`) so we never reprocess after a restart.
  Delete them to force a full re-scan.
- If `requests` ever gets added as a dep we'll keep the pure stdlib
  `urllib` path alive for the Pi 5 install — no extra wheels needed.
- Confidence floor: gazetteer hits land 0.6-0.9, Nominatim hits inherit
  the extractor's prior (0.4-0.7). The UI hides anything below 0.4 when
  the "hide low-conf" toggle is on.
