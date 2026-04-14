# ScanPi

A modular, self-contained radio scanner for any Linux box with an SDR.
Phosphor-themed web UI, on-device speech-to-text, keyword alerts, search,
audio playback. Built to run 24/7 on a Raspberry Pi in a closet — no cloud,
no account, no data leaves the box.

## One-line install

On a fresh Debian / Ubuntu / Raspberry Pi OS (root required):

```bash
curl -fsSL https://raw.githubusercontent.com/pr4888/ScanPi/master/install.sh | sudo bash
```

That script:
- installs GNU Radio, RTL-SDR tools, Python deps (`numpy<2` pinned)
- creates a dedicated `scanpi` system user (no login, no baggage)
- clones this repo into `/opt/scanpi`
- installs `faster-whisper` for on-device transcription
- writes a `scanpi.service` systemd unit and starts it
- blacklists the DVB-T kernel driver and installs RTL-SDR udev rules

When done, open `http://<host>:8080/` in a browser.

## Hardware

- Any Linux host (Pi 4/5, x86 mini-PC, old laptop)
- Any RTL-SDR (NESDR SMArTee, generic, HackRF also works)
- Decent antenna for your band of interest
  - Quarter-wave whip for 462 MHz (GMRS) is ~6.4"
  - Quarter-wave whip for 770-855 MHz (P25) is ~3.4"
  - HackRF ANT500 telescoping whip works across both

## Tools (v0.3)

Two tools ship today. More are coming; each is a plugin under `src/scanpi/tools/`.

| Tool | Band | What it does |
|---|---|---|
| **GMRS Monitor** | 462 MHz (Ch 1-7 + 15-22) | Parallel 15-channel FRS/GMRS monitor. Records each transmission to WAV, transcribes with Whisper, per-channel RSSI, duration, subtone (planned). Great for "who's on kids' walkies in the neighborhood". |
| **P25 Trunking** | 770-855 MHz | Wraps OP25's multi_rx decoder + subprocess watchdog. Tracks active talkgroups, records each call, transcribes, color-codes by category (police/fire/ems/transit/utility). Reads standard OP25 `.json` config + `.tsv` talkgroup files. |

Only one SDR-holding tool runs at a time. Switch between them on the
dashboard; the coordinator persists the last-active choice across reboots.
A second RTL-SDR lets both run in parallel (framework-supported; enable
per-tool via the `device` field when you configure it).

## Features

- **On-device Whisper transcription** (faster-whisper `tiny.en` by default)
  runs on a background worker — the flowgraph and UI never block on it.
- **Keyword alerts** — every transcript is scanned for emergency phrases
  (`shots fired`, `working fire`, `pursuit`, `cardiac`, `mayday`, `crash`, …).
  Matching calls highlight red, show a color-coded kind badge, and appear
  in the `/api/alerts` feed and dashboard.
- **Full-text search** — hit any Recent-table search box and the DB returns
  every call whose transcript, TG name, channel, or alert kind matches.
  Click a TG / channel in the Stats table to auto-filter.
- **Audio playback in browser** — HTTP Range support, 16 kHz WAV, consistent
  across Chrome/Safari/Firefox. Click a row to expand an inline detail card
  with big player + large transcript + related calls on the same TG.
- **Hourly sparkline** — activity bar chart above each tool's stats table.
- **CSV export** — `[export CSV]` on each tool page dumps the full history.
- **Retention manager** — audio clips rotate oldest-first when either
  age limit (default 7 days) or size budget (1 GB OP25, 512 MB GMRS)
  is exceeded. DB metadata + transcripts remain forever; only WAVs go.
- **Subprocess watchdog** — if OP25's decoder dies, it's respawned
  automatically (with crash-loop guard).
- **Health monitor** — dashboard card flips to `warn` if a tool is
  running but hasn't seen activity for an expected window.
- **Phosphor UI** — green + amber CRT aesthetic, scanline overlay,
  monospaced. Mobile-friendly (collapses to single column on phones).

## Endpoints (quick reference)

All URLs are relative to `http://<host>:8080`.

```
GET  /                          Dashboard
GET  /settings                  System + per-tool read-only config view
GET  /api/tools                 List tools + status + summary
GET  /api/health                Quick health check
GET  /api/coordinator/status    Which tool holds the SDR
POST /api/coordinator/activate  {tool_id: "gmrs"}
POST /api/coordinator/deactivate

GET  /tools/<id>/               Tool's own HTML page
GET  /tools/<id>/api/live       Live state (per-channel RSSI, active calls)
GET  /tools/<id>/api/stats?hours=24
GET  /tools/<id>/api/recent?limit=50
GET  /tools/<id>/api/hourly?hours=24
GET  /tools/<id>/api/alerts?limit=20
GET  /tools/<id>/api/search?q=<query>&limit=200
GET  /tools/<id>/api/clip/<id>          Range-aware WAV
GET  /tools/<id>/api/event/<id>         GMRS detail (or /api/call/<id> for OP25)
GET  /tools/<id>/api/export.csv
```

## Data layout

- `/opt/scanpi/` — code (installed read-only)
- `~scanpi/scanpi/` (`/home/scanpi/scanpi/` on default installs)
  - `gmrs.db` / `op25.db` — SQLite event logs, transcripts, alerts
  - `gmrs_audio/YYYY-MM-DD/chNN/*.wav`
  - `op25_audio/YYYY-MM-DD/tg_NNNNN/*.wav`
  - `models/` — Whisper model cache (first-use download ~75 MB)
  - `coordinator.json` — which SDR tool was last active
  - `logs/op25.log` — multi_rx decoder output (watched by the tailer)
