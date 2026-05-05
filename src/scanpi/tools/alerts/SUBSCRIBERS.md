# ScanPi Alerts — Subscriber Guide

This guide shows how to wire ScanPi MQTT alerts to:

- **ntfy.sh** (free, easiest, Android/iOS apps)
- **Pushover** (paid, priority + sounds)
- **Home Assistant** (MQTT integration)
- **Tasker** (Android, MQTT plugin)

## Topic format

```
scanpi/alerts/<severity>/<source>[/<suffix>]

severity = low | medium | high | critical
source   = gmrs | op25
suffix   = optional, set per rule via mqtt_topic_suffix
```

Wildcard subscriptions:

| Subscribe to                  | Receive                           |
|---|---|
| `scanpi/alerts/#`             | every alert                       |
| `scanpi/alerts/critical/#`    | only critical                     |
| `scanpi/alerts/+/op25`        | all P25 alerts (any severity)     |
| `scanpi/alerts/+/+/family`    | rules tagged `mqtt_topic_suffix: family` |

## Payload (JSON)

```json
{
  "id": 1234,
  "ts": 1730590200.5,
  "source": "op25",
  "channel_or_tg": "Groton Fire Dispatch (TG 8851)",
  "transcript": "engine 1 working fire 65 jefferson drive",
  "matched_rules": ["fire_general"],
  "audio_url": "/tools/op25/api/clip/9912",
  "severity": "high",
  "hits": [
    {"rule": "fire_general", "severity": "high",
     "categories": ["fire"], "matched_text": "working fire",
     "span": [10, 22], "mqtt_topic_suffix": ""}
  ],
  "backfill": false
}
```

## 1. ntfy.sh (recommended starter)

ntfy is free, self-hostable, has Android + iOS apps, supports priority + sound.

### Install ntfy server (Pi or LAN host)

```bash
sudo curl -L https://github.com/binwiederhier/ntfy/releases/latest/download/ntfy_linux_arm64.tar.gz \
    -o /tmp/ntfy.tgz
sudo tar -xz -C /usr/local/bin -f /tmp/ntfy.tgz --strip-components=1 --wildcards '*/ntfy'
sudo ntfy serve --listen-http :8080 &   # systemd unit recommended in production
```

Or with docker-compose (`docker-compose.yml`):

```yaml
services:
  ntfy:
    image: binwiederhier/ntfy
    command: serve
    ports: ["8080:80"]
    volumes:
      - ./ntfy-data:/var/cache/ntfy
    environment:
      - NTFY_BASE_URL=http://your-pi.local:8080
      - NTFY_CACHE_FILE=/var/cache/ntfy/cache.db
    restart: unless-stopped
```

### Bridge — MQTT to ntfy

`/opt/scanpi-ntfy-bridge.py`:

```python
import json, urllib.request
import paho.mqtt.client as mqtt

NTFY = "http://localhost:8080/scanpi-alerts"
PRIO = {"low": "2", "medium": "3", "high": "4", "critical": "5"}

def on_msg(client, userdata, msg):
    p = json.loads(msg.payload.decode())
    title = f"[{p['severity'].upper()}] {p.get('channel_or_tg','')}".strip()
    body  = (p.get("transcript") or "").strip()
    headers = {
        "Title":    title,
        "Priority": PRIO.get(p["severity"], "3"),
        "Tags":     ",".join(p.get("matched_rules", [])),
    }
    if p.get("severity") == "critical":
        headers["Tags"] += ",rotating_light"
    req = urllib.request.Request(NTFY, data=body.encode("utf-8"), headers=headers)
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        print("ntfy error:", e)

c = mqtt.Client(client_id="scanpi-ntfy-bridge")
c.connect("localhost", 1883, 60)
c.subscribe("scanpi/alerts/#")
c.on_message = on_msg
c.loop_forever()
```

systemd unit `/etc/systemd/system/scanpi-ntfy-bridge.service`:

```ini
[Unit]
Description=ScanPi MQTT -> ntfy bridge
After=network-online.target mosquitto.service

[Service]
ExecStart=/usr/bin/python3 /opt/scanpi-ntfy-bridge.py
Restart=always
RestartSec=10
User=scanpi

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now scanpi-ntfy-bridge
```

On your phone: install **ntfy** app -> subscribe to `http://your-pi.local:8080/scanpi-alerts`.

## 2. Pushover

Paid one-time (about 5 USD per platform), supports priority 2 (override silent mode), retry, custom sounds.

`pushover_bridge.py`:

