# Heimdall Audio Repair Care Package

Extracted from ScanPi v0.3 (commit c16132b) after a full night of debugging why
GMRS clips were unintelligible and the browser cut them off mid-playback. If
Heimdall's RF bridge has "UHF freqs but none of them are ever playable", the
symptoms overlap — try these fixes in order, top to bottom.

## Symptom → suspected cause → fix

| Symptom | Likely cause | File to apply |
|---|---|---|
| Recorded clips sound like pure hiss; no voice | `analog.nbfm_rx`'s internal squelch at +20 dB never opens on RTL-SDR-normalized signals, gating out the demodulated audio | `fm_demod_rebuild.py` |
| Voice is there but very quiet (−22 dBFS RMS) | `max_dev=5000` in demod gain but FRS/GMRS TX at ±2.5 kHz → half amplitude | `fm_demod_rebuild.py` |
| Distorted peaks on loud voice | Channel filter narrower than FM occupied bandwidth (Carson's rule: 2×(Δf+f_audio) ≈ 16 kHz for GMRS/FRS) | `fm_demod_rebuild.py` |
| Pre-roll or post-TX window full of hiss | No squelch on audio path → noise demod when no signal | `fm_demod_rebuild.py` |
| Browser plays only ~1s of a multi-second WAV | Server doesn't support HTTP Range requests → `<audio>` element bails | `audio_range_serve.py` |
| Clip plays different length on each click | UI refresh loop destroys the `<audio>` element mid-playback | See ScanPi's `page.html` incremental-DOM pattern (not copied here — UI-specific) |
| Some browsers refuse to play at all | Non-standard WAV sample rate (e.g. 10 kHz) | Use `audio_rate=16000` or 22050 Hz |

## Files in this package

### `fm_demod_rebuild.py`
Drop-in FM demod builder. Replace any `analog.nbfm_rx(...)` call in a GR
flowgraph with `build_nbfm_chain(top_block, source, sink, channel_rate,
audio_rate, squelch_db=-45, max_dev=2500)`. Returns list of GR blocks it wired
in, so your code can still hold references if needed.

### `audio_range_serve.py`
Standalone Flask/FastAPI helper `serve_file_with_range(path, media_type,
request)`. Loads the whole file once (WAVs are small), slices for byte-range
requests, deterministic bytes across repeat fetches. Fixes the "browser plays
only 1s" symptom.

## Not copied — ScanPi-specific

- 16 kHz audio rate is a config choice; the right value for Heimdall depends
  on existing capture rates. Anything ≥ 8 kHz that browsers handle is fine.
- Incremental DOM update in the web UI — pattern is in
  `src/scanpi/tools/gmrs/page.html` (see the `existing` Map + `dataset.eventId`
  approach). Apply it anywhere a table of playable clips is rebuilt on a timer.

## Heimdall context

- Bridge = `~/rfforge/bridge_data/` on Spark
- UHF capture configs: `~/rfforge/config/capture_*_uhf.toml`, keeper1
  `capture_keeper1.toml` and keeper2 (dev0 dead per memory)
- Actual FM demod happens in `rfforge.demod.gnuradio_squelch` — worth auditing
  that it's not using `nbfm_rx` with default params
- Bridge transcriber + voice_log_builder consume the WAVs; if they report
  "Thanks for watching!" hallucinations, that's Whisper on noise — root cause
  is the demod producing hiss, not Whisper.
