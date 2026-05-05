# ScanPi v0.4.0 Agent Contract

You are one of several parallel agents extending ScanPi. Read this first.

## Repo layout

```
C:\Users\rdcst\ScanPi-canonical\
├── README.md
├── pyproject.toml
├── profiles/                       # NEW — created by Agent INSTALL
│   ├── lite.toml                   # Pi 5 / ARM SBC default
│   ├── full.toml                   # Ubuntu x86_64 default
│   └── sdrs/                       # per-SDR profiles (HackRF, RTL-SDR, etc.)
├── install/                        # NEW — Agent INSTALL
│   ├── install.sh                  # entrypoint, autodetects target
│   ├── install-lite.sh
│   ├── install-full.sh
│   └── systemd/*.service
├── src/scanpi/
│   ├── tools/
│   │   ├── gmrs/                   # EXISTING — do not touch
│   │   ├── op25/                   # EXISTING — do not touch
│   │   ├── ysone/                  # EXISTING — do not touch
│   │   ├── search/                 # NEW — Agent SEARCH
│   │   ├── alerts/                 # NEW — Agent ALERTS
│   │   ├── geo/                    # NEW — Agent GEO
│   │   └── hackrf/                 # NEW — Agent HACKRF
│   ├── profile.py                  # NEW — feature flag loader (Agent INSTALL)
│   ├── app_v3.py                   # EXISTING — coordinator will register your tool here
│   └── tools/__init__.py           # EXISTING — Tool / ToolStatus / ToolRegistry base classes
└── AGENT_CONTRACT.md               # this file
```

## Each agent's scope

| Agent | Owns | Touches | Must not touch |
|---|---|---|---|
| SEARCH | `src/scanpi/tools/search/` | gmrs.db / op25.db (read-only triggers OK) | gmrs/, op25/, ysone/ source |
| ALERTS | `src/scanpi/tools/alerts/` | scanpi/alerts.db (new) | gmrs/, op25/ source |
| GEO    | `src/scanpi/tools/geo/`    | geo.db (new), gazetteer.csv | gmrs/, op25/ source |
| HACKRF | `src/scanpi/tools/hackrf/` | profiles/sdrs/*.toml | RTL-SDR tool source |
| INSTALL| `install/`, `profiles/`, `README.md`, `src/scanpi/profile.py` | top-level docs | everything in `tools/` |

## Tool framework contract

Every tool subclasses `scanpi.tools.Tool` (already exists). Required:

```python
from scanpi.tools import Tool, ToolStatus

class MyTool(Tool):
    id = "search"             # short URL slug — must match folder name
    name = "Search"           # human label for nav
    description = "Hybrid lexical + semantic transcript search"
    needs_sdr = False         # True only if you hold an SDR exclusively

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        # ...

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def status(self) -> ToolStatus: ...
    def api_router(self):     # FastAPI APIRouter mounted at /tools/<id>/api/*
        return self._router
    def page_html(self) -> str | None:    # served at /tools/<id>/
        return ...
    def summary(self) -> dict:            # dashboard widget payload
        return {...}
```

Register in `app_v3.py` happens during integration — leave a 3-line snippet at top of your tool's `__init__.py` showing how to register, in a comment block.

## Profile system contract (read-only for tool agents)

`scanpi.profile` exposes one function — assume it works even before INSTALL ships it:

```python
from scanpi.profile import feature_enabled, get_profile

if feature_enabled("semantic_search"):
    # opt-in path
else:
    # always-on path
```

Profile keys your code may check:
- `semantic_search` — bge-small embeddings (lite=opt-in, full=on)
- `multi_stream_transcription` — multiple whisper streams (lite=False, full=True)
- `iq_archive` — continuous IQ ring buffer (lite=False, full=True)
- `trunk_recorder` — TR alongside OP25 (lite=opt-in, full=True)
- `external_geocoder` — Nominatim API calls (lite=on with cache, full=on with cache)
- `cross_channel_correlation` — speaker embedding clustering (both=opt-in for now)

If `profile.py` isn't ready yet, fall back to `os.environ.get("SCANPI_FEATURE_<NAME>", "0") == "1"`.

## Database conventions

- Each tool owns its own sqlite DB at `~/scanpi/<toolid>.db` (alerts.db, geo.db, search.db).
- The two existing DBs (gmrs.db, op25.db) are READ-ONLY for new tools, except via FTS5 triggers (Agent SEARCH).
- Use `data_dir = Path(self.config.get("data_dir", Path.home() / "scanpi"))`.

## API conventions

- Routes mounted at `/tools/<id>/api/<endpoint>` automatically — your APIRouter just declares relative paths.
- Top-level shortcuts allowed for major endpoints: `/v1/search`, `/v1/alerts`, etc. Add these via `api_router()` returning the same router; integration will mount.
- All POSTs that mutate need at minimum a per-instance auth token check — call `scanpi.api._check_token(request)` if it exists, else stub with `# TODO auth` and a comment.

## UI conventions

- One HTML file per tool: `tools/<id>/page.html` — plain HTML + vanilla JS, no build step.
- Match the existing dark theme — copy `web/theme.css` patterns. Look at `tools/gmrs/page.html` and `tools/op25/page.html` for style.
- Mobile-friendly viewport meta tag is required (PWA work happens later).

## Lite vs Full defaults

| Feature | lite default | full default |
|---|---|---|
| Whisper concurrent streams | 1 (single-source mode) | 4 |
| Whisper model | tiny.en | small.en (or medium.en if GPU) |
| FTS5 search | on | on |
| Semantic search | OFF (opt-in) | ON |
| MQTT alerts | on | on |
| Geo overlay | on (cached only) | on (live geocoder) |
| IQ ring buffer | OFF | 60s in RAM |
| IQ archive on trigger | OFF | ON |
| Trunk-recorder | OFF | ON alongside OP25 |
| Speaker correlation | OFF | OFF (v2) |

## Don't-do list

- Don't touch the running Pi (192.168.4.57). Build locally only. Integration step deploys.
- Don't introduce `npm` / Node build steps. Plain HTML + vanilla JS.
- Don't add Docker as a hard dep for lite. Optional for full.
- Don't break the existing Tool framework or coordinator.
- No emojis in code or UI unless user explicitly asked for them.
- Don't use ZeroMQ yet — that's Phase 2 architecture. For now, tools read each other's sqlite directly or via FastAPI calls.

## What "done" looks like for your track

1. Code lives in your assigned subdir
2. `tools/<id>/__init__.py` exports a `<Name>Tool` class that inherits `Tool`
3. UI page renders in browser without errors
4. README in your subdir explains the tool, config keys, lite/full differences
5. Brief notes at the top of your final report: what shipped, what's stubbed, what needs integration help

Write the report into `C:\Users\rdcst\ScanPi-canonical\AGENT_REPORT_<YOURNAME>.md` when done.
