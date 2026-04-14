# ScanPi

A modular, self-contained radio scanner for any Linux box with an RTL-SDR.
Starts with a **GMRS/FRS neighborhood monitor** tool and grows by plugging in
more specialty tools (ham, NOAA, ADS-B, ISM, …).

Phosphor-themed web UI. Works on a Raspberry Pi, NUC, or an old laptop.

![ScanPi — phosphor UI](docs/screenshot.png)

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
- Decent antenna for your band of interest (quarter-wave whip for 462 MHz is ~6.4")

## What's included

| Tool | Band | Purpose |
|---|---|---|
| **GMRS Monitor** | 462 MHz block, 15 channels | Track who's on FRS/GMRS in the neighborhood, record + transcribe transmissions |
| *(add your own)* | — | Plug-in architecture — see `src/scanpi/tools/__init__.py` for the `Tool` base class |

Only one SDR-holding tool runs at a time; the dashboard lets you switch
between them. State persists across reboots.

## Upgrading

```bash
sudo bash /opt/scanpi/install.sh
```

The installer is idempotent and will pull the latest `master`.

## Manual / development install

```bash
git clone https://github.com/pr4888/ScanPi
cd ScanPi
pip install --break-system-packages -e .
pip install --break-system-packages 'numpy<2' faster-whisper
scanpi-v3
```

## Data layout

- `/opt/scanpi/` — code
- `~scanpi/scanpi/` — runtime data (SQLite DB, audio clips, Whisper model cache, coordinator state)
- `/etc/systemd/system/scanpi.service` — service

## License

MIT — see `LICENSE`.