- `/etc/systemd/system/scanpi.service`

## Upgrading

```bash
sudo bash /opt/scanpi/install.sh
```

Idempotent — pulls latest `master`, restarts the service.

## Manual / development install

```bash
git clone https://github.com/pr4888/ScanPi
cd ScanPi
pip install --break-system-packages -e .
pip install --break-system-packages 'numpy<2' faster-whisper
scanpi-v3
```

## OP25 (P25 trunking) setup

OP25 isn't bundled. If the P25 tool is showing "OP25 not found", install
it once per host:

```bash
cd ~
git clone https://github.com/boatbod/op25
cd op25
./install.sh
```

Then drop your system's `.json` + `.tsv` talkgroup file into
`~/op25/op25/gr-op25_repeater/apps/`. Set `op25_config` and
`talkgroups_tsv` in the tool config (ScanPi defaults: `clmrn_cfg.json`
and `clmrn_talkgroups.tsv`).

## Alert webhooks (push notifications)

Every transcribed call is scanned for emergency keywords. Matches (kind =
`fire`, `violence`, `pursuit`, `medical`, `emergency`, `accident`) POST a
JSON blob to a URL you configure — ScanPi stays generic, you pipe it to
whatever notification service you already use.

Set it up with one env var in the systemd unit:

```bash
sudo systemctl edit scanpi.service
# Add:
[Service]
Environment="SCANPI_WEBHOOK_URL=https://your-endpoint"
Environment="SCANPI_PUBLIC_URL=http://scanpi.local:8080"  # optional, included in payload
sudo systemctl restart scanpi.service
```

The payload is a single JSON POST:

```json
{
  "tool": "op25",
  "event_type": "alert",
  "alert_kind": "fire",
  "alert_match": "working fire",
  "tgid": 8852,
  "tg_name": "Groton Fire Ops 1",
  "category": "fire",
  "freq_mhz": 852.1625,
  "transcript": "Dispatch to 14, we have a working fire at 42 Main Street",
  "timestamp": 1700000000.0,
  "clip_url": "http://scanpi.local:8080/tools/op25/api/clip/123"
}
```

### Recipe: ntfy.sh (simplest phone push)

```
SCANPI_WEBHOOK_URL=https://ntfy.sh/YOUR-SECRET-TOPIC
```

Install the ntfy app, subscribe to `YOUR-SECRET-TOPIC`, phone notifies on
every alert. Server-side formatting isn't ideal (you get raw JSON) — for
prettier output, run a tiny adapter:

```bash
# ntfy-adapter.sh — run behind nginx or in a container
while read POST; do
  ...  # parse JSON with jq, send to ntfy with -H Title: ...
done
```

### Recipe: Home Assistant

```yaml
# configuration.yaml
rest_command:
  # (nothing — ScanPi is the one POSTing)

automation:
  - alias: "ScanPi alert"
    trigger:
      platform: webhook
      webhook_id: scanpi-alerts
    action:
      - service: notify.mobile_app_patricks_phone
        data:
          title: "🚨 {{ trigger.json.alert_kind | upper }} — {{ trigger.json.tg_name }}"
          message: "{{ trigger.json.transcript }}"
          data:
            url: "{{ trigger.json.clip_url }}"
```

Then:

```
SCANPI_WEBHOOK_URL=https://ha.example/api/webhook/scanpi-alerts
```

### Recipe: Discord webhook

Discord wants a specific payload shape, so run a 20-line adapter:

```python
# /usr/local/bin/scanpi-discord.py
from flask import Flask, request
import requests
app = Flask(__name__)
DISCORD = "https://discord.com/api/webhooks/..."
@app.route("/scanpi", methods=["POST"])
def forward():
    e = request.get_json()
    requests.post(DISCORD, json={
        "embeds": [{
            "title": f"🚨 {e['alert_kind'].upper()} — {e.get('tg_name') or 'Ch '+str(e.get('channel'))}",
            "description": e.get("transcript","(no transcript)"),
            "color": 0xff5e5e,
            "fields": [
                {"name": "Category", "value": e.get("category",""), "inline": True},
                {"name": "Freq",     "value": f"{e['freq_mhz']:.4f} MHz", "inline": True},
            ],
            "url": e.get("clip_url"),
        }]
    })
    return "", 204
app.run(host="127.0.0.1", port=9000)
```

Then `SCANPI_WEBHOOK_URL=http://127.0.0.1:9000/scanpi`.

## Writing your own tool

Implement `scanpi.tools.Tool`:

```python
from scanpi.tools import Tool, ToolStatus

class MyTool(Tool):
    id = "mytool"
    name = "My Tool"
    description = "does something cool with the SDR"
    needs_sdr = True  # or False if your tool doesn't need exclusive SDR access

    def start(self): ...           # spin up
    def stop(self):  ...           # tear down
    def status(self) -> ToolStatus: ...
    def summary(self) -> dict:      # shown on dashboard card
        return {...}
    def api_router(self):           # optional FastAPI router mounted at /tools/<id>/api
        ...
    def page_html(self) -> str | None:  # optional HTML served at /tools/<id>/
        ...
```

Register the tool in `run_v3()` in `src/scanpi/app_v3.py`.

## License

MIT — see `LICENSE`.
