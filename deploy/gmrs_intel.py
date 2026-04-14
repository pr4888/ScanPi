"""Heimdall gateway module: GMRS/FRS activity ingestion + stats.

Receives TX events from ScanPi (or any GMRS monitor) via POST /v1/gmrs/event
and exposes aggregated stats via GET /v1/gmrs/stats + GET /v1/gmrs/events.

Events append to ~/rfforge/bridge_data/gmrs_events.csv. Stats computed on-demand.
"""
from __future__ import annotations

import csv
import logging
import os
import time
from pathlib import Path

from flask import jsonify, request

log = logging.getLogger("gateway.gmrs")

GMRS_CSV = Path(os.path.expanduser("~/rfforge/bridge_data/gmrs_events.csv"))
GMRS_HEADER = [
    "timestamp_utc", "source", "keeper",
    "channel", "freq_mhz", "service",
    "start_ts", "end_ts", "duration_s",
    "peak_rssi", "avg_rssi",
    "ctcss_hz", "ctcss_code",
    "clip_path",
]

# FRS/GMRS channel plan for validation + metadata
_CH_META = {
    1:  ("FRS/GMRS", 462.5625), 2: ("FRS/GMRS", 462.5875), 3: ("FRS/GMRS", 462.6125),
    4:  ("FRS/GMRS", 462.6375), 5: ("FRS/GMRS", 462.6625), 6: ("FRS/GMRS", 462.6875),
    7:  ("FRS/GMRS", 462.7125),
    8:  ("FRS", 467.5625), 9: ("FRS", 467.5875), 10: ("FRS", 467.6125),
    11: ("FRS", 467.6375), 12: ("FRS", 467.6625), 13: ("FRS", 467.6875),
    14: ("FRS", 467.7125),
    15: ("GMRS", 462.5500), 16: ("GMRS", 462.5750), 17: ("GMRS", 462.6000),
    18: ("GMRS", 462.6250), 19: ("GMRS", 462.6500), 20: ("GMRS", 462.6750),
    21: ("GMRS", 462.7000), 22: ("GMRS", 462.7250),
}


def _ensure_csv():
    GMRS_CSV.parent.mkdir(parents=True, exist_ok=True)
    if not GMRS_CSV.exists():
        with GMRS_CSV.open("w", newline="") as f:
            csv.writer(f).writerow(GMRS_HEADER)


def _append_event(row: dict):
    _ensure_csv()
    with GMRS_CSV.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=GMRS_HEADER, extrasaction="ignore")
        w.writerow(row)


def _read_events(since_ts: float = 0.0, limit: int | None = None) -> list[dict]:
    if not GMRS_CSV.exists():
        return []
    out = []
    with GMRS_CSV.open("r", newline="") as f:
        for row in csv.DictReader(f):
            try:
                start = float(row.get("start_ts") or 0)
            except ValueError:
                continue
            if start >= since_ts:
                out.append(row)
    if limit:
        out = out[-limit:]
    return out


def _aggregate(rows: list[dict]) -> list[dict]:
    """Group events by channel and compute counts/airtime/last-active."""
    agg: dict[int, dict] = {}
    for r in rows:
        try:
            ch = int(r["channel"])
            dur = float(r.get("duration_s") or 0)
            end = float(r.get("end_ts") or r.get("start_ts") or 0)
            peak = float(r.get("peak_rssi") or -120)
        except (ValueError, KeyError):
            continue
        a = agg.setdefault(ch, {
            "channel": ch, "tx_count": 0, "total_airtime_s": 0.0,
            "last_active": 0.0, "peak_rssi_max": -120.0, "peak_rssi_sum": 0.0,
        })
        a["tx_count"] += 1
        a["total_airtime_s"] += dur
        a["last_active"] = max(a["last_active"], end)
        a["peak_rssi_max"] = max(a["peak_rssi_max"], peak)
        a["peak_rssi_sum"] += peak
    out = []
    for ch, a in sorted(agg.items(), key=lambda kv: -kv[1]["tx_count"]):
        meta = _CH_META.get(ch, ("?", 0.0))
        out.append({
            "channel": ch,
            "service": meta[0],
            "freq_mhz": meta[1],
            "tx_count": a["tx_count"],
            "total_airtime_s": round(a["total_airtime_s"], 1),
            "avg_duration_s": round(a["total_airtime_s"] / a["tx_count"], 1) if a["tx_count"] else 0,
            "last_active": a["last_active"],
            "avg_peak_rssi": round(a["peak_rssi_sum"] / a["tx_count"], 1) if a["tx_count"] else None,
            "peak_rssi_max": round(a["peak_rssi_max"], 1),
        })
    return out


