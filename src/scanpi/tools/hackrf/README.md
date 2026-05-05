# HackRF Wideband Channelizer (ScanPi tool)

A GNU Radio polyphase FFT channelizer driven by a HackRF One. Splits a
single ~8 MHz wide capture into N narrowband channels at once, each with
its own NFM/AM demod chain, squelch, and per-TX WAV recorder. Mirrors the
Tool / event / WAV / sqlite layout the GMRS tool already uses, so the
shared dashboard widgets, transcription pipeline, and SEARCH / ALERTS
agents work without modification.

The shape of one HackRF profile is one TOML file under
`~/scanpi/profiles/sdrs/<id>.toml`. Switching from GMRS coverage to
marine VHF coverage at runtime is a single API call (or a click on the
Profile tab in the UI).

## Quick wiring (app_v3 integration)

```python
try:
    from .tools.hackrf import HackrfTool
    registry.register(HackrfTool(config={"data_dir": str(data_dir)}))
except Exception:
    log.warning("HackrfTool failed to register; skipping (gnuradio missing or no HackRF?)")
```

The registration is `try/except` guarded because gnuradio is not on the
dev box. On the Pi, `apt install gnuradio gr-osmosdr hackrf` is enough.

## TOML schema

```toml
[sdr]
id          = "hackrf0"          # short identifier — also the filename
driver      = "hackrf"
serial      = ""                 # leave empty for the first HackRF found
center_hz   = 462_500_000
sample_rate = 8_000_000          # see "Pi 5 USB ceiling" below
gain        = "lna=24,vga=20,amp=0"
front_end   = ""                 # description of physical filter (free-form)
fake_iq_path = ""                # if set, replay this IQ file instead of HW

[channelizer]
type            = "pfb"          # only "pfb" supported today
num_chans       = 32             # M — must divide sample_rate cleanly
attenuation_db  = 80.0
taps_window     = "blackman-harris"
transition_frac = 0.20           # transition BW = 20% of channel BW

[[channels]]
name        = "GMRS-1"
freq_hz     = 462_562_500
demod       = "nfm"              # nfm | wfm | am
bw_hz       = 12_500
squelch_db  = -25.0
deemph_us   = 75.0               # NFM/WFM only; ignored for AM
notes       = "FRS/GMRS shared, 2W FRS / 5W GMRS"
```

### Validation rules

- `sample_rate > 0`
- `num_chans > 0`
- `channel_bw_hz = sample_rate // num_chans` must be > 0
- every channel's `freq_hz` must lie inside
  `[center_hz - sample_rate/2, center_hz + sample_rate/2]`
- `demod` must be one of `nfm`, `wfm`, `am`
- channel `name` must be unique within the profile

The validator also fills in two computed fields per channel:
- `output_index` — the PFB output port (0..M-1) carrying that frequency
- `bin_offset_hz` — residual offset inside the bin; the flowgraph applies
  a `rotator_cc` to pull it to DC if it isn't already aligned.

## Lite vs full

| Feature | lite (Pi 5) | full (x86_64) |
|---|---|---|
| Default sample rate | 8 Msps | 8 Msps (10 Msps optional, see below) |
| Default `num_chans` | 32 | 32 |
| Default channels enabled | suggest 6-8 active in the [[channels]] block | all 32 active |
| Audio output | 16 kHz mono PCM WAV | same |
| Whisper transcription | tiny.en, queued | small.en, parallel |
| IQ ring buffer | OFF | 60 s in RAM, snapshot on trigger |

The presets in `profiles/sdrs/presets/` ship with channels appropriate
for **full**. To run them on a Pi 5, comment out the `[[channels]]`
blocks you don't want and reload the profile — the channelizer still
splits the full band into 32 outputs internally; demod chains only run
for the `[[channels]]` you list. CPU and disk drop linearly with the
number of channels you actually keep.

## HackRF gotchas (from research, treat as known)

