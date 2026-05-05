"""FastAPI APIRouter for HackrfTool.

Endpoints (all relative to /tools/hackrf/api/):

    GET  /profile               current loaded profile (TOML text + parsed)
    POST /profile               upload TOML body, validate, save, restart
    POST /profile/preset        install a preset by name and load it
    GET  /channels              live channel grid
    GET  /events                recent tx_events (filter by since/channel)
    GET  /event/{event_id}      single event detail
    GET  /audio/{event_id}      stream WAV with HTTP range support
    GET  /clip/{event_id}       alias for /audio (matches GMRS pattern)
    GET  /presets               list bundled preset TOMLs
    GET  /hackrf/health         device + flowgraph health
    GET  /summary               dashboard widget data
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response

from .profiles import (
    install_preset,
    list_presets,
    list_user_profiles,
    load_profile,
    parse_text,
    presets_dir,
    save_profile,
    user_profiles_dir,
)

if TYPE_CHECKING:
    from . import HackrfTool

log = logging.getLogger(__name__)


def _serve_file_with_range(path: str, media_type: str, request: Request) -> Response:
    """Range-supported file serve (copy of the GMRS helper)."""
    with open(path, "rb") as f:
        data = f.read()
    file_size = len(data)
    range_header = request.headers.get("range")
    if not range_header:
        return Response(
            content=data,
            media_type=media_type,
            headers={
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
                "Cache-Control": "no-cache",
            },
        )
    try:
        units, rng = range_header.split("=", 1)
        start_s, end_s = rng.split("-", 1)
        if start_s == "" and end_s != "":
            length = int(end_s)
            start = max(0, file_size - length)
            end = file_size - 1
        else:
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else file_size - 1
    except Exception:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})
    if start >= file_size or end >= file_size or start > end:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})
    length = end - start + 1
    return Response(
        content=data[start:end + 1],
        status_code=206,
        media_type=media_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(length),
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-cache",
        },
    )


def _parse_since(since: str | None) -> float:
    """'24h', '60m', '3600' (seconds), or None -> unix ts cutoff."""
    if not since:
        return 0.0
    s = since.strip().lower()
    try:
        if s.endswith("h"):
            return time.time() - float(s[:-1]) * 3600
        if s.endswith("m"):
            return time.time() - float(s[:-1]) * 60
        if s.endswith("s"):
            return time.time() - float(s[:-1])
        # bare number: seconds-ago
        return time.time() - float(s)
    except ValueError:
        return 0.0


def _profile_to_payload(prof) -> dict:
    return {
        "id": prof.sdr.id,
        "driver": prof.sdr.driver,
        "serial": prof.sdr.serial,
        "center_hz": prof.sdr.center_hz,
        "sample_rate": prof.sdr.sample_rate,
        "gain": prof.sdr.gain,
        "fake_iq": bool(prof.sdr.fake_iq_path),
        "channelizer": {
            "type": prof.channelizer.type,
            "num_chans": prof.channelizer.num_chans,
            "channel_bw_hz": prof.channel_bw_hz,
            "attenuation_db": prof.channelizer.attenuation_db,
        },
        "channels": [
            {
                "name": c.name,
                "freq_hz": c.freq_hz,
                "demod": c.demod,
                "bw_hz": c.bw_hz,
                "squelch_db": c.squelch_db,
                "deemph_us": c.deemph_us,
                "output_index": c.output_index,
                "bin_offset_hz": round(c.bin_offset_hz, 1),
                "notes": c.notes,
            }
            for c in prof.channels
        ],
        "source_path": str(prof.source_path) if prof.source_path else None,
    }


def _hackrf_health() -> dict:
    """Best-effort device + USB topology check.

    Returns:
      {
        "device_present": bool,
        "device_info": str | None,
        "usb_topology": str | None,
        "lsusb_available": bool,
      }
    """
    out: dict = {
        "device_present": False,
        "device_info": None,
        "usb_topology": None,
        "lsusb_available": False,
    }
    # hackrf_info if available
    try:
        proc = subprocess.run(
            ["hackrf_info"], capture_output=True, text=True, timeout=2.0,
        )
        if proc.returncode == 0 and "Found HackRF" in (proc.stdout or ""):
            out["device_present"] = True
            out["device_info"] = proc.stdout.strip()
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        out["device_info"] = "hackrf_info timed out"
    except Exception as e:
        out["device_info"] = f"hackrf_info error: {e}"

    # lsusb -t for topology
    if shutil.which("lsusb"):
        out["lsusb_available"] = True
        try:
            proc = subprocess.run(
                ["lsusb", "-t"], capture_output=True, text=True, timeout=2.0,
            )
            if proc.returncode == 0:
                out["usb_topology"] = proc.stdout
        except Exception:
            pass

    return out


def build_router(tool: "HackrfTool") -> APIRouter:
    r = APIRouter()

    @r.get("/profile")
    def get_profile():
        prof = tool.profile
        if prof is None:
            raise HTTPException(404, "no profile loaded")
        # Best-effort raw TOML if we know the path; else regenerate.
        raw = None
        if prof.source_path and prof.source_path.exists():
            try:
                raw = prof.source_path.read_text(encoding="utf-8")
            except Exception:
                raw = None
        return {
            "profile": _profile_to_payload(prof),
            "toml": raw,
            "running": tool.is_running,
        }

    @r.post("/profile")
    async def post_profile(request: Request):
        # TODO auth — uses scanpi.api._check_token if available
        body = await request.body()
        text = body.decode("utf-8", errors="replace")
        try:
            new_prof = parse_text(text)
        except Exception as e:
            raise HTTPException(400, f"profile invalid: {e}")
        # Save under user profiles dir using the profile's id as filename.
        try:
            dest = user_profiles_dir() / f"{new_prof.sdr.id}.toml"
            new_prof.source_path = dest
            saved = save_profile(new_prof, dest)
        except Exception as e:
            raise HTTPException(500, f"could not save profile: {e}")
        # Reload the saved file (so source_path/round-trip is consistent).
        try:
            reloaded = load_profile(saved)
        except Exception as e:
            raise HTTPException(500, f"saved profile fails reload: {e}")
        try:
            tool._swap_profile(reloaded)
        except Exception as e:
            log.exception("profile swap failed")
            raise HTTPException(500, f"profile swap failed: {e}")
        return {"ok": True, "saved_to": str(saved),
                "profile": _profile_to_payload(reloaded),
                "running": tool.is_running}

    @r.post("/profile/preset")
    def post_preset(name: str = Body(..., embed=True)):
        try:
            dest = install_preset(name)
            prof = load_profile(dest)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        except Exception as e:
            raise HTTPException(400, f"preset load failed: {e}")
        try:
            tool._swap_profile(prof)
        except Exception as e:
            log.exception("profile swap failed")
            raise HTTPException(500, f"profile swap failed: {e}")
        return {"ok": True, "saved_to": str(dest),
                "profile": _profile_to_payload(prof),
                "running": tool.is_running}

    @r.get("/channels")
    def channels():
        prof = tool.profile
        if prof is None:
            return {"channels": [], "running": tool.is_running}
        live = tool.live
        return {
            "running": tool.is_running,
            "center_hz": prof.sdr.center_hz,
            "sample_rate": prof.sdr.sample_rate,
            "num_chans": prof.channelizer.num_chans,
            "channel_bw_hz": prof.channel_bw_hz,
            "channels": [
                {
                    "name": c.name,
                    "freq_hz": c.freq_hz,
                    "demod": c.demod,
                    "bw_hz": c.bw_hz,
                    "squelch_db": c.squelch_db,
                    "output_index": c.output_index,
                    "open": (live.get(c.name).open if c.name in live else False),
                    "last_rssi": (round(live[c.name].last_rssi, 1) if c.name in live else None),
                    "peak_rssi": (round(live[c.name].peak_rssi, 1) if c.name in live else None),
                    "open_since": (live[c.name].open_since if c.name in live else None),
                    "last_activity_ts": (live[c.name].last_event_ts if c.name in live else 0.0),
                    "notes": c.notes,
                }
                for c in prof.channels
            ],
        }

    @r.get("/events")
    def events(since: str = Query("24h"), channel: str | None = Query(None),
               limit: int = Query(50, ge=1, le=2000)):
        since_ts = _parse_since(since)
        rows = tool.db.recent_events(channel=channel, since_ts=since_ts, limit=limit)
        return {"since": since, "channel": channel, "limit": limit, "events": rows}

    @r.get("/event/{event_id}")
    def event_detail(event_id: int):
        e = tool.db.get_event(event_id)
        if not e:
            raise HTTPException(404, "event not found")
        return e

    @r.get("/audio/{event_id}")
    def audio(event_id: int, request: Request):
        e = tool.db.get_event(event_id)
        if not e or not e.get("clip_path") or not os.path.exists(e["clip_path"]):
            raise HTTPException(404, "clip not found")
        return _serve_file_with_range(e["clip_path"], "audio/wav", request)

    # GMRS-style alias so the dashboard live-feed can hit /clip/<id>.
    @r.get("/clip/{event_id}")
    def clip(event_id: int, request: Request):
        return audio(event_id, request)

    @r.get("/presets")
    def presets():
        items = []
        for p in list_presets():
            text = p.read_text(encoding="utf-8", errors="replace")
            items.append({
                "name": p.stem,
                "filename": p.name,
                "description": _peek_description(text),
                "size_bytes": p.stat().st_size,
            })
        return {"presets": items, "presets_dir": str(presets_dir())}

    @r.get("/profile/list")
    def profile_list():
        return {
            "user_profiles": [{"path": str(p), "name": p.stem}
                              for p in list_user_profiles()],
            "presets": [{"path": str(p), "name": p.stem}
                        for p in list_presets()],
            "active": (str(tool.profile.source_path)
                       if tool.profile and tool.profile.source_path else None),
        }

    @r.get("/hackrf/health")
    def health():
        info = _hackrf_health()
        info["flowgraph_running"] = tool.is_running
        info["meta"] = tool.monitor_meta
        if tool.profile:
            info["profile_id"] = tool.profile.sdr.id
            info["sample_rate_locked"] = info["meta"].get("audio_rate") is not None
        return info

    @r.get("/summary")
    def summary():
        return tool.summary()

    return r


def _peek_description(text: str, max_lines: int = 6) -> str:
    """Pull leading '#' comment lines as a description for the preset list."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            out.append(s.lstrip("#").strip())
            if len(out) >= max_lines:
                break
        else:
            break
    return " · ".join(out)