# -------------------------------------------------------------------- public


def get_gmrs_activity(hours: float = 24.0, min_count: int = 1) -> dict:
    """Context-module-style helper. Returns summary for AI prompt inclusion."""
    since = time.time() - hours * 3600 if hours > 0 else 0.0
    rows = _read_events(since_ts=since)
    channels = [c for c in _aggregate(rows) if c["tx_count"] >= min_count]
    return {
        "hours": hours,
        "total_events": len(rows),
        "active_channels": len(channels),
        "channels": channels,
    }


def format_gmrs_context(hours: float = 24.0) -> str:
    """Human-readable block for injection into AI system prompt."""
    data = get_gmrs_activity(hours=hours)
    if not data["channels"]:
        return "GMRS/FRS ACTIVITY: no transmissions logged in last %dh.\n" % int(hours)
    lines = ["GMRS/FRS ACTIVITY (last %dh, %d events across %d channels):" %
             (int(hours), data["total_events"], data["active_channels"])]
    for c in data["channels"][:10]:
        lines.append(
            f"  Ch {c['channel']:2d} ({c['freq_mhz']:.4f} MHz, {c['service']}): "
            f"{c['tx_count']} TX, airtime {c['total_airtime_s']:.0f}s, "
            f"peak RSSI {c['peak_rssi_max']:.0f} dBFS"
        )
    return "\n".join(lines) + "\n"


