"""ScanPi v0.3 — unified app with tool framework.

Architecture:
  - ToolRegistry holds all specialty tools (GMRS, Scanner, …).
  - SdrCoordinator arbitrates SDR access — only one SDR-needing tool runs at a time.
  - FastAPI main app mounts each tool's APIRouter at /tools/<id>/api/
    and serves each tool's page at /tools/<id>/.
  - Home ("/") renders the dashboard shell with nav + summary widgets.

Entry point: run_v3() — separate from legacy app.run() so v0.2 still works.
"""
from __future__ import annotations

import logging
import signal
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from .coordinator import SdrCoordinator
from .tools import Tool, ToolRegistry

log = logging.getLogger(__name__)


# HTML templates use plain __TITLE__ / __BODY__ placeholders and a simple
# .replace(). NOT str.format() — the body and shell contain JS object literals
# whose `{}` characters are a syntax nightmare to escape for .format().

SHELL_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>ScanPi :: __TITLE__</title>
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<link rel="stylesheet" href="/static/theme.css">
</head><body>
<header>
 <h1><span class="live-dot"></span>ScanPi</h1>
 <nav id="nav"></nav>
</header>
<main id="main">__BODY__</main>
<script>
async function loadNav(){
  const r = await fetch("/api/tools");
  const data = await r.json();
  const here = window.location.pathname;
  const nav = document.getElementById("nav");
  const dashClass = here === "/" ? "active" : "";
  const settingsClass = here === "/settings" ? "active" : "";
  nav.innerHTML = '<a href="/" class="'+dashClass+'">dashboard</a>' +
    data.tools.map(t => {
      const active = here.startsWith("/tools/"+t.id);
      return '<a href="/tools/'+t.id+'/" class="'+(active?"active":"")+'">'+t.name+'</a>';
    }).join("") +
    '<a href="/settings" class="'+settingsClass+'">settings</a>';
}
loadNav();
</script>
</body></html>"""


DASHBOARD_BODY = """
<section>
  <h2>Tools</h2>
  <div class="grid" id="tools-grid">Loading…</div>
</section>

<section>
  <h2>Live activity — all tools
    <span id="feed-count" style="margin-left:12px;color:var(--amber);font-size:11px"></span>
  </h2>
  <div class="panel" id="live-feed" style="max-height:400px;overflow-y:auto">
    <div style="color:var(--green-dim);font-size:11px">Loading…</div>
  </div>
</section>
<script>
const fmtAgo = ts => {
  if(!ts) return "never";
  const a = Date.now()/1000 - ts;
  if(a < 60) return Math.floor(a)+"s ago";
  if(a < 3600) return Math.floor(a/60)+"m ago";
  return Math.floor(a/3600)+"h ago";
};

