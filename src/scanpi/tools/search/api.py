"""FastAPI router for the search tool.

Endpoints (all relative — mounted at /tools/search/api/* automatically):
  GET /search                 — main search (lexical | semantic | hybrid)
  GET /search/similar/{src}/{id}  — vector kNN against a specific call's embedding
  GET /search/health          — counts, model status, profile flags echo
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query

log = logging.getLogger(__name__)


# Map "since" shorthand like "24h", "30m", "7d" to seconds.
def _parse_since(since: str | None) -> float:
    if not since:
        return 0.0
    s = since.strip().lower()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([smhdw])?$", s)
    if not m:
        return 0.0
    n = float(m.group(1))
    unit = m.group(2) or "s"
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    cutoff = time.time() - n * mult
    return cutoff


def _result_row_from_meta(m: dict, score: float, match_type: str) -> dict:
    """Normalize a row from fts_meta + extras into the API result shape."""
    return {
        "id": m["id"],
        "source": m["source"],
        "source_id": m["source_id"],
        "channel_or_tg": m.get("channel_or_tg") or "",
        "label": m.get("label") or "",
        "ts": m.get("ts"),
        "transcript": m.get("transcript") or "",
        "snippet": m.get("snippet") or m.get("transcript") or "",
        "duration_s": m.get("duration_s"),
        "score": float(score),
        "match_type": match_type,
        "audio_url": m.get("audio_url") or "",
    }


def build_router(tool) -> APIRouter:
    """Build the APIRouter. Imports here to avoid circular deps with __init__."""
    from .embed import topk_cosine

    r = APIRouter()

    # ----- /search ---------------------------------------------------------
    @r.get("/search")
    def search(
        q: str = Query("", description="Search text"),
        mode: str = Query("lexical", pattern="^(lexical|semantic|hybrid)$"),
        since: str | None = Query(None, description="e.g. 24h, 7d, 30m"),
        source: str = Query("all", pattern="^(gmrs|op25|all)$"),
        limit: int = Query(50, ge=1, le=500),
    ):
        if not tool._db:
            return {"q": q, "mode": mode, "results": [], "warning": "search db offline"}
        if not q.strip():
            return {"q": q, "mode": mode, "results": []}
        since_ts = _parse_since(since)

        warnings: list[str] = []

        # --- Lexical (always available) -----------------------------------
        lex_hits = []
        if mode in ("lexical", "hybrid"):
            try:
                lex_hits = tool._db.search_fts(
                    query=q, since_ts=since_ts, source=source, limit=limit,
                )
            except Exception:
                log.exception("FTS search failed")
                warnings.append("lexical search failed")

        # --- Semantic (opt-in) --------------------------------------------
        sem_hits: list[dict] = []
        do_semantic = mode in ("semantic", "hybrid")
        if do_semantic:
            if not tool._semantic_enabled:
                warnings.append("semantic_search disabled in profile")
            elif tool._semantic_status != "ready":
                warnings.append(f"semantic model {tool._semantic_status}")
            elif tool._embed_worker is None:
                warnings.append("embedding worker offline")
            else:
                try:
                    qvec = tool._embed_worker.encode_query(q)
                    if qvec is None:
                        warnings.append("query encode failed")
                    else:
                        # Pull more than `limit` so filters (since/source) still leave headroom.
                        topk = topk_cosine(qvec, tool._db, k=limit * 4)
                        ids = [tid for tid, _ in topk]
                        scores = {tid: s for tid, s in topk}
                        metas = tool._db.fetch_metas_bulk(ids)
                        for tid in ids:
                            m = metas.get(tid)
                            if not m:
                                continue
                            if since_ts and (m.get("ts") or 0) < since_ts:
                                continue
                            if source != "all" and m.get("source") != source:
                                continue
                            sem_hits.append(_result_row_from_meta(
                                m, scores[tid], "semantic",
                            ))
                            if len(sem_hits) >= limit:
                                break
                except Exception:
                    log.exception("semantic search failed")
                    warnings.append("semantic search errored")

        # --- Combine ------------------------------------------------------
        if mode == "lexical":
            merged = lex_hits[:limit]
        elif mode == "semantic":
            merged = sem_hits[:limit]
        else:  # hybrid -> reciprocal rank fusion
            merged = _rrf_merge(lex_hits, sem_hits, k=60)[:limit]

        return {
            "q": q, "mode": mode, "since": since, "source": source,
            "warnings": warnings,
            "results": merged,
            "count": len(merged),
        }

    # ----- /search/similar/{source}/{id} ----------------------------------
    @r.get("/search/similar/{src}/{src_id}")
    def similar(src: str, src_id: int, limit: int = Query(20, ge=1, le=100)):
        if src not in ("gmrs", "op25"):
            raise HTTPException(400, "source must be 'gmrs' or 'op25'")
        if not tool._db:
            return {"results": [], "warning": "search db offline"}
        if not tool._semantic_enabled or tool._semantic_status != "ready" \
                or tool._embed_worker is None:
            return {
                "results": [],
                "warning": f"semantic search not available (status={tool._semantic_status})",
            }
        # Find the fts_id for this source/source_id pair
        meta = tool._db.fetch_meta_by_source(src, src_id)
        if not meta:
            return {"results": [], "warning": "no transcript indexed for that call"}
        fts_id = int(meta["id"])
        vec_blob = tool._db.fetch_embedding(fts_id)
        if not vec_blob:
            return {"results": [], "warning": "no embedding yet — try again soon"}
        try:
            import numpy as np
            qvec = np.frombuffer(vec_blob, dtype=np.float32)
        except Exception:
            return {"results": [], "warning": "embedding decode failed"}
        topk = topk_cosine(qvec, tool._db, k=limit + 1)
        ids = [tid for tid, _ in topk if tid != fts_id][:limit]
        scores = {tid: s for tid, s in topk}
        metas = tool._db.fetch_metas_bulk(ids)
        out = []
        for tid in ids:
            m = metas.get(tid)
            if m:
                out.append(_result_row_from_meta(m, scores[tid], "semantic"))
        return {"reference": _result_row_from_meta(meta, 1.0, "self"), "results": out}

    # ----- /search/health -------------------------------------------------
    @r.get("/search/health")
    def health():
        counts = tool._db.row_counts() if tool._db else {"fts": 0}
        return {
            "running": tool._sync_thread is not None and tool._sync_thread.is_alive(),
            "counts": counts,
            "last_sync_ts": tool._last_sync_ts,
            "last_sync_added": tool._last_sync_added,
            "model_loaded": (tool._embed_worker.is_ready()
                             if tool._embed_worker is not None else False),
            "semantic_status": tool._semantic_status,
            "profile_features": {
                "semantic_search": tool._semantic_enabled,
            },
        }

    return r


def _rrf_merge(
    lex: list[dict], sem: list[dict], k: int = 60,
) -> list[dict]:
    """Reciprocal rank fusion of two ranked result lists.

    RRF score = sum over lists of 1 / (k + rank_in_list).
    Same call is identified by (source, source_id).
    Match type becomes 'hybrid' if seen in both lists, otherwise the original.
    """
    by_key: dict[tuple[str, int], dict[str, Any]] = {}

    def _key(row: dict) -> tuple[str, int]:
        return (row["source"], int(row["source_id"]))

    for rank, row in enumerate(lex, start=1):
        key = _key(row)
        entry = by_key.setdefault(key, {"row": dict(row), "rrf": 0.0, "in": set()})
        entry["rrf"] += 1.0 / (k + rank)
        entry["in"].add("lex")
    for rank, row in enumerate(sem, start=1):
        key = _key(row)
        entry = by_key.setdefault(key, {"row": dict(row), "rrf": 0.0, "in": set()})
        # If we already have a lexical hit, prefer the lexical row's snippet
        # (which has highlights) but keep the higher confidence.
        if "lex" not in entry["in"]:
            entry["row"] = dict(row)
        entry["rrf"] += 1.0 / (k + rank)
        entry["in"].add("sem")

    merged: list[dict] = []
    for key, entry in by_key.items():
        row = entry["row"]
        row["score"] = float(entry["rrf"])
        row["match_type"] = "hybrid" if entry["in"] == {"lex", "sem"} else \
            ("lexical" if entry["in"] == {"lex"} else "semantic")
        merged.append(row)
    merged.sort(key=lambda x: x["score"], reverse=True)
    return merged
