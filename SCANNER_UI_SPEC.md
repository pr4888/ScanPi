# Scanner Web UI Spec - Research & Design

## Research Findings

### 1. Rdio Scanner (chuot/rdio-scanner)

The gold standard for scanner web UIs. Mimics a physical scanner display.

**Layout**: Three zones stacked vertically:
- **LED indicator** at top -- solid green when audio playing, blinks when paused, yellow for archive playback. Color customizable per system or talkgroup.
- **Multi-line display** showing (top to bottom):
  - Current time + queue count ("Q: 27")
  - System label + talkgroup tag
  - Talkgroup name + recording timestamp
  - Frequency, TGID
  - Error counts + unit IDs
  - 5-item call history at bottom
- **Button bar** along the bottom (mimics physical scanner buttons):
  - LIVE FEED (on/partial/off) -- partial means only some groups selected
  - HOLD SYS / HOLD TG -- locks to current system or talkgroup
  - REPLAY LAST -- press repeatedly to go back through history (1x = current, 3x = third item)
  - SKIP NEXT -- advances queue
  - AVOID -- escalating: tap once = immediate, again = 30min, again = 60min, again = 120min
  - SEARCH CALL -- opens archive browser
  - PAUSE -- suspends queue without clearing
  - SELECT TG -- opens group/system selection panel

**Audio**: Auto-plays queued calls in sequence. Queue fills as new calls arrive. No manual play buttons needed in live mode -- it just scans like a real scanner.

**Organization**: Systems > Groups > Talkgroups. Three-state toggles: all on, all off, partial. Bulk select/deselect per group.

**Search/Archive**: Filter by date, system, talkgroup, group, tags, sort order. Toggle between play buttons and download buttons. Paginated.

**Key insight**: The display IS the scanner. It doesn't look like a web app -- it looks like a scanner face. One call at a time, sequential playback, queue-based.

---

### 2. OpenMHz

**Layout**: System-centric. Browse systems by name/location. Select a system, see its recent calls.

**Call list**: Reverse chronological. Each call shows:
- Talkgroup name (human readable, e.g., "Groton Fire Dispatch")
- Timestamp (relative: "2 min ago")
- Duration
- Play button inline

**Audio**: Click play on any call in the list. Standard browser audio controls. No auto-scan -- it's an archive browser, not a live scanner.

**Organization**: By radio system (geographic). Within a system, calls are flat-listed with talkgroup labels.

**Key insight**: Simple and clean. A person finds their local system, sees recent calls, plays what interests them. No configuration needed. The talkgroup NAME is the primary identifier, not the frequency or TGID.

---

### 3. Broadcastify Calls

**Layout**: Dashboard-oriented with real-time statistics.

**Features**:
- Real-time metrics: calls/minute, active listeners, online nodes
- Playlists (My Playlists, Public Playlists) -- curated sets of talkgroups
- Coverage browser for geographic discovery
- Transcription with 99.98% success rate displayed
- Call deduplication (accepted vs rejected stats)

**Key insight**: Playlists are the killer feature -- a curated "Groton Fire + Police" playlist that someone can share. Geographic discovery helps new users find what's near them.

---

### 4. SDRTrunk

**Layout**: Desktop application with multiple panels:
- **Spectral display** -- frequency waterfall (developer/power-user feature)
- **Now Playing table** -- the core. Each row shows:
  - Status: ACTIVE / CALL / CONTROL / DATA / ENCRYPTED / FADE / IDLE / RESET / TEARDOWN
  - Decoder type (P25, DMR, etc.)
  - FROM (unit ID with alias) / TO (talkgroup with alias)
  - Channel number
  - Frequency
  - Channel name (user-assigned or "TRAFFIC")
- **Detail tabs when a channel is selected**:
  - Details: system/site/channel summary
  - Events: list of events on that channel
  - Messages: decoded protocol messages with timestamps
  - Channel: signal quality (power dB meter, peak levels, squelch)
- **Audio**: Per-channel mute/volume controls

