# Agent GEO — Report

ScanPi v0.4.0 — `tools/geo/` package.

## What shipped

A complete `GeoTool(Tool)` package at `src/scanpi/tools/geo/` that pulls
street/town/route references out of GMRS and OP25 transcripts and pins
them on a Leaflet map.

| File | Role |
|------|------|
| `__init__.py` | `GeoTool` class, worker thread, source-DB polling, cursor persistence, integration snippet at the top of the docstring |
| `extractor.py` | Title-case-aware regex extractor for street, town, route, intersection, landmark candidates with span-overlap dedup and town-context enrichment |
| `geocoder.py` | 3-tier resolver: gazetteer → cache → Nominatim (rate-limited 1.05 s, viewbox-bounded SE CT, contact User-Agent), with `local` hook reserved behind `feature_enabled("local_geocoder")` |
| `db.py` | `geo.db` schema (`gazetteer`, `cache`, `pins`), idempotent migrations, seeding helpers |
| `api.py` | FastAPI APIRouter — `/pins`, `/pins/all`, `/pin/{id}`, `/gazetteer`, `/gazetteer/search`, `/geo/health` |
| `page.html` | Leaflet UI (CARTO dark tiles), live + historical layers, time slider, hide-low-confidence toggle, audio playback in popups, mobile viewport |
| `data/towns_seed.csv` | 30 CT towns + centroids covering every name listed in the task plus extras (Pawcatuck, Noank, Gales Ferry…) |
| `data/streets_seed.csv` | ~50 starter streets/routes for Groton/Mystic/New London/Stonington/etc. with real OSM-aligned coordinates |
| `README.md` | What the tool does, config keys, lite/full diff, attribution (Leaflet, CARTO, OSM/Nominatim) |

### Verified working

End-to-end test with a fake `gmrs.db` + `op25.db` (no `gnuradio`/SDR
required), populated three transcripts, ran the worker for 2 s, confirmed:

- 6 pins materialized (towns, route, intersection, street) with correct
  lat/lon attached.
- Source DBs were opened with `?mode=ro` — never written to.
- Cursors persisted to `~/scanpi/geo_cursor_{gmrs,op25}.txt`.
- FastAPI test client returns 200 on every endpoint, GeoJSON shape valid.
- Gazetteer search/add round-trip works (manual landmark inserted).
- Confidence values land 0.6–0.9 for gazetteer hits.

## What's stubbed

- **`local_geocoder` path** — `Geocoder._maybe_local_lookup()` is an
  intentional empty stub. Hook is in place for a future Photon/Pelias
  HTTP client; gated by `feature_enabled("local_geocoder")`. README
  documents the integration point.
- **POST `/gazetteer` auth** — left a `# TODO auth` comment per
  AGENT_CONTRACT (no `_check_token` exists in `scanpi/api.py` yet).
- **`requests` fallback** — geocoder uses pure stdlib `urllib`, so the
  "if requests import fails, degrade to cache-only" requirement
  effectively becomes "if external HTTP fails, degrade to cache-only,"
  which it does.
- **Real coordinates discipline** — every coord in the seed CSVs is a
  real OSM-aligned town centroid or street midpoint. A few I-95 / Route
  exit points are approximate (town center used as proxy). All are
  confidence ≥ 0.6 in code; nothing fabricated with bogus values.
- **Whisper-noisy text** — extractor is title-case-strict to avoid the
  "and the intersection" false positives that a permissive regex
  produces. If your Whisper output is all-lowercase, towns will still
  match (we lowercase both sides for the town list) but bare streets
  will not. That's deliberate. Confidence stays low when town context
  is missing.

## Integration concerns

1. **`scanpi.profile` does not yet exist.** The tool wraps the
   `feature_enabled` import in a try/except and falls back to
   `SCANPI_FEATURE_<NAME>` env vars. No action needed; works either way.
2. **Registration in `app_v3.py`.** Documented at the top of
   `__init__.py`. The 3-line snippet to add inside `run_v3()` after the
   OP25 registration:
   ```python
   from .tools.geo import GeoTool
   registry.register(GeoTool(config={"data_dir": str(data_dir)}))
   ```
   `needs_sdr = False`, so the coordinator picks it up via
   `start_non_sdr_tools()` automatically.
3. **Static asset note.** `page.html` pulls Leaflet 1.9.4 from unpkg
   and CARTO tiles from `*.basemaps.cartocdn.com`. The Pi 5 needs
   internet on first load (browsers cache afterward). For an offline
   install we'd need to vendor `leaflet.css`/`leaflet.js` into
   `src/scanpi/web/` and switch the page references — leaving as-is to
   match the contract's "no build step, plain HTML+vanilla JS".
4. **Source DB schema assumption.** Reader queries assume
   `transcript_status = 'ok'` exists on both source tables. Confirmed
   present in `gmrs/db.py` and `op25/db.py`.
5. **gmrs/op25 transcript ordering.** We poll by `id ASC` and persist a
   cursor — if either tool ever rewrites transcript text on an existing
   row (e.g. a re-transcribe pass), GEO won't pick it up. Acceptable
   per current behavior of those tools, which set transcript once.
6. **Performance.** Worker polls every 15 s, batches up to 200 rows
   per source, and only hits Nominatim on a gazetteer miss. Cache is
   keyed on the literal query string so repeated mentions of the same
   street resolve from cache after the first hit. On a quiet day the
   tool will issue zero external requests.

## Quick sanity checks the integrator can run

```bash
# Run from repo root after registering the tool in app_v3.py:
python -m scanpi.cli_v3   # or however v3 is launched

# Then:
curl 'http://localhost:8080/tools/geo/api/geo/health'
curl 'http://localhost:8080/tools/geo/api/pins?since=5m'
curl 'http://localhost:8080/tools/geo/'   # serves page.html
```

The map will be empty until GMRS/OP25 produce transcripts that mention
real places. With the seed gazetteer in place, "fire on Route 27 in
Mystic" pins immediately with zero external calls.
