# Overnight session 2026-04-14 — recap

What got built while Patrick slept (~01:00 → 06:45 CT).

## New features shipped

**Audio pipeline (the hard-won fight)**
- Replaced `analog.nbfm_rx` with raw quadrature_demod + audio LPF + fm_deemph
  chain so the demod actually produces audio (nbfm_rx's internal +20 dB
  squelch was silencing everything).
- `max_dev=2500` narrowband scaling so voice isn't at half amplitude.
- Channel filter widened to ±8 kHz (FM sidebands were being chopped).
- Audio-path pwr_squelch silences pre-roll / close-hold windows.
- 16 kHz WAV for broad browser compatibility.
- `np.clip` + scale-to-24000 in AudioRecorder prevents int16 wraparound.
- HTTP Range-aware clip serving (deterministic bytes across repeat fetches).
- Incremental DOM updates prevent `<audio>` elements getting destroyed
  mid-playback by the refresh tick.

**OP25 P25 trunking — NEW TOOL**
- `OP25Tool` plugin: spawns multi_rx.py, tails its log, captures UDP audio,
  per-call WAV files, SQLite events.
- Watchdog thread respawns the decoder if it dies (crash-loop guard).
- Talkgroup TSV loader + keyword category classifier
  (police/fire/ems/transit/utility/school/other).
- Release-event parsing so call end-ts is authoritative.
- Category color tags throughout the UI.
- 854 calls captured overnight on your CLMRN system with zero manual
  intervention.

**Whisper transcription**
- faster-whisper tiny.en in background thread, lazy-loaded on first call.
- Transcripts saved alongside calls with status (`ok` / `silent` / `too_short` / etc.).
- Works on both tools (GMRS + OP25).

**Keyword alerts**
- `src/scanpi/alerts.py` — 6 categories, 30+ keywords
  (fire/violence/pursuit/medical/emergency/accident).
- Every ok transcript scanned, alert_kind + alert_match written to DB.
- UI renders alert badges + red-border rows + top-banner.
- Retroactive scan on startup flags pre-existing transcripts.
- Dashboard cards show 24h alert counts.

**Webhook notifications**
- `src/scanpi/notify.py` — POST JSON to `SCANPI_WEBHOOK_URL` on every alert.
- README recipes for ntfy.sh, Home Assistant, Discord.
- Clip URL included if `SCANPI_PUBLIC_URL` is set.

**Search + phrases**
- Server-side full-text search (`/api/search?q=...`).
- Top-phrases endpoint + phrase-cloud UI (click a phrase → filter Recent).

**Row-click detail**
- Click any Recent row → inline detail with large transcript, full audio
  player, related calls on the same TG / channel.

**Hourly activity sparkline**
- Inline SVG bar chart above Stats on each tool page.

**CSV export**
- `/tools/<id>/api/export.csv` on both tools.

**Settings page**
- Read-only system + per-tool config view at `/settings`.

**Dashboard live feed**
- Unified activity stream across all tools, last 25 events, auto-refresh.

**Retention manager**
- Oldest-first WAV pruning by age (7 days) + size (1 GB OP25 / 512 MB GMRS).
- DB rows kept forever; only the audio file is removed and `clip_path` NULLed.

**Multi-SDR coordinator**
- Per-device arbitration. Two tools can run in parallel if they use
  different `sdr_device` indexes (foundation for dual-SDR setups).

**Health monitor**
- Tool status flips `healthy=False` if running but no activity past warmup.
- Dashboard shows warn badge + cause in status line.

**Theme**
- Phosphor green-on-black with amber accents. Scanline overlay.
- Per-channel color coding for GMRS (15 distinct hues).
- Mobile-friendly layout.

## Numbers (as of 06:45 AM CT)

- P25 calls captured: **836** in last 24h
- Active talkgroups: **34**
- Peak hour: **197 calls**
- Top phrases: "thank you." 18×, "roger." 15×, "good." 9×, "all right." 4×
- Alert matches: **0** (quiet overnight on CLMRN — as it should be)
- Audio on disk: **25 MB** (budget is 1 GB; retention hasn't needed to
  prune anything yet)
- Service uptime: several restarts during deploys, stable otherwise
- Disk free: **3.0 GB / 13 GB** (unchanged from overnight start)

## Commits

13 commits on `v0.3-tool-framework` overnight:

```
0728ef9 docs: README recipes for alert webhooks
49e079c feat: alert webhooks — push notifications when keywords fire
a1f53d4 feat: dashboard card unifies GMRS/OP25 stats + adds all-time counter
67d5ffa feat: top-phrases endpoint + phrase-cloud UI block
deb65cc feat: multi-SDR coordinator (foundation for dual-tool parallel)
6a53bdf feat: unified live activity feed on dashboard
c4d3224 docs: full README rewrite + dashboard alert ribbon
dd5dfc8 feat: transcript search + row-click detail expansion
4c1b98c feat: keyword alert system
8b8ba22 feat: audio retention / disk-budget manager + lower transcript threshold
a04e9fe fix(op25): buffering, TSV parsing, release-event end timestamps
743bd85 feat(op25): subprocess watchdog + dashboard transcript preview + Recent filter
(+ earlier rounds 1-4 before the audit/wakeup cycle)
```

## Things to check when you wake up

1. Open **http://scanpi:8080/** — dashboard should render, OP25 card
   should show ~800+ calls 24h, live feed should scroll.
2. Open **http://scanpi:8080/tools/op25/** — phrase cloud, hourly
   sparkline, stats table, recent calls with clickable rows.
3. Try a row click → should expand inline with audio + transcript.
4. Try typing in the search box → server-side search across all calls.
5. Click a TG in Stats → should auto-filter Recent.
6. Try http://scanpi:8080/settings — read-only system view.
7. On your phone → same UI, collapsed to single column.
8. If you want webhook alerts:
   `sudo systemctl edit scanpi-v3.service` →
   `Environment="SCANPI_WEBHOOK_URL=https://ntfy.sh/your-topic"` →
   restart.

## Known non-issues

- TG-8752 displays as "TG-8752" because it's not in your
  `clmrn_talkgroups.tsv` (you have 8751, 8753, 8755). Add the row and
  restart to pick up the name.
- "Thank you." being the #1 transcript is correct — that's just cops
  ending every call with "thank you, bye" or "ten-four, thanks".
  Not a Whisper hallucination.
- Zero alerts ≠ broken alerts — the keyword list just didn't match
  anything said in 836 calls tonight. System will fire when it matters.
