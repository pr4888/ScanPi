# Agent HACKRF — Report

Status: **ready to integrate.** All offline tests pass on the dev box. The
flowgraph + tool module are wired, lazy-imported, and skip-register cleanly
when gnuradio is missing. Hardware testing pending HackRF arrival.

## Files shipped

```
src/scanpi/tools/hackrf/
  __init__.py          HackrfTool(Tool); sdr_device_index = 200; lifecycle, callbacks
  profiles.py          TOML schema, parse/validate/save, output_index computation
  db.py                ~/scanpi/hackrf.db — tx_events + profile_log
  flowgraph.py         GR top_block: pfb.channelizer_ccf -> N demod chains; lazy-imports GR
  api.py               FastAPI router: /profile, /channels, /events, /audio, /presets, /hackrf/health, /summary
  page.html            4-tab UI: Channels (live grid), Events (audio playback), Profile (TOML editor), Health
  test_offline.py      unittest suite — 4 pure-Python tests + 2 GR-conditional smoke tests
  README.md            schema, lite-vs-full, gotchas, integration snippet, tomorrow's checklist

profiles/sdrs/presets/
  hackrf_gmrs_frs.toml         center 465 MHz, 24 channels (FRS 1-14 + GMRS 15-22 + repeater inputs)
  hackrf_marine_vhf.toml       center 159 MHz, 19 channels (Ch 6/9/13/16/22A + DSC 70 + 67/68/71/72 + NOAA WX1-7 + AIS A/B)
  hackrf_vhf_public_safety.toml center 153.5 MHz, 21 channels (CT fire/EMS analog + VFIRE/VLAW/VMED + MURS + DOT)
  hackrf_uhf_business.toml      center 464.25 MHz, 23 channels (Part 90 itinerant + business pool overview)
```

## What works (verified locally)

- **Profile load / validate / save round-trip** — all 4 presets parse, every
  channel falls inside the [center − sr/2, center + sr/2] window, every
  channel resolves to a unique PFB output bin with `|bin_offset_hz| <= 125 kHz`
  (well inside the 250 kHz per-bin bandwidth so the per-channel rotator
  pulls them cleanly to DC).
- **HackrfTool instantiates without GR** — config-driven, finds bundled
  preset as default profile, exposes status/summary/api/page methods.
  `start()` is a no-op when gnuradio is unavailable (warns, returns).
- **API router** builds with all expected routes:
  `/profile`, `/profile/preset`, `/channels`, `/events`, `/event/{id}`,
  `/audio/{id}`, `/clip/{id}` (alias), `/presets`, `/profile/list`,
  `/hackrf/health`, `/summary`.
- **HTTP Range** support for WAV streaming — copied from the GMRS pattern,
  same `_serve_file_with_range` helper.
- **DB** — open/close/transcript/RSSI roundtrip works; profile_log records
  every profile load.
- **Validation** rejects out-of-window channels with a clear error message
  pointing at the channel + window bounds.
- **Page HTML** renders 4 tabs (Channels live grid with RSSI meters, Events
  list with inline audio, Profile editor with preset picker + TOML editor,
  Health with `hackrf_info` + `lsusb -t` + flowgraph meta). Mobile viewport
  meta tag present. Matches the dark theme variables.

## Stubbed / future work

- **Transcription** — the tool persists WAVs and event rows but does NOT
  spawn its own whisper.cpp worker. The shared transcription pipeline
  (Agent SEARCH) is expected to consume `~/scanpi/hackrf.db` the same way
  it consumes gmrs.db. Hook is `_maybe_run_alerts()` in `__init__.py` —
  empty for now; wire it when the search/transcription consumer ships.
- **Webhook fire on alert** — wired through `fire_webhook` but only fires
  if a transcript lands. Inert until transcription is connected.
- **Per-channel RF metadata enrichment** (RFForge-style RF_TRUST, energy
  gate, voice gate / Silero) — out of scope for v1; HackRF events already
  flow through the standard squelch + WAV record path that Whisper can
  filter downstream.
- **Trigger-based IQ snapshot to SigMF** — research recommends this for
  the "save the moments that mattered" pattern. Not implemented; the
  `feature_enabled("iq_archive")` flag is read from profile but no IQ
  ring buffer exists yet.

## Integration into app_v3.py

Add this block to `run_v3()` in `src/scanpi/app_v3.py`, **after** the existing
GMRS/OP25/Yardstick registrations. The try/except guard is mandatory — gnuradio
is not importable on the Windows dev box, and the import chain inside HackrfTool
will surface that as ImportError or AttributeError:

```python
try:
    from .tools.hackrf import HackrfTool
    registry.register(HackrfTool(config={
        "data_dir": str(data_dir),
        # optional: "profile_path": "...",  "sdr_device": 200,
        # optional: "audio_rate": 16_000, "preroll_s": 0.5, "max_record_s": 120.0,
    }))
except Exception:
    log.warning("HackrfTool failed to register (gnuradio missing or no HackRF?); skipping")
```

`sdr_device_index = 200` keeps the HackRF in its own device space —
the SdrCoordinator will not arbitrate it against RTL-SDRs (0-99) or YS1
(100), so it runs in parallel.

## Tomorrow's hardware-day checklist (when the HackRF arrives)

1. `apt install gnuradio gr-osmosdr hackrf` on the Pi 5.
2. `hackrf_info` — confirm device + firmware version.
3. `lsusb -t` — confirm HackRF and USB SSD are on different host
    controllers.
4. Run `python -m scanpi.tools.hackrf.test_offline` — the two skipped GR
    tests should now run and pass (synthesizes IQ tone, builds top_block,
    runs scheduler 250 ms with the file source).
5. Open `http://scanpi.local:8080/tools/hackrf/`, Profile tab → apply
    preset `hackrf_gmrs_frs`. Channels tab should populate.
6. Key up a GMRS handheld on channel 1 / channel 22 — Channels tab should
    show OPEN state with a green meter, Events tab should show a row
    with a playable WAV after squelch closes.
7. Watch CPU on the Pi — the research budget says the PFB + 24 channels
    + per-channel demod should sit around 1-1.5 cores at 8 Msps. If
    higher, sub-set the channels in the profile.
8. Health tab: `device_present: true`, `flowgraph_running: true`,
    `audio_rate: 16000`. If `device_info` shows zero-output / RMS == 0
    on a channel for more than a minute, watch for the known
    `hackrf_transfer` zero-output bug noted in the research doc and
    consider a watchdog.
9. Try `POST /tools/hackrf/api/profile` with a hand-edited TOML to swap
    to `hackrf_marine_vhf` mid-flight; verify the profile_log table
    records the swap.

## Test results

```
Ran 6 tests in 0.040s
OK (skipped=2)   # 2 GR tests skipped — gnuradio not on dev box
```

The two GR-only tests (`test_build_topology`, `test_run_briefly`) are
ready to run on the Pi the moment `gnuradio` is importable; they
exercise the channelizer + scheduler with a synthesized IQ file source,
no radio required.