UI_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Heimdall — GMRS Intel</title>
<style>
 :root{--bg:#0b0d10;--fg:#d8dde3;--dim:#7b8796;--ok:#4ae04a;--hot:#ff8844;--hdr:#1a1f26;--row:#12161c;--accent:#4a9eff}
 html,body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif}
 header{padding:12px 18px;background:var(--hdr);border-bottom:1px solid #252d38;display:flex;justify-content:space-between;align-items:center}
 h1{margin:0;font-size:16px}
 .sub{color:var(--dim);font-size:12px}
 main{padding:16px;max-width:1100px;margin:0 auto}
 table{width:100%;border-collapse:collapse;background:var(--row)}
 th,td{padding:8px 12px;text-align:left;border-bottom:1px solid #1d232b}
 th{color:var(--dim);font-weight:500;font-size:12px;text-transform:uppercase;letter-spacing:.5px;background:var(--hdr)}
 .num{text-align:right;font-variant-numeric:tabular-nums}
 .hot{color:var(--hot);font-weight:600}
 section{margin-top:20px}
 h2{font-size:13px;text-transform:uppercase;color:var(--dim);margin:0 0 8px;letter-spacing:.5px}
 .recommend{background:var(--row);padding:12px 16px;border-left:3px solid var(--accent);margin-bottom:16px}
 .recommend strong{color:var(--accent)}
 nav a{color:var(--accent);text-decoration:none;margin-right:14px;font-size:12px}
</style></head>
<body>
<header>
  <div><h1>Heimdall — GMRS/FRS Intel</h1><nav><a href="/">gateway</a><a href="/ais">ais</a><a href="/v1/rf-intel/ui">rf-intel</a></nav></div>
  <div class="sub" id="meta">—</div>
</header>
<main>
  <div class="recommend" id="rec">Loading…</div>
  <section><h2>Channel activity (last 24h)</h2>
    <table id="stats"><thead><tr><th>Ch</th><th>Freq</th><th>Service</th><th class="num">TX</th><th class="num">Airtime</th><th class="num">Avg dur</th><th>Last</th><th class="num">Peak RSSI</th></tr></thead><tbody></tbody></table>
  </section>
  <section><h2>Recent events</h2>
    <table id="recent"><thead><tr><th>Time</th><th>Ch</th><th class="num">Dur</th><th class="num">Peak</th><th>Source</th></tr></thead><tbody></tbody></table>
  </section>
</main>
<script>
const fmt={dur:s=>s==null?"—":s<60?(+s).toFixed(1)+"s":((+s)/60).toFixed(1)+"m",
 rssi:d=>d==null?"—":(+d).toFixed(1),
 ago:ts=>{if(!ts)return"never";const a=Date.now()/1000-(+ts);if(a<60)return Math.floor(a)+"s ago";if(a<3600)return Math.floor(a/60)+"m ago";return Math.floor(a/3600)+"h ago";},
 time:ts=>ts?new Date((+ts)*1000).toLocaleTimeString():"—"};
async function refresh(){
  try{
    const [s,e]=await Promise.all([fetch("/v1/gmrs/stats?hours=24").then(r=>r.json()),fetch("/v1/gmrs/events?hours=24&limit=50").then(r=>r.json())]);
    document.getElementById("meta").textContent=`${s.total_events} events · ${s.active_channels} active ch · updated ${new Date().toLocaleTimeString()}`;
    const st=document.querySelector("#stats tbody");st.innerHTML="";
    const top=s.channels[0];
    for(const c of s.channels){
      const hot=(top&&c.channel===top.channel)?"hot":"";
      const tr=document.createElement("tr");
      tr.innerHTML=`<td class="${hot}">Ch ${c.channel}</td><td>${(+c.freq_mhz).toFixed(4)} MHz</td><td>${c.service}</td><td class="num ${hot}">${c.tx_count}</td><td class="num">${fmt.dur(c.total_airtime_s)}</td><td class="num">${fmt.dur(c.avg_duration_s)}</td><td>${fmt.ago(c.last_active)}</td><td class="num">${fmt.rssi(c.peak_rssi_max)}</td>`;
      st.appendChild(tr);
    }
    const rec=document.getElementById("rec");
    if(top&&top.tx_count>=3)rec.innerHTML=`Most active last 24h: <strong>Ch ${top.channel}</strong> (${(+top.freq_mhz).toFixed(4)} MHz) — ${top.tx_count} TX, ${fmt.dur(top.total_airtime_s)} airtime, peak ${fmt.rssi(top.peak_rssi_max)} dBFS.`;
    else rec.textContent="No significant activity in last 24h.";
    const et=document.querySelector("#recent tbody");et.innerHTML="";
    for(const ev of e.events.slice().reverse()){
      const tr=document.createElement("tr");
      tr.innerHTML=`<td>${fmt.time(ev.start_ts)}</td><td>Ch ${ev.channel}</td><td class="num">${fmt.dur(ev.duration_s)}</td><td class="num">${fmt.rssi(ev.peak_rssi)}</td><td>${ev.keeper||"?"}</td>`;
      et.appendChild(tr);
    }
  }catch(err){document.getElementById("meta").textContent="error: "+err;}
}
refresh();setInterval(refresh,5000);
</script></body></html>"""


def register(app, auth):
    """Attach routes to an existing Flask app. auth.require_auth is the decorator."""

    from flask import Response

    @app.route("/v1/gmrs/ui", methods=["GET"])
    def gmrs_ui():
        return Response(UI_HTML, mimetype="text/html")

    @app.route("/v1/gmrs/event", methods=["POST"])
    @auth.require_auth
    def gmrs_event_ingest():
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "Expected JSON body"}), 400
        try:
            ch = int(data.get("channel", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "channel must be int"}), 400
        if ch not in _CH_META:
            return jsonify({"error": f"unknown channel {ch}"}), 400

        service, expected_mhz = _CH_META[ch]
        row = {
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": str(data.get("source", "scanpi")),
            "keeper": str(data.get("keeper", "scanpi")),
            "channel": ch,
            "freq_mhz": data.get("freq_mhz", expected_mhz),
            "service": service,
            "start_ts": data.get("start_ts"),
            "end_ts": data.get("end_ts"),
            "duration_s": data.get("duration_s"),
            "peak_rssi": data.get("peak_rssi"),
            "avg_rssi": data.get("avg_rssi"),
            "ctcss_hz": data.get("ctcss_hz"),
            "ctcss_code": data.get("ctcss_code"),
            "clip_path": data.get("clip_path"),
        }
        _append_event(row)
        log.info("GMRS ch=%d dur=%.1fs peak=%s keeper=%s",
                 ch, float(row["duration_s"] or 0), row["peak_rssi"], row["keeper"])
        return jsonify({"status": "ok", "channel": ch})

    @app.route("/v1/gmrs/stats", methods=["GET"])
    @auth.require_auth
    def gmrs_stats():
        hours = float(request.args.get("hours", 24.0))
        min_count = int(request.args.get("min_count", 1))
        return jsonify(get_gmrs_activity(hours=hours, min_count=min_count))

    @app.route("/v1/gmrs/events", methods=["GET"])
    @auth.require_auth
    def gmrs_events():
        hours = float(request.args.get("hours", 24.0))
        limit = int(request.args.get("limit", 100))
        since = time.time() - hours * 3600 if hours > 0 else 0.0
        rows = _read_events(since_ts=since, limit=limit)
        return jsonify({"hours": hours, "count": len(rows), "events": rows})

    log.info("GMRS intel endpoints registered: /v1/gmrs/event, /v1/gmrs/stats, /v1/gmrs/events")