**Key insight**: The Now Playing table with FROM/TO aliases is how professionals monitor. Status indicators (ACTIVE/ENCRYPTED/etc.) let you see system health at a glance. But this is a power-user tool -- too much info for casual listeners.

---

### 5. Physical Scanners (Uniden SDS100/SDS200, Whistler TRX-1)

**Display during scanning** (what you see at a glance):
- **Department/Agency name** (biggest text): "GROTON FIRE"
- **Talkgroup name**: "DISPATCH"
- **System name**: smaller, above department
- **Frequency**: shown but not prominent
- **Signal strength bars**: S-meter
- **Mode indicator**: P25, DMR, FM
- **Favorites List name**: which list is active (e.g., "CT Coast")

**Physical controls** (buttons on the device):
- **SCAN** -- start/stop scanning
- **HOLD** -- lock on current channel
- **L/O (Lockout)** -- same as AVOID, skip this channel/talkgroup
- **FUNC + L/O** -- temporary lockout
- **REPLAY** -- instant replay of last transmission (SDS100 keeps buffer)
- **PRIORITY** -- always check this channel, even while scanning others
- **WEATHER** -- jump to NOAA weather
- **CLOSE CALL** -- detect nearby strong signals regardless of programming

**Favorites Lists**: The primary organization unit. Users create lists like:
- "Local Fire/EMS"
- "State Police"
- "Marine"
- "Airport"

Each list contains departments, and each department contains channels/talkgroups. This is how 99% of scanner users think -- NOT by frequency.

**Key insight**: The department/agency name is the HERO text. Frequency is secondary. The scanner cycles through channels automatically and stops when it hears something. The user's primary interaction is HOLD (stay here) and LOCKOUT (skip this). Everything else is automatic.

---

## Synthesis: What a Scanner Web UI MUST Have

### The Core Mental Model

A scanner user does NOT think in frequencies. They think in:
1. **"Who is talking?"** -- Groton Fire, State Police, Coast Guard
2. **"What are they saying?"** -- the audio/transcript
3. **"When?"** -- how recent
4. **"Is this interesting?"** -- hold it, or skip it

The UI must serve this mental model, not a developer's mental model of SDR parameters.

---

### Minimum Viable Scanner UI

#### PRIMARY VIEW: "The Scanner Face"

This is the default view. It should feel like looking at a scanner, not a dashboard.

```
+----------------------------------------------------------+
|  [LED]  SCANNING            Groton Fire Dispatch    [HOLD]|
|                                                           |
|     GROTON FIRE DISPATCH                                  |
|     (big hero text, color-coded by category)              |
|                                                           |
|     151.2950 MHz  FM        2s ago                        |
|     "Engine 4 responding to Main Street..."               |
|     (transcript, if available)                            |
|                                                           |
|  [=====progress bar======]  0:04 / 0:12                  |
|                                                           |
|  [LIVE] [HOLD] [SKIP] [REPLAY] [AVOID]   Q:3             |
|                                                           |
|  --- Recent Calls ---                                     |
|  12:34  Groton Fire Dispatch     "Engine 4 respond..."  5s|
|  12:31  State Police Troop E     "10-4 copy that..."    3s|
|  12:28  Groton Fire Ops          "On scene, single..."  8s|
|  12:25  Marine Ch16              "Securite securit..."  12s|
|  12:20  Groton Fire Dispatch     "Medical call at..."   4s|
+----------------------------------------------------------+
```

**Elements**:
- **LED indicator**: Green = playing audio. Blinking = paused. Off = idle/no traffic.
- **Status line**: "SCANNING" / "HOLDING: Groton Fire" / "PAUSED"
- **Hero text**: Department/talkgroup name. LARGE. Color-coded border (red=fire, blue=police, cyan=marine, etc.)
- **Subtext**: Frequency, mode, time since call
- **Transcript**: If available, show first line. Real-time if using streaming transcription.
- **Progress bar**: Audio playback progress with time.
- **Button bar**: LIVE (toggle auto-scan), HOLD (lock channel), SKIP, REPLAY, AVOID
- **Recent calls list**: 5-10 most recent calls. Click any to play. Shows: time, channel name, transcript snippet, duration.
- **Queue count**: "Q:3" -- how many calls are waiting

