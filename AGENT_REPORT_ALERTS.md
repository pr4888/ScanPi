# Agent ALERTS — Report

Status: shipped, all required deliverables in place. End-to-end smoke + API tests pass locally.

## What shipped

`src/scanpi/tools/alerts/`:

| File | Notes |
|---|---|
| `__init__.py`     | `AlertsTool(Tool)`, `needs_sdr=False`. Polling worker thread (interval 4s) that scans gmrs.db + op25.db read-only via `?mode=ro` URI, runs new `transcript_status='ok'` rows through the watchlist, writes hits to alerts.db, publishes to MQTT. Includes 24h backfill on start so the UI has history immediately. Idempotency via `(source, source_call_id)` so re-polling never double-fires. Profile gate: `feature_enabled("mqtt_alerts")` (default ON). 3-line registration block for `app_v3.py` is at the top of the file. |
| `db.py`           | `AlertsDB` — sqlite at `~/scanpi/alerts.db`. Tables: `alerts(id, ts, source, source_call_id, channel, severity, rules_matched JSON, transcript, audio_url, ack_ts)` + `watchlist_history(rule_name, hit_count, last_hit_ts)`. Indexed by ts, severity, source, and `(source, source_call_id)` for the dedup guard. |
| `watchlist.py`    | YAML loader/saver at `~/scanpi/watchlist.yaml`. Tries `ruamel.yaml` first for round-trip preservation, falls back to PyYAML (comments lost on save — noted in README). 16-rule default seeded on first run including officer down, shots fired, code 3, mayday, pan-pan, fire, MVA, child-missing/AMBER, susan/brianna/james family-name placeholders (disabled by default), plus 2 regex examples (CT phone format, license plate). Plain phrases auto-wrapped with `\b...\b` boundaries; regex patterns detected via metachar scan. Severity validation, regex compile validation, atomic save (.tmp + replace). |
| `matcher.py`      | `match_transcript(text, watchlist) -> list[Hit]`, `aggregate_severity(hits) -> str`. One hit per rule per call. |
| `publisher.py`    | `MQTTPublisher` with paho-mqtt. Default `mqtt://localhost:1883`, override via `SCANPI_MQTT_URL`. Background reconnect loop (30s retries). If `paho-mqtt` import fails the publisher becomes a no-op and logs a one-time warning — alerts still hit alerts.db. Topic format: `scanpi/alerts/<severity>/<source>[/<suffix>]`. |
| `api.py`          | FastAPI `APIRouter` factory. Endpoints: `GET /alerts` (filters `since` (1h/24h/7d/unix-ts), severity, source, limit), `GET /alerts/{id}`, `POST /alerts/{id}/ack`, `GET /watchlist`, `POST /watchlist` (validates regex, returns 400 on bad), `DELETE /watchlist/{name}`, `GET /alerts/health` (mqtt + poll + db status), and a `/recent` adapter so the dashboard live feed can ingest alerts as events. |
| `page.html`       | 3-tab dark UI. Tab 1: live alerts with severity/since/source filters, ack button, embedded audio player wired to `audio_url`. Tab 2: full watchlist CRUD with inline editor (regex validation surfaces from server). Tab 3: subscribe — shows live MQTT health, topic format with wildcards, and copy-paste recipes for ntfy.sh, Pushover, Home Assistant, Tasker. Mobile-friendly viewport meta + media query that collapses rule cards on narrow screens. Uses theme.css tokens consistently. |
| `SUBSCRIBERS.md`  | Full deployment recipes for all four push channels including systemd units, docker-compose for ntfy server, complete bridge scripts, and a `mosquitto_pub` test command. |
| `README.md`       | Config keys table, watchlist YAML format, API surface, lite vs full notes, troubleshooting matrix, idempotency + backfill explanation. |

## Verification done locally

Three test runs, all green:

1. **Module import + rule validation** — confirmed plain-phrase auto-bounding, regex pass-through, that bad regex and bad severity raise `ValueError`.
2. **End-to-end backfill** — synthesised a gmrs.db with 3 transcripts and an op25.db with 1, ran `_backfill()` with MQTT pointed at a dead host. Resulted in 3 alerts in alerts.db (officer-down + 10-33 stack -> critical, mayday -> critical, working-fire -> high), correct rules_matched arrays, correct severity aggregation, watchlist_history rows incremented. Re-running backfill produced no duplicates.
3. **API round-trip** — used FastAPI TestClient against the router. Verified GET watchlist (16 default rules, 9 enabled), POST upsert (200), POST upsert with bad regex (400 + error detail), DELETE (200, then 404 second time), GET alerts (empty list when no source DBs), POST ack on non-existent id (404), GET /alerts/health.
4. **YAML round-trip** — re-loaded the seeded file; all backslash regex patterns (`\bcode\s*(?:3|three)\b`, `\b10[-\s]?33\b`, `pan[-\s]?pan`) compile correctly and match the expected phrases.
5. **SQLite read-only URI** — confirmed `file:C:/path?mode=ro` URI form works on Windows (relevant for dev; Linux Pi is the same syntax).

## Integration concerns

1. **Registration in `app_v3.py`**: The 3 lines are at the top of `__init__.py`, but the coordinator currently only auto-starts non-SDR tools via `coord.start_non_sdr_tools()`. Since `needs_sdr=False`, the tool will be started by `start_non_sdr_tools()` automatically — no extra wiring needed beyond `registry.register(...)`.

2. **`profile.py` not yet present**: `_feature_enabled()` falls back to `os.environ.get("SCANPI_FEATURE_MQTT_ALERTS")`, default ON. Will silently switch to the real `feature_enabled` import once Agent INSTALL ships profile.py.

3. **Auth**: `POST /watchlist`, `POST /alerts/.../ack`, and `DELETE /watchlist/...` have `# TODO auth` comments and don't currently require a token. Per the contract, "stub with `# TODO auth` and a comment" if no shared `_check_token` exists yet. If `scanpi.api._check_token` lands later, search for the TODO comments and wire it in.

4. **Dependencies not yet declared in `pyproject.toml`**: This tool needs `paho-mqtt` (optional, graceful degradation if absent) and a YAML library — `ruamel.yaml` (preferred) OR `pyyaml` (works, loses comments). Recommend Agent INSTALL adds:
   ```
   "paho-mqtt>=1.6",
   "ruamel.yaml>=0.18",
   ```
   to the lite/full extras. Without either YAML lib the tool can't start (raises `RuntimeError` on first watchlist load).

5. **Read-only DB paths**: The tool resolves source DB paths from `data_dir` (`<data_dir>/gmrs.db`, `<data_dir>/op25.db`). This matches the GMRS and OP25 tools' default. If anyone reconfigures those tools to a different db path, alerts won't see them — explicit `gmrs_db_path` / `op25_db_path` config keys could be added later if needed (kept simple for now).

6. **Backfill noise on first install**: 24h of alerts publish to MQTT on startup. If this is annoying on first deploy, set `backfill_hours = 0` in config or just toss the watchlist.yaml that gets seeded — it's noted in README under "Backfill behavior".

7. **Dashboard summary integration**: `summary()` returns `alert_counts` keyed by severity (critical/high/medium/low) with non-zero values. `app_v3.py`'s `DASHBOARD_BODY` already renders an alert ribbon when `sm.alert_counts` is present (lines around 149-160 of app_v3.py) — should "just work" once the tool is registered.

## Quick local test

```python
from scanpi.tools.alerts import AlertsTool
tool = AlertsTool(config={"data_dir": "/tmp/scanpi_test"})
tool.start()
# ... seed ~/scanpi_test/gmrs.db with a transcript containing "officer down"
# ... watch /tools/alerts/api/alerts/health and /tools/alerts/api/alerts
tool.stop()
```

## Files

All under `C:\Users\rdcst\ScanPi-canonical\src\scanpi\tools\alerts\`:

- `__init__.py`
- `api.py`
- `db.py`
- `matcher.py`
- `page.html`
- `publisher.py`
- `watchlist.py`
- `README.md`
- `SUBSCRIBERS.md`
