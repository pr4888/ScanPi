"""REST API — FastAPI endpoints for web UI and external access."""
from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import ScanConfig
from .db import ScanPiDB

STATIC_DIR = Path(__file__).parent / "web" / "static"


def create_app(cfg: ScanConfig, db: ScanPiDB, scanner=None, surveyor=None,
               transcriber=None, trunking=None, storage=None,
               op25_bridge=None) -> FastAPI:
    app = FastAPI(title="ScanPi", version="0.1.0")

    # Serve static files
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # --- Dashboard ---

    @app.get("/", response_class=HTMLResponse)
    async def index():
        index_path = Path(__file__).parent / "web" / "index.html"
        return index_path.read_text()

    @app.get("/api/status")
    async def status():
        stats = db.get_stats()
        current_freq = scanner.current_freq if scanner else None
        storage_info = storage.get_usage() if storage else {}
        return {
            "scanner_active": scanner._running if scanner else False,
            "current_freq_hz": current_freq,
            "current_freq_mhz": current_freq / 1e6 if current_freq else None,
            **stats,
            "storage": storage_info,
            "uptime_s": time.time() - app.state.start_time if hasattr(app.state, "start_time") else 0,
        }

    @app.on_event("startup")
    async def startup():
        app.state.start_time = time.time()

    # --- Talkgroups & Calls (P25 trunking — the main experience) ---

    @app.get("/api/talkgroups")
    async def list_talkgroups():
        if not op25_bridge:
            return {"talkgroups": [], "count": 0}
        tgs = op25_bridge.get_talkgroup_summary()
        return {"talkgroups": tgs, "count": len(tgs)}

    @app.get("/api/calls")
    async def list_calls(
        limit: int = 50,
        tgid: int | None = None,
        category: str | None = None,
        search: str | None = None,
    ):
        if not op25_bridge:
            return {"calls": [], "count": 0}
        calls = op25_bridge.get_recent_calls(limit=limit, tgid=tgid, category=category)
        if search:
            calls = [c for c in calls if search.lower() in (c.get("transcript") or "").lower()
                     or search.lower() in (c.get("tg_name") or "").lower()]
        return {"calls": calls, "count": len(calls)}

    @app.get("/api/calls/active")
    async def active_calls():
        if not op25_bridge:
            return {"calls": []}
        return {"calls": op25_bridge.get_active_calls()}

    @app.get("/api/calls/{call_id}/audio")
    async def call_audio(call_id: int):
        if not op25_bridge:
            raise HTTPException(503)
        calls = op25_bridge.get_recent_calls(limit=10000)
        match = [c for c in calls if c["id"] == call_id]
        if not match or not match[0].get("filepath"):
            raise HTTPException(404, "Audio not available")
        filepath = Path(match[0]["filepath"])
        if not filepath.exists():
            raise HTTPException(404, "Audio file not found")
        return FileResponse(str(filepath), media_type="audio/wav")

    # --- Frequencies ---

    @app.get("/api/frequencies")
    async def list_frequencies(
        mode: str | None = None,
        enabled: bool = False,
        min_score: float = 0,
    ):
        freqs = db.get_frequencies(enabled_only=enabled, mode=mode, min_score=min_score)
        return {"frequencies": freqs, "count": len(freqs)}

    @app.get("/api/frequencies/{freq_hz}")
    async def get_frequency(freq_hz: int):
        freqs = db.get_frequencies()
        match = [f for f in freqs if f["freq_hz"] == freq_hz]
        if not match:
            raise HTTPException(404, "Frequency not found")
        return match[0]

    @app.post("/api/frequencies/{freq_hz}/label")
    async def label_frequency(freq_hz: int, request: Request):
        body = await request.json()
        label = body.get("label", "")
        db.label_frequency(freq_hz, label)
        return {"ok": True}

    @app.post("/api/frequencies/{freq_hz}/toggle")
    async def toggle_frequency(freq_hz: int):
        freqs = db.get_frequencies()
        match = [f for f in freqs if f["freq_hz"] == freq_hz]
        if not match:
            raise HTTPException(404)
        current = match[0].get("enabled", True)
        with db.cursor() as c:
            c.execute("UPDATE frequencies SET enabled = ? WHERE freq_hz = ?",
                      (not current, freq_hz))
        return {"enabled": not current}

    # --- Recordings ---

    @app.get("/api/recordings")
    async def list_recordings(
        freq_id: int | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        recs = db.get_recordings(freq_id=freq_id, limit=limit, offset=offset, search=search)
        return {"recordings": recs, "count": len(recs)}

    @app.get("/api/recordings/{rec_id}/audio")
    async def get_audio(rec_id: int):
        recs = db.get_recordings(limit=10000)
        match = [r for r in recs if r["id"] == rec_id]
        if not match:
            raise HTTPException(404)
        filepath = Path(match[0]["filepath"])
        if not filepath.exists():
            raise HTTPException(404, "Audio file not found")
        return FileResponse(str(filepath), media_type="audio/wav")

    # --- Favorites ---

    @app.get("/api/favorites")
    async def list_favorites(category: str | None = None):
        favs = db.get_favorites(category=category)
        return {"favorites": favs, "count": len(favs)}

    @app.post("/api/favorites")
    async def add_favorite(request: Request):
        body = await request.json()
        freq_hz = body.get("freq_hz")
        name = body.get("name", "")
        if not freq_hz or not name:
            raise HTTPException(400, "freq_hz and name required")
        fav_id = db.add_favorite(
            freq_hz=freq_hz,
            name=name,
            category=body.get("category", "other"),
            color=body.get("color"),
            priority=body.get("priority", 0),
            alert_keywords=body.get("alert_keywords", ""),
            notes=body.get("notes", ""),
        )
        return {"id": fav_id, "ok": True}

    @app.put("/api/favorites/{fav_id}")
    async def update_favorite(fav_id: int, request: Request):
        body = await request.json()
        db.update_favorite(fav_id, **body)
        return {"ok": True}

    @app.delete("/api/favorites/{fav_id}")
    async def delete_favorite(fav_id: int):
        db.delete_favorite(fav_id)
        return {"ok": True}

    # --- Channels (summary view) ---

    @app.get("/api/channels")
    async def list_channels():
        """Active channels with recording counts — the main useful view."""
        channels = db.get_channel_summary()
        return {"channels": channels, "count": len(channels)}

    @app.get("/api/channels/{freq_id}/recordings")
    async def channel_recordings(freq_id: int, limit: int = 50):
        recs = db.get_recordings(freq_id=freq_id, limit=limit)
        return {"recordings": recs, "count": len(recs)}

    @app.post("/api/recordings/{rec_id}/transcribe")
    async def transcribe_recording(rec_id: int):
        """On-demand transcription of a specific recording."""
        if not transcriber:
            raise HTTPException(503, "Transcriber not available")
        recs = db.get_recordings(limit=10000)
        match = [r for r in recs if r["id"] == rec_id]
        if not match:
            raise HTTPException(404)
        result = await transcriber.transcribe_file(match[0]["filepath"])
        if result:
            text, confidence = result
            keywords = transcriber._extract_keywords(text)
            db.set_transcript(rec_id, text, confidence, keywords)
            return {"transcript": text, "confidence": confidence, "keywords": keywords}
        raise HTTPException(500, "Transcription failed")

    # --- Activity ---

    @app.get("/api/activity")
    async def activity_log(
        limit: int = 100,
        event_type: str | None = None,
    ):
        with db.cursor() as c:
            q = "SELECT * FROM activity_log WHERE 1=1"
            params = []
            if event_type:
                q += " AND event_type = ?"
                params.append(event_type)
            q += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)
            c.execute(q, params)
            rows = [dict(r) for r in c.fetchall()]
        return {"events": rows, "count": len(rows)}

    # --- Scanner Control ---

    @app.post("/api/scanner/scan-now")
    async def scan_frequency(request: Request):
        """Manually tune to a frequency."""
        body = await request.json()
        freq_hz = body.get("freq_hz")
        if not freq_hz or not scanner:
            raise HTTPException(400)
        # Add to catalog if not exists
        freq_id = db.upsert_frequency(freq_hz, -50)
        freq_info = {"id": freq_id, "freq_hz": freq_hz, "activity_score": 0.5,
                     "mode": "analog_fm", "label": None}
        import asyncio
        asyncio.create_task(scanner._dwell_on(freq_hz, 10, freq_info))
        return {"ok": True, "freq_hz": freq_hz}

    @app.post("/api/survey/run")
    async def trigger_survey():
        """Manually trigger a full survey."""
        if not surveyor:
            raise HTTPException(503)
        import asyncio
        asyncio.create_task(surveyor.full_survey())
        return {"ok": True, "message": "Survey started"}

    # --- Trunking ---

    @app.get("/api/trunking/status")
    async def trunking_status():
        if not trunking:
            return {"available": False}
        return {"available": True, **trunking.get_status()}

    @app.post("/api/trunking/discover")
    async def discover_trunking():
        """Scan for P25 control channels."""
        if not trunking:
            raise HTTPException(503, "Trunking not available")
        import asyncio
        channels = await trunking.discover_control_channels()
        if channels:
            trunking.generate_op25_config(channels)
        return {"control_channels": channels, "count": len(channels)}

    @app.post("/api/trunking/start")
    async def start_trunking():
        if not trunking:
            raise HTTPException(503)
        import asyncio
        asyncio.create_task(trunking.start_op25())
        return {"ok": True}

    @app.post("/api/trunking/stop")
    async def stop_trunking():
        if not trunking:
            raise HTTPException(503)
        await trunking.stop_op25()
        return {"ok": True}

    # --- Coalesce ---

    @app.post("/api/frequencies/coalesce")
    async def coalesce():
        """Merge adjacent bins into channels."""
        from .coalesce import coalesce_frequencies, auto_label_channels
        channels = coalesce_frequencies(db)
        auto_label_channels(db)
        return {"channels": channels}

    # --- Settings ---

    @app.get("/api/settings")
    async def get_settings():
        return {
            "sdr_device": cfg.sdr_device,
            "sdr_gain": cfg.sdr_gain,
            "sdr_ppm": cfg.sdr_ppm,
            "survey_interval_min": cfg.survey_interval_min,
            "detection_threshold_db": cfg.detection_threshold_db,
            "dwell_time_s": cfg.dwell_time_s,
            "vad_enabled": cfg.vad_enabled,
            "vad_threshold": cfg.vad_threshold,
            "transcribe_enabled": cfg.transcribe_enabled,
            "transcribe_model": cfg.transcribe_model,
            "retention_days": cfg.retention_days,
            "max_storage_gb": cfg.max_storage_gb,
            "bands": [
                {"name": b.name, "start_mhz": b.start_mhz,
                 "end_mhz": b.end_mhz, "enabled": b.enabled}
                for b in cfg.survey_bands
            ],
        }

    @app.post("/api/settings")
    async def update_settings(request: Request):
        body = await request.json()
        if "sdr_gain" in body:
            cfg.sdr_gain = str(body["sdr_gain"])
        if "sdr_ppm" in body:
            cfg.sdr_ppm = int(body["sdr_ppm"])
        if "detection_threshold_db" in body:
            cfg.detection_threshold_db = float(body["detection_threshold_db"])
        if "dwell_time_s" in body:
            cfg.dwell_time_s = float(body["dwell_time_s"])
        if "vad_enabled" in body:
            cfg.vad_enabled = bool(body["vad_enabled"])
        if "vad_threshold" in body:
            cfg.vad_threshold = float(body["vad_threshold"])
        if "transcribe_enabled" in body:
            cfg.transcribe_enabled = bool(body["transcribe_enabled"])
        if "retention_days" in body:
            cfg.retention_days = int(body["retention_days"])
        if "max_storage_gb" in body:
            cfg.max_storage_gb = float(body["max_storage_gb"])
        cfg.save()
        return {"ok": True}

    return app
