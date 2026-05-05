"""FastAPI router for the Alerts tool.

Mounted by the coordinator at /tools/alerts/api/*. The router operates on a
pre-built `AlertsTool` instance — pass it to `make_router()`.
"""
from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request

from .watchlist import VALID_SEVERITIES

if TYPE_CHECKING:  # pragma: no cover
    from . import AlertsTool


_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.IGNORECASE)


def _parse_since(spec: str | None) -> float:
    """Parse a relative duration like '24h' / '30m' / '7d' into a unix ts.

    Falls back to "24h ago" on error.
    """
    if spec is None:
        spec = "24h"
    spec = str(spec).strip().lower()
    m = _DURATION_RE.match(spec)
    if not m:
        # Maybe it's already a unix ts?
        try:
            return float(spec)
        except Exception:
            return time.time() - 24 * 3600
    val = float(m.group(1))
    unit = (m.group(2) or "s").lower()
    seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit, 1)
    return time.time() - val * seconds


def make_router(tool: "AlertsTool") -> APIRouter:
    r = APIRouter()

    # ---- alerts -----------------------------------------------------

    @r.get("/alerts")
    def list_alerts(
        since: str = "24h",
        severity: str | None = None,
        source: str | None = None,
        limit: int = 50,
    ):
        if severity and severity not in VALID_SEVERITIES:
            raise HTTPException(400, f"severity must be one of {VALID_SEVERITIES}")
        rows = tool.db.list_alerts(
            since_ts=_parse_since(since),
            severity=severity, source=source, limit=int(limit),
        )
        return {"alerts": rows, "count": len(rows), "since": since}

    @r.get("/alerts/health")
    def alerts_health():
        return tool.health_payload()

    @r.get("/alerts/{alert_id}")
    def alert_detail(alert_id: int):
        row = tool.db.get_alert(alert_id)
        if not row:
            raise HTTPException(404, "alert not found")
        return row

    @r.post("/alerts/{alert_id}/ack")
    async def ack_alert(alert_id: int, request: Request):
        # TODO auth — wire scanpi.api._check_token(request) when shared auth lands.
        ok = tool.db.acknowledge(alert_id)
        if not ok:
            row = tool.db.get_alert(alert_id)
            if not row:
                raise HTTPException(404, "alert not found")
            return {"ok": True, "already_acked": True}
        return {"ok": True}

    # ---- watchlist --------------------------------------------------

    @r.get("/watchlist")
    def list_watchlist():
        rules = [r_.to_dict() for r_ in tool.watchlist.all()]
        return {"rules": rules, "count": len(rules)}

    @r.post("/watchlist")
    async def upsert_rule(request: Request):
        # TODO auth — wire scanpi.api._check_token(request).
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid JSON")
        try:
            rule = tool.watchlist.upsert(body)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "rule": rule.to_dict()}

    @r.delete("/watchlist/{name}")
    async def delete_rule(name: str, request: Request):
        # TODO auth.
        ok = tool.watchlist.delete(name)
        if not ok:
            raise HTTPException(404, f"rule not found: {name}")
        return {"ok": True}

    # ---- backwards-compat: same router serves "recent" so dashboard
    #      live-feed code can fold alerts in if we ever hook it up.
    @r.get("/recent")
    def recent(limit: int = 50):
        rows = tool.db.list_alerts(since_ts=0, limit=int(limit))
        # Shape to match dashboard expectations (events / calls).
        events = []
        for a in rows:
            events.append({
                "id": a["id"],
                "start_ts": a["ts"],
                "channel": a.get("channel"),
                "duration_s": None,
                "transcript": a.get("transcript"),
                "alert_kind": a["severity"],
                "clip_path": None,
                "category": "alert",
            })
        return {"events": events}

    return r