**Behavior**:
- In LIVE mode, calls auto-play sequentially as they arrive (like a real scanner)
- HOLD locks to the current talkgroup/channel -- only plays calls from that source
- SKIP advances to next queued call
- REPLAY replays current call, press again for previous
- AVOID temporarily skips this channel (escalating: 30m, 60m, 120m, permanent)

#### SECONDARY VIEW: "Channel List"

Accessed via a tab or drawer. This is where you browse and configure.

```
+----------------------------------------------------------+
|  Search: [______________]    [All] [Fire] [Police] [Marine]|
|                                                           |
|  FAVORITES (pinned to top)                                |
|  * Groton Fire Dispatch    151.2950  12 calls  2m ago     |
|  * State Police Troop E    TG 8851   8 calls   5m ago     |
|  * Marine Ch16             156.8000  3 calls   15m ago    |
|                                                           |
|  ALL CHANNELS                                             |
|    Groton Fire Ops         151.2950  4 calls   22m ago    |
|    Waterford Fire          154.0625  1 call    45m ago    |
|    USCG Ch22A              157.1000  2 calls   1h ago     |
|    ...                                                    |
+----------------------------------------------------------+
```

**Per channel row** (at a glance, no click needed):
- Star/favorite toggle (filled = favorite)
- Channel NAME (human readable, prominent)
- Frequency or TGID (secondary, monospace)
- Category badge (color-coded: Fire, Police, Marine, etc.)
- Call count (last 24h)
- Last active (relative time: "2m ago")
- Activity indicator (green dot = heard something in last 5 min)

**Favorites always sort to top**. This is critical -- the user's curated list is their scanner programming.

**Click a channel** to:
- See recent recordings with audio players
- See transcript history
- Toggle favorite
- Set priority (scans this channel more often)
- Set AVOID (temporary lockout)

#### TERTIARY VIEW: "Call History / Search"

Full searchable archive of all recordings.

```
+----------------------------------------------------------+
|  Search transcripts: [mayday_________]  [Search]          |
|  Filter: [All Categories v]  [Last 24h v]                 |
|                                                           |
|  Mar 26 12:34  Groton Fire Dispatch  151.2950  5.2s       |
|    "Engine 4 responding to 42 Main Street, structure fire" |
|    [PLAY] [Download]                                      |
|                                                           |
|  Mar 26 12:31  State Police Troop E  TG 8851   3.1s       |
|    "10-4 copy that, en route to exit 87"                   |
|    [PLAY] [Download]                                      |
|  ...                                                      |
+----------------------------------------------------------+
```

---

### Controls Spec

| Control | Behavior | Visual |
|---------|----------|--------|
| **LIVE** | Toggle auto-scan on/off. When on, new calls auto-play in sequence. | Lit green when active |
| **HOLD** | Lock to current channel. Only plays calls from this source. Press again to release. | Lit yellow when holding |
| **SKIP** | Stop current audio, advance to next in queue. | Momentary |
| **REPLAY** | Replay current call. Press again = previous call. Press 3x = 3rd most recent. | Momentary |
| **AVOID** | Temporarily skip this channel. Escalating: tap 1 = 30min, tap 2 = 60min, tap 3 = 120min, tap 4 = permanent. Shows countdown. | Red when active, shows remaining time |
| **PRIORITY** | (In channel list) Mark channel as priority -- checked between every other channel scan. | Star changes to exclamation mark |

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| Space | Play/Pause |
| H | Hold |
| S | Skip |
| R | Replay |
| A | Avoid |
| L | Toggle Live |
| 1-9 | Play Nth recent call |
| / | Focus search |
| Esc | Close panel / release hold |

---

### What the Current ScanPi UI Gets Wrong

Looking at the existing `index.html`:

1. **Channels page is the default** -- shows a developer dashboard (stats cards, channel grid with freq_hz, recording counts, storage used). A scanner user does not care about storage or total recording count on the main screen.

