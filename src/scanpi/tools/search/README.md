# ScanPi Search Tool

Cross-source transcript search across the GMRS monitor and OP25 P25 trunking
tools. Supports lexical (FTS5), semantic (bge-small embeddings), and hybrid
ranking via reciprocal rank fusion.

## What this tool does

- ATTACHes `gmrs.db` and `op25.db` to its own `search.db` as **read-only**
  source databases. Never writes to them.
- Maintains an FTS5 virtual table (`fts_calls`) over the `transcript` column
  of `tx_events` (GMRS) and `p25_calls` (OP25).
- A background sync thread polls each source every ~10 s and inserts new
  transcribed rows. Idempotent — uses `UNIQUE(source, source_id)` and a
  per-source rowid watermark.
- (Optional) Generates 384-dim sentence embeddings via **bge-small-en-v1.5**
  on ONNX Runtime. Stored as float32 BLOBs in the same SQLite DB; cosine
  search is brute-force against the in-RAM matrix (fast enough for <200 k rows
  on a Pi 5).
- Provides `/search`, `/search/similar/{src}/{id}`, `/search/health`, and a
  single-page UI at `/tools/search/`.

## Why no sqlite-vss / Lance / FAISS?

Tried them. Both `sqlite-vss` and `Lance` had ARM build issues on the Pi 5 in
2026-04 testing. Brute-force numpy cosine over a (50 000, 384) float32 array
takes ~30 ms on a Pi 5 and is dependency-free. We can revisit when the index
crosses ~200 k rows or if Pi 5 ARM wheels stabilize.

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/tools/search/api/search` | Main search. Params: `q`, `mode`, `since`, `source`, `limit` |
| GET | `/tools/search/api/search/similar/{source}/{id}` | kNN against a specific call's embedding |
| GET | `/tools/search/api/search/health` | Counts, last sync, model status, profile flag echo |

### `/search` parameters

- `q` (str, required): query text. Bare tokens are prefix-matched (`fire` ->
  `fire*`). Quoted phrases (`"shots fired"`) preserved.
- `mode` (`lexical` | `semantic` | `hybrid`): default `lexical`.
- `since` (`24h`, `7d`, `30m`, `1w`, etc.): time cutoff. Empty = all time.
- `source` (`gmrs` | `op25` | `all`): default `all`.
- `limit` (int, 1-500): default 50.

### Result shape

```json
{
  "id": 1234,                // fts_meta.id (NOT the source id)
  "source": "op25",
  "source_id": 9876,         // p25_calls.id or tx_events.id
  "channel_or_tg": "TG 8851",
  "label": "Groton Fire Dispatch",
  "ts": 1714752000.123,
  "transcript": "...",
  "snippet": "...<mark>fire</mark>...",
  "audio_url": "/tools/op25/api/clip/9876",
  "duration_s": 5.4,
  "score": 0.0123,           // higher = better
  "match_type": "lexical"    // or "semantic" / "hybrid"
}
```

## Config keys

Passed via `SearchTool(config={...})` in `app_v3.run_v3()`:

| Key | Default | Notes |
|---|---|---|
| `data_dir` | `~/scanpi` | Owns `search.db` here, looks for `gmrs.db`/`op25.db` here |
| `gmrs_db` | `<data_dir>/gmrs.db` | Override path explicitly if needed |
| `op25_db` | `<data_dir>/op25.db` | Override path explicitly if needed |
| `sync_interval_s` | `10.0` | How often the FTS reindex thread polls |
| `backfill_limit` | `5000` | Max existing rows to embed on first run |
| `embed_batch_size` | `16` | Rows per ONNX inference batch |
| `model_name` | `BAAI/bge-small-en-v1.5` | HF repo id for bge-small |
| `model_dir` | `~/scanpi/models/bge-small-en-v1.5` | Where the ONNX + tokenizer live |

## Profile flag

```python
from scanpi.profile import feature_enabled
feature_enabled("semantic_search")  # lite: opt-in (off), full: on
```

If `False`, the embedding worker is never started. Lexical and FTS5 sync
still run normally. The UI shows `semantic: disabled` and the mode toggle
falls back to lexical with a warning banner.

## Lite vs Full

| | Lite (Pi 5) | Full (x86 box) |
|---|---|---|
| FTS5 | ON | ON |
| Embeddings | OFF (opt-in via profile) | ON |
| Backfill cap | 5 000 | 5 000 (override in config) |
| Model | bge-small-en (~33 MB onnx) | bge-small-en (same) |
| Cosine top-K | brute force numpy | brute force numpy |

## Dependencies

Required (already in scanpi):
- `fastapi`, `sqlite3` (stdlib)

Optional — install for semantic search:
- `onnxruntime` (~25 MB)
- `tokenizers` (~5 MB, rust-backed)
- `numpy`
- `huggingface_hub` (only used for first-run model download)

If any of these are missing, the worker logs a friendly message and the tool
runs lexical-only. **Importing `tools.search` itself never fails** — the
optional deps are guarded inside the worker.

## Manual model install (offline / restricted networks)

```bash
mkdir -p ~/scanpi/models/bge-small-en-v1.5
cd ~/scanpi/models/bge-small-en-v1.5
wget https://huggingface.co/BAAI/bge-small-en-v1.5/resolve/main/onnx/model.onnx
wget https://huggingface.co/BAAI/bge-small-en-v1.5/resolve/main/tokenizer.json
```

After placing the files, restart ScanPi (or just the search tool). The worker
detects them on next start and goes from `loading` -> `ready`.

## Troubleshooting

**Symptom**: `semantic: failed` in the UI header.
- Check logs for the actual import error. Most likely missing
  `onnxruntime` or `tokenizers`.
- If on a Pi 5, prefer `pip install onnxruntime` (NOT `onnxruntime-gpu`).

**Symptom**: `0 indexed` even though GMRS/OP25 has transcribed calls.
- Tool starts BEFORE the source DBs exist? The first sync after they appear
  re-attaches automatically. Wait one cycle (~10 s).
- Check `~/scanpi/search.db` permissions — if the search tool can't open it
  the sync thread dies silently.
- Look for `transcript_status='ok'` rows — pending/failed transcripts are
  not indexed.

**Symptom**: hybrid mode looks like just lexical.
- Until the embedding backfill finishes, semantic results are sparse. Watch
  `embedded_count` in `/search/health` climb. With backfill_limit=5000 and
  ~25 ms per call, the initial pass takes about two minutes on a Pi 5.

**Symptom**: query for `"foo bar"` returns nothing.
- The token sanitizer treats the quoted phrase as a literal FTS5 phrase
  match. If the exact phrase isn't in any transcript, no rows match. Try
  unquoted (`foo bar`) for prefix-matching each word.

**Symptom**: search.db fills up disk faster than expected.
- FTS5 + 384-dim embeddings = ~2 KB per row indexed. 100 000 rows = ~200 MB.
  Add a retention policy by source_id age if needed.

## Pi 5 performance

Numbers measured against the spec (not yet measured on hardware):

- FTS5 sync: 500 rows / cycle, every 10 s — negligible CPU.
- Embedding: ~25 ms per call (bge-small, batch=16, 2 threads). Initial
  backfill of 5 000 rows: ~130 s.
- Cosine top-K against 50 000 vectors: ~30 ms per search.
- DB writes: WAL mode, synchronous=NORMAL — safe and quick.

## Architecture notes

- We deliberately do **not** install FTS5 contentless triggers on the source
  DBs. Triggers across attached read-only DBs are a footgun; polling is
  simpler and the staleness window is bounded by `sync_interval_s`.
- We store embeddings in the same DB (not a separate `embeddings.db`) so
  cosine kNN can join in a single attached connection if we ever switch to
  sqlite-vss without changing the data layout.
- Reciprocal rank fusion (k=60) is the standard hybrid-IR merge — see
  Cormack et al., 2009. No tuning hyperparameter; just average the
  reciprocal ranks.
