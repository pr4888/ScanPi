# ScanPi

> The scanner of the past, with the features of the present.

Multi-SDR neighborhood scanner: P25 trunking, GMRS/marine/fire monitoring,
keyword alerts to your phone, transcript search, geographic overlay, all
served over Tailscale.

Two distributions, one codebase:

| | scanpi-lite | scanpi-full |
|---|---|---|
| Target | Pi 5 / Pi 4 / ARM SBC | Ubuntu x86_64 (NUC, server, workstation) |
| Transcription | 1 stream at a time, tiny.en | 4 streams concurrent, small.en (or medium.en if GPU) |
| Semantic search | opt-in | on |
| IQ archive | opt-in | continuous ring buffer + trigger snapshots |
| Trunk-recorder | opt-in | runs alongside OP25 |
| Cross-channel correlation | not yet | opt-in |
| Disk | ~1 GB install | ~3-4 GB install |
| Stable load avg | ~2-3 | scales with cores |

## Quick start

### Pi 5 (lite)

Fresh Raspberry Pi OS Bookworm 64-bit on an 8 GB+ SD:

```bash
git clone https://github.com/pr4888/ScanPi
cd ScanPi
sudo bash install/install.sh
```

Plug in an RTL-SDR (NESDR SMArTee or similar). Wait ~5 minutes. Open
`http://scanpi.local:8080/` from any device on your LAN.

### Ubuntu (full)

```bash
git clone https://github.com/pr4888/ScanPi
cd ScanPi
sudo bash install/install.sh --full
```

Plug in a HackRF + extra RTL-SDRs. Open `http://<your-ip>:8080/`.

### Tailscale (recommended)

```bash
# on the ScanPi box
sudo tailscale up
sudo tailscale serve --bg --https=443 http://localhost:8080

# from any phone/laptop on your tailnet
open https://scanpi.<your-tailnet>.ts.net
```

That's it. MagicDNS resolves the name; the cert is real Let's Encrypt;
the listener is auth'd at the network layer.

## What you get out of the box

- **GMRS / FRS monitor** — multi-channel parallel decoding, squelch + VAD, audio recording
- **OP25 P25 trunking** — control-channel follow, talkgroup metadata, per-call audio
- **Keyword search (FTS5)** — full-text over every transcript, regex supported
- **Semantic search (full default; opt-in lite)** — "find calls about gunfire" matches "shots fired" + "weapon discharge" via bge-small-en embeddings
- **MQTT keyword alerts** — watchlist hits push to mosquitto then to your phone (Pushover / HomeAssistant / Tasker / ntfy.sh)
- **Geographic overlay** — street/town names from transcripts then pins on a Leaflet map
- **Single-source transcription mode (lite default)** — pick which source gets real-time whisper at any time, swap on the fly
- **HackRF wideband** — polyphase channelizer for many narrow channels at once (when hardware present)
- **YARD Stick One ISM sweep** — sub-1 GHz LPD/Z-Wave/LoRa surveillance (when hardware present)

## Architecture

Each SDR is a `Tool` registered with a coordinator. Tools with different
`sdr_device_index` values run concurrently; tools sharing an index are
arbitrated. Adding a new SDR is one TOML drop-in into `~/scanpi/profiles/sdrs/`.

```
            +------------------------------------------+
            |                ScanPi web UI             |  port 8080
            |  /tools/{gmrs,op25,search,alerts,geo,    |
            |   hackrf,ysone}/  + dashboard            |
            +------+--------------+--------------+-----+
                   |              |              |
              +----+----+    +----+----+    +----+----+
              |  GMRS   |    |  OP25   |    |  HackRF |   one tool per SDR;
              |  tool   |    |  P25    |    |  wide-  |   each holds an
              | RTL-SDR |    | trunk   |    |  band   |   sdr_device_index
              +---------+    +---------+    +---------+
                   |              |              |
                   v              v              v
              +-----------------------------------+
              |     sqlite (gmrs/op25/hackrf db)  |
              +-----------------------------------+
                   ^                       |
                   |                       v
                   |              +-----------------+
                   |              |  alerts tool    |---> MQTT (mosquitto)
                   |              |  (watchlist)    |     ---> phone push
                   |              +-----------------+
                   |
              +-----------------+
              |  search tool    |  FTS5 + bge-small-en --> /v1/search
              +-----------------+
              +-----------------+
              |  geo tool       |  Nominatim cache --> Leaflet map
              +-----------------+
```

See `RESEARCH_2026-05-04.md` for the full architecture analysis (compute
budgets, USB topology, decoder comparisons, 30-day roadmap).

## Profiles

`~/scanpi/profile.toml` is the single config file. Bundled defaults at
`profiles/lite.toml` and `profiles/full.toml`. Edit freely; restart with
`sudo systemctl restart scanpi-v3`.

Most useful keys:

```toml
[transcription]
active_target = "op25"   # or "gmrs", "all", "none". Live-switch via UI too.

[search]
semantic = false         # set true on lite if you have CPU to spare

[experimental]
trunk_recorder = false   # set true to run TR alongside OP25 (full default)
```

## Updating

```bash
cd /path/to/ScanPi
git pull
sudo bash install/install.sh --skip-deps
sudo systemctl restart scanpi-v3
```

## Hardware support

**Tested**: NESDR SMArt v5, NESDR SMArTee v5, RTL-SDR Blog v3, HackRF One,
YARD Stick One, KrakenSDR (single-channel mode).

**Should work** (gr-osmosdr / SoapySDR drivers present): Airspy R2, Airspy
Mini, BladeRF, USRP B200/B210, LimeSDR Mini, RFspace SDR-IQ.

**Not in scope**: SDRplay (proprietary driver), USRP N-series.

## Don't-do list (read this)

- Don't expose `/v1/*` to the public internet without auth in front. Tailscale
  Funnel is fine for personal use *with* auth — see `docs/auth.md`.
- Don't run trunk-recorder on lite without a USB SSD. SD card IOPS will choke.
- Don't put a HackRF and a USB SSD on the same Pi USB controller. `lsusb -t`
  will tell you which port is on which bus.
- Don't use the upstream `op25/op25` repo. Use the boatbod fork — bundled.
- Don't ignore RF front-end filters. An unfiltered HackRF picks up your Pi's
  switching power supply. Cheap SAW filter = 10+ dB SNR win.

## Documentation

- `docs/install.md` — detailed install + first-boot wizard
- `docs/profiles.md` — every profile key explained
- `docs/sdr-pluggability.md` — how to add a new SDR (it's one TOML file)
- `docs/alerts.md` — watchlist syntax, MQTT topics, phone push setup
- `docs/imaging.md` — building a flashable Pi image (Raspberry Pi Imager preset)
- `docs/auth.md` — securing the web UI before exposing via Funnel
- `docs/troubleshooting.md` — common gotchas
- `RESEARCH_2026-05-04.md` — the architectural research that shaped this design

## License

GPLv3 for the ScanPi code. Bundled dependencies retain their own licenses
(boatbod OP25 GPLv3, whisper.cpp MIT, Silero VAD MIT, Leaflet BSD-2,
bge-small-en MIT, etc.).

## Credits

Built on the shoulders of the FOSS SDR community: GNU Radio, gr-osmosdr,
OP25 (boatbod), trunk-recorder, RTLSDR-Airband, AIS-catcher, dump1090,
rdio-scanner, whisper.cpp, Silero VAD, sqlite, FastAPI, Leaflet,
OpenStreetMap.