```python
import json, os, urllib.request, urllib.parse
import paho.mqtt.client as mqtt

USER  = os.environ["PUSHOVER_USER"]
TOKEN = os.environ["PUSHOVER_TOKEN"]
PRIO = {"low": -1, "medium": 0, "high": 1, "critical": 2}

def on_msg(client, userdata, msg):
    p = json.loads(msg.payload.decode())
    sev = p["severity"]
    fields = {
        "user": USER, "token": TOKEN,
        "title": f"[{sev.upper()}] {p.get('channel_or_tg','')}",
        "message": (p.get("transcript") or "")[:1024],
        "priority": PRIO.get(sev, 0),
        "sound": "siren" if sev == "critical" else "pushover",
    }
    if PRIO.get(sev, 0) == 2:
        fields["retry"]  = 60
        fields["expire"] = 600
    data = urllib.parse.urlencode(fields).encode()
    try:
        urllib.request.urlopen("https://api.pushover.net/1/messages.json",
                               data=data, timeout=10).read()
    except Exception as e:
        print("pushover error:", e)

c = mqtt.Client(client_id="scanpi-pushover-bridge")
c.connect("localhost", 1883, 60)
c.subscribe("scanpi/alerts/#")
c.on_message = on_msg
c.loop_forever()
```

Create a Pushover application at https://pushover.net/apps to get the token. Add a `pushover-bridge.service` unit similar to the ntfy one above, with `Environment=PUSHOVER_USER=... PUSHOVER_TOKEN=...`.

## 3. Home Assistant

Add to `configuration.yaml`:

```yaml
mqtt:
  sensor:
    - name: "ScanPi Last Alert"
      state_topic: "scanpi/alerts/+/+/#"
      value_template: "{{ value_json.severity }}"
      json_attributes_topic: "scanpi/alerts/+/+/#"

automation:
  - alias: "ScanPi: Critical alert"
    trigger:
      - platform: mqtt
        topic: "scanpi/alerts/critical/#"
    action:
      - service: notify.mobile_app_my_phone
        data:
          title: "ScanPi CRITICAL"
          message: "{{ trigger.payload_json.transcript }}"
          data:
            ttl: 0
            priority: high
            channel: "alarm_stream"

  - alias: "ScanPi: High alert"
    trigger:
      - platform: mqtt
        topic: "scanpi/alerts/high/#"
    action:
      - service: notify.mobile_app_my_phone
        data:
          title: "ScanPi: {{ trigger.payload_json.channel_or_tg }}"
          message: "{{ trigger.payload_json.transcript }}"
```

To play a TTS announcement on a media player:

```yaml
- service: tts.google_translate_say
  data:
    entity_id: media_player.kitchen
    message: >-
      Alert from ScanPi: {{ trigger.payload_json.severity }}
      on {{ trigger.payload_json.channel_or_tg }}.
      {{ trigger.payload_json.transcript }}
```

## 4. Tasker (Android)

Install **MQTT Client** plugin from Play Store.

Profile config:

```
Broker:   your-pi.local
Port:     1883
Topic:    scanpi/alerts/#
QoS:      1
Username: (blank if anonymous broker)
```

Tasker task on message:

```
1. Variable Set %sev    TO  %mqtt_payload (parse JSON: $.severity)
2. Variable Set %text   TO  %mqtt_payload (parse JSON: $.transcript)
3. Variable Set %src    TO  %mqtt_payload (parse JSON: $.channel_or_tg)
4. Notify  Title="ScanPi [%sev] %src"  Text=%text
5. If %sev ~ critical
     Vibrate Pattern: 0,400,200,400,200,800
     Play Ringtone: /system/media/audio/alarms/Argon.ogg
   End If
```

## Troubleshooting

| Symptom                          | Check |
|---|---|
| No alerts arriving               | `journalctl -u scanpi-* -n 50` and confirm `paho-mqtt` is installed |
| Alerts in DB but not on MQTT     | `mqtt connected` is **off** in `/tools/alerts/api/alerts/health` |
| Mosquitto rejecting connections  | `sudo journalctl -u mosquitto -n 30`; check `/etc/mosquitto/mosquitto.conf` for anonymous + `listener 1883` |
| Phone notifications stop after a few hours | Battery optimization is killing the bridge app — exempt it |
| Tasker doesn't fire              | Confirm the MQTT plugin can connect from the same Wi-Fi |

## Test it

Publish a synthetic alert from the Pi:

```bash
mosquitto_pub -h localhost -t scanpi/alerts/critical/test \
  -m '{"ts":1730590000,"source":"test","severity":"critical","channel_or_tg":"TEST","transcript":"this is a test","matched_rules":["test"]}'
```

If your subscribers are wired up, every channel should buzz.