async function render(){
  const [sdr, tools] = await Promise.all([
    fetch("/api/coordinator/status").then(r=>r.json()),
    fetch("/api/tools").then(r=>r.json()),
  ]);
  const grid = document.getElementById("tools-grid");
  grid.innerHTML = "";
  for(const t of tools.tools){
    const card = document.createElement("div");
    card.className = "card";
    const isActive = sdr.active === t.id;
    const running = t.status && t.status.running;
    const healthy = t.status && t.status.healthy !== false;
    const tagClass = !healthy ? "active" : (running ? "running" : "stopped");
    const tagText = !healthy ? "warn" : (running ? "running" : "stopped");
    const activeTag = isActive ? '<span class="tag active">SDR-active</span>' : '';
    const sdrBadge = t.needs_sdr ? ' <span style="color:var(--warn);font-size:11px">needs SDR</span>' : '';
    let controls = '';
    if(t.needs_sdr){
      if(isActive){
        controls = '<button class="secondary" onclick="deactivate()">Stop (release SDR)</button>';
      } else {
        controls = '<button onclick="activate(\\''+t.id+'\\')">Activate</button>';
      }
    }
    const statusMsg = t.status && t.status.message ? t.status.message : '';
    const sm = t.summary || {};
    // Big stats block — "what's this tool doing lately" at a glance
    let stats = '';
    if (sm.running) {
      // Unify GMRS + OP25 field names for display
      const total24 = sm.total_tx_24h ?? sm.total_calls_24h ?? 0;
      const activeN = sm.active_channels_24h ?? sm.active_tgs_24h ?? 0;
      const activeLabel = sm.active_channels_24h != null ? "active ch" : "active TGs";
      const allTime = sm.all_time_count ?? 0;
      let topLabel = '—';
      if (sm.top_channel != null) {
        topLabel = `Ch ${sm.top_channel}${sm.top_freq_mhz ? ` · ${sm.top_freq_mhz} MHz` : ''}`;
      } else if (sm.top_tg_name) {
        topLabel = sm.top_tg_name;
      } else if (sm.top_tg != null) {
        topLabel = `TG ${sm.top_tg}`;
      }
      stats = '<div class="stat-row">'
        + '<div><div class="stat-big">'+total24+'</div><div class="stat-label">24h</div></div>'
        + '<div><div class="stat-big">'+activeN+'</div><div class="stat-label">'+activeLabel+'</div></div>'
        + '<div><div class="stat-big" style="font-size:14px;margin-top:4px">'+topLabel+'</div><div class="stat-label">busiest</div></div>'
        + '<div><div class="stat-big">'+allTime+'</div><div class="stat-label">all-time</div></div>'
        + '</div>';
    }
    const lastTs = (sm && sm.last_activity_ts) || (t.status && t.status.last_activity_ts);
    const lastActivity = lastTs ? '<div class="meta"><span>last activity: <strong>'+fmtAgo(lastTs)+'</strong></span></div>' : '';
    // Latest transcript preview — the "what did they say" blurb
    let preview = '';
    if (sm && sm.preview) {
      const sub = sm.preview_tg ? (sm.preview_tg + ' · ') : '';
      const when = sm.preview_ts ? (' · ' + fmtAgo(sm.preview_ts)) : '';
      preview = '<div class="last-line" style="margin:10px 0 6px">'
        + '<span style="color:var(--amber-dim);font-size:10px;text-transform:uppercase;letter-spacing:1px">'+sub+'latest heard'+when+'</span><br>'
        + '<span style="color:var(--green)">"'+sm.preview.replace(/</g,'&lt;')+'"</span>'
        + '</div>';
    }
    // Alert ribbon — 24h alert counts, clickable to open the tool page
    let alertRibbon = '';
    if (sm && sm.alert_counts) {
      const kinds = Object.entries(sm.alert_counts);
      if (kinds.length > 0) {
        const total = kinds.reduce((a, [,n]) => a + n, 0);
        const badges = kinds.map(([k, n]) =>
          `<span class="alert-badge ${k}">${k} ${n}</span>`
        ).join(' ');
        alertRibbon = `<div class="alert-banner" style="margin:10px 0 6px;padding:6px 10px;font-size:11px">`
          + `<strong>ALERTS 24h:</strong> ${total} · ${badges}</div>`;
      }
    }
    card.innerHTML =
      '<h3>'+t.name+' <span class="tag '+tagClass+'">'+tagText+'</span>'+activeTag+sdrBadge+'</h3>'+
      '<div class="desc">'+t.description+'</div>'+
      stats + alertRibbon + preview +
      '<div class="meta">'+statusMsg+'</div>'+lastActivity+
      '<div style="margin-top:10px;display:flex;gap:8px"><a class="inline" href="/tools/'+t.id+'/">Open →</a>'+controls+'</div>';
    grid.appendChild(card);
  }
}
async function activate(id){
  await fetch("/api/coordinator/activate", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({tool_id:id})});
  render();
}
async function deactivate(){
  await fetch("/api/coordinator/deactivate", {method:"POST"});
  render();
}
// Unified live feed — pulls Recent from each tool, merges by timestamp
async function renderFeed() {
  try {
    const toolsResp = await fetch("/api/tools").then(r => r.json());
    const items = [];
    for (const t of toolsResp.tools) {
      try {
        const rec = await fetch(`/tools/${t.id}/api/recent?limit=25`).then(r => r.json());
        const list = rec.events || rec.calls || [];
        for (const e of list) {
          items.push({
            ts: e.start_ts,
            tool: t.id,
            toolName: t.name,
            label: e.tg_name || ("Ch " + e.channel),
            category: e.category || (e.channel ? "gmrs" : "other"),
            duration: e.duration_s,
            transcript: e.transcript,
            alert_kind: e.alert_kind,
            id: e.id,
            has_clip: !!e.clip_path,
          });
        }
      } catch (err) {}
    }
    items.sort((a, b) => b.ts - a.ts);
    const top = items.slice(0, 25);
    document.getElementById("feed-count").textContent = `${items.length} recent events across tools`;
    const feed = document.getElementById("live-feed");
    if (!top.length) {
      feed.innerHTML = '<div style="color:var(--green-dim);font-size:11px">No activity yet.</div>';
      return;
    }
    feed.innerHTML = top.map(it => {
      const alertBadge = it.alert_kind ? `<span class="alert-badge ${it.alert_kind}">${it.alert_kind}</span>` : "";
      const dur = it.duration != null ? (it.duration < 60 ? it.duration.toFixed(1)+"s" : (it.duration/60).toFixed(1)+"m") : "—";
      const text = it.transcript
        ? `<span style="color:var(--green)">"${it.transcript.replace(/</g,"&lt;")}"</span>`
        : '<span style="color:var(--green-deep)">(no transcript)</span>';
      const timeStr = new Date(it.ts*1000).toLocaleTimeString();
      const audio = it.has_clip
        ? `<audio controls preload="none" src="/tools/${it.tool}/api/clip/${it.id}" style="height:24px;width:160px;vertical-align:middle"></audio>`
        : '<span style="color:var(--green-deep);font-size:10px">(no audio)</span>';
      return `<div style="border-bottom:1px dashed var(--border);padding:6px 0;font-size:12px">
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <span style="color:var(--amber);font-variant-numeric:tabular-nums">${timeStr}</span>
          <a href="/tools/${it.tool}/" style="color:var(--green-dim);font-size:10px;text-transform:uppercase;letter-spacing:1px">${it.tool}</a>
          <span style="color:var(--fg)">${it.label}</span>
          <span style="color:var(--green-dim);font-size:11px">${dur}</span>
          ${alertBadge}
          ${audio}
        </div>
        <div style="margin-top:2px;margin-left:72px">${text}</div>
      </div>`;
    }).join("");
  } catch (e) {
    document.getElementById("live-feed").innerHTML = '<div style="color:var(--hot);font-size:11px">feed error: '+e+'</div>';
  }
}
render();
renderFeed();
setInterval(render, 3000);
setInterval(renderFeed, 4000);
</script>
"""


def _render_shell(title: str, body: str) -> str:
    return SHELL_HTML.replace("__TITLE__", title).replace("__BODY__", body)


def create_app(registry: ToolRegistry, coordinator: SdrCoordinator) -> FastAPI:
    from fastapi.staticfiles import StaticFiles
    app = FastAPI(title="ScanPi")

    static_dir = Path(__file__).parent / "web"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon():
        from fastapi.responses import FileResponse
        f = static_dir / "favicon.svg"
        if f.exists():
            return FileResponse(f, media_type="image/svg+xml")

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        return _render_shell("Dashboard", DASHBOARD_BODY)

    @app.get("/settings", response_class=HTMLResponse)
    def settings():
        f = static_dir / "settings.html"
        if f.exists():
            return f.read_text(encoding="utf-8")
        raise HTTPException(404)

    @app.get("/api/tools")
    def list_tools():
        return {
            "tools": [
                {
                    "id": t.id, "name": t.name, "description": t.description,
                    "needs_sdr": t.needs_sdr,
                    "status": _status_dict(t.status()),
                    "summary": t.summary(),
                }
                for t in registry.all()
            ]
        }

    @app.get("/api/coordinator/status")
    def coord_status():
        return {"active": coordinator.active}

    @app.post("/api/coordinator/activate")
    def coord_activate(body: dict):
        tid = body.get("tool_id")
        if not tid:
            raise HTTPException(400, "tool_id required")
        try:
            coordinator.activate(tid)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"active": coordinator.active}

    @app.post("/api/coordinator/deactivate")
    def coord_deactivate():
        coordinator.deactivate()
        return {"active": coordinator.active}

    @app.get("/tools/{tool_id}/", response_class=HTMLResponse)
    def tool_page(tool_id: str):
        tool = registry.get(tool_id)
        if not tool:
            raise HTTPException(404, f"unknown tool: {tool_id}")
        page = tool.page_html()
        if page is None:
            return _render_shell(tool.name, f"<p><em>{tool.name} has no UI page.</em></p>")
        return page

    @app.get("/tools/{tool_id}", include_in_schema=False)
    def tool_page_redirect(tool_id: str):
        return RedirectResponse(url=f"/tools/{tool_id}/")

    # Mount each tool's APIRouter at /tools/<id>/api
    for tool in registry.all():
        router = tool.api_router()
        if router is not None:
            app.include_router(router, prefix=f"/tools/{tool.id}/api")

    @app.get("/api/health")
    def health():
        return {"status": "ok", "tools": [t.id for t in registry.all()], "active_sdr": coordinator.active}

    return app


def _status_dict(status) -> dict:
    return {
        "running": status.running, "healthy": status.healthy,
        "last_activity_ts": status.last_activity_ts,
        "message": status.message, "extra": status.extra,
    }


# ---------------------------------------------------------------- runner


def run_v3(host: str = "0.0.0.0", port: int = 8080,
           data_dir: Path | None = None):
    """Build registry with available tools, wire coordinator, start server."""
    import uvicorn
    from .tools.gmrs import GmrsTool
    from .tools.op25 import OP25Tool
    from .tools.ysone import YardstickTool

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    data_dir = data_dir or (Path.home() / "scanpi")
    data_dir.mkdir(parents=True, exist_ok=True)

    registry = ToolRegistry()
    registry.register(GmrsTool(config={"data_dir": str(data_dir)}))
    registry.register(OP25Tool(config={"data_dir": str(data_dir)}))
    # YS1 is on its own USB device (sdr_device=100) → coordinator treats
    # it as a separate radio, runs in parallel with an RTL-SDR tool.
    try:
        registry.register(YardstickTool(config={"data_dir": str(data_dir)}))
    except Exception:
        log.exception("YardstickTool failed to register (YS1 not present?); skipping")

    coord = SdrCoordinator(registry, state_file=data_dir / "coordinator.json")
    coord.start_non_sdr_tools()
    coord.resume_last()

    def _shutdown(sig, frame):
        log.info("signal %s — shutting down", sig)
        coord.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    app = create_app(registry, coord)
    log.info("ScanPi v0.3 running at http://%s:%d/", host, port)
    uvicorn.run(app, host=host, port=port, log_level="warning")
