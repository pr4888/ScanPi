# ScanPi

Self-contained Raspberry Pi 5 radio scanner with auto-discovery, recording, transcription, and web UI.

## What it does

1. **Discovers** — Sweeps local spectrum, finds active frequencies automatically
2. **Classifies** — Identifies analog FM, P25, DMR, NXDN via signal analysis
3. **Scans** — Priority-queue scanning with adaptive dwell times
4. **Records** — VAD-gated recording (Silero) — only saves real voice
5. **Transcribes** — On-device whisper.cpp (tiny.en) — no cloud needed
6. **Learns** — Builds activity profiles, busy hours, frequency catalog
7. **Serves** — Clean dark web UI for browsing, playback, search

## Hardware

- Raspberry Pi 5 (4GB+)
- Any RTL-SDR (NESDR, generic, etc.)
- SD card (32GB+)
- Optional: USB drive for expanded storage

## Quick Start

```bash
# Install
pip install -e .

# First run — creates config
scanpi --init

# Edit config (optional)
nano ~/scanpi/config.toml

# Run
scanpi

# Open browser
# http://pi-hostname:8080
```

## One-shot survey

```bash
scanpi --survey-only
```

## Install on Pi 5

```bash
# Dependencies
sudo apt install rtl-sdr librtlsdr-dev

# Optional: whisper.cpp for transcription
# Build from source or use faster-whisper
pip install faster-whisper

# Optional: Silero VAD for noise rejection
pip install onnxruntime
# Download silero_vad.onnx to ~/scanpi/models/
```

## Architecture

```
scanpi/
├── surveyor.py     — rtl_power spectrum sweeps
├── classifier.py   — signal identification (FM/P25/DMR/NXDN)
├── scanner.py      — priority-queue tuning + recording
├── transcriber.py  — whisper.cpp / faster-whisper
├── storage.py      — retention, USB auto-mount
├── db.py           — SQLite catalog + recordings + transcripts
├── api.py          — FastAPI REST endpoints
├── app.py          — main orchestrator
└── web/            — dark theme web UI
```

## License

MIT
