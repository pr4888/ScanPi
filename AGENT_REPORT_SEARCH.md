# Agent SEARCH — report

## What shipped

Fully working `src/scanpi/tools/search/` package with six files:

| File | Lines | Purpose |
|---|---|---|
| `__init__.py` | ~190 | `SearchTool(Tool)` with `needs_sdr=False`, lifecycle that spins up an FTS5 sync thread plus an optional embedding worker. |
| `db.py` | ~400 | `SearchDB` — opens `search.db` with `uri=True`, ATTACHes `gmrs.db` and `op25.db` via `file:.../path?mode=ro`, owns the `fts_calls` virtual table + `fts_meta` shadow table + `embeddings` BLOB table + `sync_watermarks`. Polling sync (no triggers across attached DBs). |
| `embed.py` | ~270 | `EmbeddingWorker` thread. Lazy-loads bge-small-en-v1.5 via onnxruntime + tokenizers; downloads to `~/scanpi/models/` on first run; auto-fails gracefully if any optional dep is missing. `topk_cosine()` does brute-force float32 dot product against an in-RAM matrix. |
| `api.py` | ~190 | FastAPI `APIRouter` with `/search`, `/search/similar/{src}/{id}`, `/search/health`. Hybrid mode uses RRF (k=60). Lexical-only is the default and always-available fallback. |
| `page.html` | ~280 | Single-file dark-theme search UI matching `tools/gmrs/page.html` aesthetic. Mode toggle (lex/sem/hyb), since dropdown, source filter, audio playback, "find similar" buttons, mobile-friendly flex layout, viewport meta. Vanilla JS only. |
| `README.md` | ~190 | Tool overview, config keys, lite vs full, troubleshooting, manual model install instructions. |

## Verification (local, against fixture DBs)

Ran an end-to-end test that:
1. Created mock `gmrs.db` (3 rows: 2 ok transcripts + 1 pending) and `op25.db` (2 rows).
2. Started `SearchTool` against the temp dir.
3. Confirmed FTS5 sync ran and indexed exactly the 4 transcribed rows (pending row excluded).
4. Confirmed `_db.search_fts("fire")` returned both fire-related rows with `<mark>` snippets.
5. Spun up FastAPI `TestClient`, hit `/search`, `/search/health`, `/search/similar`, `/search` with `mode=hybrid` and `mode=semantic`. All returned 200, with appropriate `warnings` array when semantic is unavailable.
6. Verified `audio_url` correctly resolves to `/tools/gmrs/api/clip/{id}` and `/tools/op25/api/clip/{id}`.
7. Verified the package imports cleanly when `onnxruntime` is **not** installed (current local env), and that toggling `SCANPI_FEATURE_SEMANTIC_SEARCH=1` makes the worker attempt to load and gracefully transition `loading -> failed` when the model can't be loaded — without raising.

Also fixed two issues caught during testing:
- ATTACH-with-URI requires `uri=True` on the main connection; without that, sqlite tries to literally open `file:C:/.../path?mode=ro` as a path.
- FastAPI `Query(regex=...)` is deprecated; switched to `pattern=...`.

## What's stubbed / deferred

Nothing intentionally stubbed. The semantic path is real but **untested with a real model load** because:
- The local dev box doesn't have `onnxruntime` installed.
- I deliberately did NOT auto-install or download anything per the contract's "no Pi touching" rule and to keep your dev env minimal.

Once `onnxruntime` + `tokenizers` are in the venv and `SCANPI_FEATURE_SEMANTIC_SEARCH=1`, the worker will:
1. Try `huggingface_hub.hf_hub_download` for `BAAI/bge-small-en-v1.5/onnx/model.onnx` and `tokenizer.json`.
2. Fall back to `urllib.request.urlretrieve` if `huggingface_hub` is missing.
3. If both fail (no internet), log a clear "place files manually under `~/scanpi/models/bge-small-en-v1.5/`" message and idle in `failed` state.

Worth a smoke test on the Pi after deploy: run with the flag enabled, watch logs, confirm the model downloads and `embedded_count` climbs in `/search/health`.

## Integration concerns / requests

1. **Three-line registration in `app_v3.py`** — I left the exact snippet at the top of `__init__.py` as a comment block. It plugs in right after the `YardstickTool` registration around line 388:

   ```python
   from .tools.search import SearchTool
   registry.register(SearchTool(config={"data_dir": str(data_dir)}))
   # SearchTool has needs_sdr=False so it auto-starts via coord.start_non_sdr_tools().
   ```

2. **Profile module fallback** — I import `from ...profile import feature_enabled`, fall back to `os.environ.get("SCANPI_FEATURE_SEMANTIC_SEARCH", "0") == "1"` if Agent INSTALL hasn't shipped `profile.py` yet. No coordination needed, but worth knowing.

3. **Database path assumptions** — Hard-codes `gmrs.db` and `op25.db` filenames inside `data_dir`. If those tools ever rename their DBs the search tool won't find them. Override via `gmrs_db` / `op25_db` config keys if needed.

4. **Audio URL contract** — The result `audio_url` field assumes `/tools/gmrs/api/clip/{id}` and `/tools/op25/api/clip/{id}` continue to exist. Both currently do (verified by reading their `api_router()` definitions). If those endpoints ever move, only `db.py`'s `_sync_gmrs` / `_sync_op25` methods need to change.

5. **Optional deps** — When/if the Pi install script ships, recommend adding to lite: `pip install onnxruntime tokenizers numpy huggingface_hub`. Total weight ~50 MB. Without them the tool runs lexical-only and the UI shows `semantic: failed`. Lexical alone is genuinely useful; the gate is just on the embedding pieces.

6. **Embedding storage growth** — At ~1.6 KB/row (1.5 KB vec + ~100 B FTS overhead), 50 000 calls = ~80 MB. No retention policy yet. If transcripts grow large, recommend a follow-up to age out old `embeddings` rows or vacuum on a schedule.

## Files written

- `C:\Users\rdcst\ScanPi-canonical\src\scanpi\tools\search\__init__.py`
- `C:\Users\rdcst\ScanPi-canonical\src\scanpi\tools\search\db.py`
- `C:\Users\rdcst\ScanPi-canonical\src\scanpi\tools\search\embed.py`
- `C:\Users\rdcst\ScanPi-canonical\src\scanpi\tools\search\api.py`
- `C:\Users\rdcst\ScanPi-canonical\src\scanpi\tools\search\page.html`
- `C:\Users\rdcst\ScanPi-canonical\src\scanpi\tools\search\README.md`