2. **Frequency is the primary identifier** -- channel rows lead with `freq_hz.toFixed(4)`. Real scanners lead with the DEPARTMENT NAME.

3. **No auto-play / live scanning feel** -- there's no queue, no sequential playback, no "scanner is running" feel. You have to click into a channel, find a recording, and click play. That's an archive browser, not a scanner.

4. **Scanner page is a frequency tuner** -- shows a big frequency display with a "Tune" input and "Survey" / "Coalesce" buttons. These are developer/SDR-engineer controls. A normal person has no idea what coalesce means.

5. **No HOLD / SKIP / AVOID / REPLAY** -- the fundamental scanner interactions are missing entirely.

6. **Settings page exposes SDR internals** -- Gain, PPM, Detection Threshold, Dwell Time, VAD Threshold. These should be hidden or in an "Advanced" section. A normal user should never see PPM.

7. **No live status feel** -- the LED dot is tiny and buried in the nav. Rdio Scanner makes the LED a prominent element that gives immediate visual feedback.

8. **Recordings page is a flat search** -- no context about which channel, no inline playback with visual waveform, just a text search box.

---

### Recommended Architecture

**Three modes, one screen:**

1. **SCANNER MODE** (default, fills the screen)
   - The scanner face. LED, hero text, controls, recent call list.
   - This is 90% of what users interact with.
   - Auto-plays audio. Cycles through channels.
   - Mobile-first (works on a phone propped up on a desk).

2. **CHANNELS** (slide-out drawer or second tab)
   - Browse/search channels. Favorites pinned top. Category filters.
   - Configure favorites, priority, avoid.
   - See per-channel recording history.

3. **HISTORY** (third tab)
   - Full transcript search across all channels.
   - Date range filter.
   - Export/download.

**Settings** should be a gear icon that opens a modal, NOT a full page. Advanced SDR settings hidden behind an "Advanced" toggle.

---

### Data Model Requirements

Each "call" (recording) needs:

| Field | Source | Display |
|-------|--------|---------|
| channel_name | Favorites DB or auto-classify | "Groton Fire Dispatch" |
| category | Favorites DB or auto-classify | fire, police, ems, marine, weather, other |
| frequency_hz | SDR | 151295000 (display as 151.2950 MHz) |
| talkgroup_id | P25/trunking decoder | 8851 (if trunked) |
| timestamp | Recording time | Relative ("2m ago") and absolute |
| duration_s | Audio file | 5.2 |
| transcript | Whisper | "Engine 4 responding..." |
| audio_url | Storage | /api/recordings/{id}/audio |
| source_unit | P25 decoder (if available) | Unit ID of transmitter |
| is_emergency | P25 flag or keyword detect | Red highlight |
| is_encrypted | P25 flag | Grey out, show lock icon |

---

### Visual Design Principles

1. **Dark theme mandatory** -- scanners are used in dark rooms, cars, at night
2. **Category colors are consistent everywhere** -- Fire=red, Police=blue, EMS=orange, Marine=cyan, Weather=green
3. **Monospace for frequencies** -- but frequencies are SECONDARY to names
4. **LED indicator is prominent** -- not a tiny dot, a real visible indicator
5. **Minimal chrome** -- the scanner face should be mostly dark with the channel name glowing
6. **Mobile-responsive** -- many scanner listeners use phones/tablets
7. **Touch-friendly buttons** -- HOLD, SKIP, etc. should be large enough to tap
8. **Queue count visible** -- users want to know if they're missing calls

### Priority Order for Implementation

1. Scanner face with auto-play queue (this IS the product)
2. Recent calls list with one-tap replay
3. Favorites with human-readable names
4. HOLD / SKIP / AVOID controls
5. Category filtering
6. Transcript display (inline, not separate page)
7. Search across transcripts
8. Keyboard shortcuts
9. REPLAY (buffer of last N calls)
10. Avoid with escalating timeouts
11. Priority channel scanning
12. Settings (hidden behind gear icon)