1. **Pi 5 USB 2.0 ceiling**: HackRF negotiates USB 2.0. **8 Msps is the
   sustained safe rate.** 10 Msps works only if the HackRF is the only
   device on its host controller. 12.5–20 Msps drops samples.
   Default the preset to `sample_rate = 8_000_000`.
2. **Powered USB hub mandatory** if HackRF + RTL-SDRs + USB SSD all on
   the Pi. Pi 5 PSU current budget is real.
3. **Front-end SAW filter** per band gives ~10 dB SNR. Without one, the
   8-bit ADC desensitizes from FM-broadcast / pager strong signals.
4. **`hackrf_open` after device reset takes ~2 s.** Don't fail-fast.
5. **Zero-output channels are a known intermittent** of `hackrf_transfer`.
   The gr-osmosdr / SoapyHackRF path used here is more reliable but not
   immune. Watchdog-restart on RMS == 0 if you see this in the field.
6. **PPM correction**: HackRF has a TCXO but isn't disciplined.
   ±10 ppm drift can be visible. Calibrate against a known nearby NOAA
   carrier or a GPSDO if the channel is wide enough to see it.

## Picking a preset

Bundled presets live in `profiles/sdrs/presets/`. List them with
`GET /tools/hackrf/api/presets`. Apply one with
`POST /tools/hackrf/api/profile/preset` body `{"name": "hackrf_gmrs_frs"}`.

| Preset | Center | Span | Band |
|---|---|---|---|
| `hackrf_gmrs_frs` | 465 MHz | 8 MHz | GMRS + FRS (462 + 467) |
| `hackrf_marine_vhf` | 159 MHz | 8 MHz | Marine VHF + DSC + NOAA wx |
| `hackrf_vhf_public_safety` | 156 MHz | 8 MHz | CT VHF analog fire/EMS |
| `hackrf_uhf_business` | 465 MHz | 10 MHz | UHF business pool overview |

Swapping presets restarts the flowgraph; in-flight events are flushed
and recorded normally.

## Adding a custom channel

Edit the profile in the UI ("Profile" tab → save & restart) or write a
new TOML to `~/scanpi/profiles/sdrs/<id>.toml` and POST it:

```bash
curl -X POST -H "Content-Type: text/plain" \
  --data-binary @my.toml \
  http://scanpi.local:8080/tools/hackrf/api/profile
```

Validation errors come back as 400 with a message pointing at the
offending channel. The validator runs in milliseconds — there's no
penalty to iterating.

## Database

`~/scanpi/hackrf.db` (sqlite). Schema mirrors the GMRS tool, except the
channel column is a string (so it can be `"GMRS-1"`, `"MARINE-16"`,
`"NOAA-WX1"` regardless of profile). Includes a `profile_log` table
that records every profile load (timestamp, id, center, sr, channel
count, source path) so you can correlate event spans with profile
swaps.

## Audio

`~/scanpi/hackrf_audio/<channel>/<YYYY-MM-DD>/<channel>_<HHMMSS>_<unix>.wav`.
16 kHz 16-bit mono PCM WAV. Browsers play these natively; the
`/audio/<event_id>` endpoint streams with HTTP Range headers so seeking
works.

## Hardware testing TODO (when the HackRF arrives)

1. `hackrf_info` — confirm fw version + serial
2. `lsusb -t` — confirm HackRF is on its own controller, USB SSD on the other
3. Run `python -m scanpi.tools.hackrf.test_offline` — synthesizes an IQ
   tone, builds the flowgraph, runs scheduler ~250 ms, no hardware needed
4. Apply `hackrf_gmrs_frs` preset, watch the Channels tab for live RSSI
5. Key up a GMRS radio on ch 1 / ch 22 — verify OPEN state + WAV recording
6. Check `/tools/hackrf/api/hackrf/health` — `device_present: true`,
   `flowgraph_running: true`
7. Play back a recorded WAV — voice should be intelligible at default
   gain (lna=24, vga=20, amp=0)
