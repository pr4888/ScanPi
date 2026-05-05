# Alerts Tool

Watchlist matcher + MQTT publisher for ScanPi transcripts.

The tool polls `~/scanpi/gmrs.db` and `~/scanpi/op25.db` (read-only,
`?mode=ro` URI) every 4 seconds, runs new transcripts through the
watchlist regex set, and on a hit:

1. Inserts an `alerts` row into `~/scanpi/alerts.db`
2. Publishes `scanpi/alerts/<severity>/<source>` on MQTT

Both are best-effort independent — if MQTT is down, the alert still
lands in the DB and shows on the web UI; the publisher reconnects
every 30 s.

## Files

| File | Purpose |
|---|---|
| `__init__.py`     | `AlertsTool` class — lifecycle, polling worker |
| `db.py`           | `AlertsDB` — sqlite schema + queries |
| `watchlist.py`    | YAML loader, `Rule` dataclass, defaults, regex compile |
| `matcher.py`      | `match_transcript(text, watchlist)` -> list[Hit] |
| `publisher.py`    | `MQTTPublisher` — paho-mqtt with reconnect loop |
| `api.py`          | FastAPI `APIRouter` — alerts + watchlist CRUD |
| `page.html`       | Three-tab dark UI (Live / Watchlist / Subscribe) |
| `SUBSCRIBERS.md`  | ntfy/Pushover/HomeAssistant/Tasker recipes |

## Configuration keys

Pass via `AlertsTool(config={...})`. Most are optional.

| Key | Default | Meaning |
|---|---|---|
| `data_dir`         | `~/scanpi`           | Where alerts.db, watchlist.yaml, gmrs.db, op25.db live |
| `watchlist_path`   | `<data_dir>/watchlist.yaml` | Override watchlist file location |
| `mqtt_url`         | env `SCANPI_MQTT_URL` or `mqtt://localhost:1883` | Broker URL (`mqtt://user:pass@host:port`) |
| `poll_interval_s`  | `4.0`                | Source-DB poll cadence |
| `backfill_hours`   | `24.0`               | On startup, re-scan this many hours so the UI has history |

Profile gating:

```python
feature_enabled("mqtt_alerts")   # default ON for both lite + full
```

If `paho-mqtt` is not installed the tool still runs; alerts go to the DB
only and a one-time warning is logged.

## Watchlist format

`~/scanpi/watchlist.yaml` (auto-seeded with sensible defaults on first
run). YAML round-trip preserves your edits if `ruamel.yaml` is
available; with PyYAML only, comments are lost on save (the README
note here is also surfaced in the `setup` log on startup).

```yaml
rules:
  - name: officer_down
    pattern: officer down              # plain phrase -> auto \b boundaries
    severity: critical                  # low | medium | high | critical
    categories: [police, emergency]
    enabled: true
    mqtt_topic_suffix: ""               # optional: scanpi/alerts/<sev>/<src>/<suffix>

  - name: code_3
    pattern: '\bcode\s*(?:3|three)\b'   # full regex (presence of regex chars triggers regex mode)
    severity: high
    categories: [dispatch]
    enabled: true
```

Validation rules:

- `name` must be non-empty (used as the unique key)
- `pattern` must be non-empty and compile as Python regex
- `severity` must be `low | medium | high | critical`
- Plain phrases (no regex metachars) get wrapped in `\b...\b`
- All matching is case-insensitive

## API surface

Mounted at `/tools/alerts/api/`:

| Method + path                              | Effect |
|---|---|
| `GET /alerts?since=24h&severity=&source=&limit=50` | List alerts in window |
| `GET /alerts/{id}`                         | Single alert detail |
| `POST /alerts/{id}/ack`                    | Mark `ack_ts = now()` |
| `GET /watchlist`                           | List rules |
| `POST /watchlist`                          | Upsert rule (validates regex) |
| `DELETE /watchlist/{name}`                 | Remove rule |
| `GET /alerts/health`                       | MQTT/poll/DB status JSON |
| `GET /recent`                              | Dashboard live-feed adapter (alerts as events) |

The `since` param accepts: `1h`, `30m`, `7d`, `2d`, or a unix timestamp.

## Profiles (lite vs full)

The tool itself is identical on both. Differences come from the source
DBs:

| Aspect | lite | full |
|---|---|---|
| Source transcripts processed   | only running tools' DBs                | all available  |
| `mqtt_alerts` default          | on                                     | on             |
| Backfill hours                 | 24                                     | 24             |
| Polling interval               | 4 s                                    | 4 s            |
| MQTT broker assumption         | local mosquitto                        | local mosquitto |

If the user is running a remote broker (e.g. Home Assistant on a NAS),
override with `SCANPI_MQTT_URL=mqtt://user:pass@nas.lan:1883` in the
systemd unit env.

## How matching works

For each enabled rule, the worker calls `re.search(pattern, transcript)`.
At most one hit per rule per call is emitted (no flood). Aggregated
severity = max severity across all hits. Idempotency: `(source,
source_call_id)` is unique per alert — re-polling the same row never
double-fires.

The MQTT topic suffix used per alert is taken from the highest-severity
hit that has a non-empty `mqtt_topic_suffix`.

## Backfill behavior

On `start()` the tool reads the last 24 hours of `transcript_status =
'ok'` rows from each source, matches them, and inserts any hits it
hasn't seen before. This means a freshly-installed Alerts tool catches
up on the day's history immediately. Backfilled alerts publish to MQTT
too — set `backfill_hours = 0` to skip if you don't want a phone-buzz
storm on first install.

## Troubleshooting

| Problem | Look at |
|---|---|
| No alerts ever fire        | `GET /alerts/health` -> watchlist.enabled > 0; source DBs exist |
| Alerts in DB, none on MQTT | `mqtt.connected = false` -> check broker (`mosquitto_sub -t '#' -v`) |
| `paho-mqtt` warning at start | `pip install paho-mqtt` (Pi: `apt install python3-paho-mqtt` works too) |
| Pattern not matching       | Plain phrases get `\b` boundaries — phrases mid-word need full regex |
| YAML edits lost on save    | `pip install ruamel.yaml` for round-trip preservation |
| Polling lag                | Default 4 s; lower `poll_interval_s` if you need real-time |
| Repeated alerts on restart | Check that `(source, source_call_id)` rows exist (idempotency guard) |

## Phone push next steps

See `SUBSCRIBERS.md` for ntfy.sh / Pushover / Home Assistant / Tasker
wiring. Easiest first install: ntfy + Android app, about 15 minutes
end-to-end.
