"""FastAPI web app for the GMRS monitor — runs on a separate port from main ScanPi."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from .service import GmrsService

log = logging.getLogger(__name__)


def create_app(service: GmrsService) -> FastAPI:
    app = FastAPI(title="ScanPi GMRS Monitor")
    web_file = Path(__file__).parent / "web.html"

    @app.get("/", response_class=HTMLResponse)
    def index():
        return web_file.read_text(encoding="utf-8")

    @app.get("/api/live")
    def live():
        return service.snapshot()

    @app.get("/api/stats")
    def stats(hours: float = 24.0):
        return {"hours": hours, "channels": service.stats(hours)}

    @app.get("/api/recent")
    def recent(limit: int = 50):
        return {"events": service.recent(limit)}

    @app.get("/api/health")
    def health():
        return {"status": "ok", "channels": len(service.live),
                "gateway_enabled": bool(service.gateway_url and service.gateway_token)}

    @app.get("/api/clip/{event_id}")
    def clip(event_id: int):
        path = service.clip_path(event_id)
        if not path:
            raise HTTPException(status_code=404, detail="clip not found")
        return FileResponse(path, media_type="audio/wav",
                            filename=Path(path).name)

    return app
