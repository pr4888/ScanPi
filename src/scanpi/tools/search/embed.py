"""EmbeddingWorker — bge-small-en-v1.5 via onnxruntime, with graceful degradation.

Runs in a background thread:
  1. Tries to load the model (downloading if needed). If that fails — log a
     friendly message and idle. Lexical search still works.
  2. Pulls untemmbedded fts_meta rows in batches, encodes them, writes vectors
     to the embeddings BLOB table.
  3. On startup it backfills up to N existing rows (default 5000), then settles
     into incremental mode.

Dependencies (all optional — guarded so __init__.py imports don't crash):
  * onnxruntime
  * tokenizers (HF) or transformers
  * numpy
  * huggingface_hub (for model download) — falls back to manual download

The model file is ~33 MB onnx + tokenizer json. Saved to
~/scanpi/models/bge-small-en-v1.5/ by default.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384


class EmbeddingWorker:
    """Lazy-loading embedding worker.

    Construction is cheap and never throws on missing deps. start() spawns the
    thread which tries to load the model. on_status callback fires with one of:
      'loading' | 'ready' | 'failed' | 'disabled'
    """

    def __init__(
        self,
        db,                       # SearchDB
        model_dir: Path,
        model_name: str = DEFAULT_MODEL,
        backfill_limit: int = 5000,
        batch_size: int = 16,
        on_status: Callable[[str], None] | None = None,
    ):
        self.db = db
        self.model_dir = Path(model_dir)
        self.model_name = model_name
        self.backfill_limit = int(backfill_limit)
        self.batch_size = int(batch_size)
        self.on_status = on_status or (lambda s: None)

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._session = None  # onnxruntime InferenceSession
        self._tokenizer = None
        self._loaded = False

    # --- public ----------------------------------------------------------

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="search-embed-worker", daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def is_ready(self) -> bool:
        return self._loaded

    def encode_query(self, text: str):
        """Encode a single string for kNN search. Returns (np.ndarray, dim) or None."""
        if not self._loaded:
            return None
        try:
            import numpy as np
            vecs = self._encode_batch([text])
            if vecs is None or len(vecs) == 0:
                return None
            return vecs[0].astype(np.float32)
        except Exception:
            log.exception("encode_query failed")
            return None

    # --- worker loop -----------------------------------------------------

    def _run(self):
        ok = self._try_load()
        if not ok:
            self.on_status("failed")
            return
        self._loaded = True
        self.on_status("ready")
        log.info("embedding model ready: %s", self.model_name)

        # Backfill phase: process the most recent <backfill_limit> rows.
        self._backfill()

        # Incremental phase: poll for new untembedded rows.
        while not self._stop.is_set():
            try:
                pending = self.db.pending_embedding_ids(limit=self.batch_size * 4)
            except Exception:
                log.exception("pending_embedding_ids failed")
                pending = []
            if not pending:
                # No work — wait a bit longer.
                self._sleep(15.0)
                continue
            self._process_batch(pending[: self.backfill_limit])
            self._sleep(2.0)

    def _backfill(self):
        if self.backfill_limit <= 0:
            return
        log.info("embedding backfill start (limit=%d)", self.backfill_limit)
        processed = 0
        while processed < self.backfill_limit and not self._stop.is_set():
            pending = self.db.pending_embedding_ids(limit=min(self.batch_size * 4, 200))
            if not pending:
                break
            self._process_batch(pending)
            processed += len(pending)
            log.info("embedding backfill: %d/%d done", processed, self.backfill_limit)
        log.info("embedding backfill complete (%d rows)", processed)

    def _process_batch(self, pending: list[tuple[int, str]]):
        """Embed a batch and write back."""
        if not pending:
            return
        texts = [t or "" for _, t in pending]
        ids = [i for i, _ in pending]
        try:
            vecs = self._encode_batch(texts)
        except Exception:
            log.exception("batch encode failed; sleeping 30s")
            self._sleep(30.0)
            return
        if vecs is None:
            return
        for fts_id, vec in zip(ids, vecs):
            try:
                self.db.store_embedding(
                    fts_id, vec.astype("float32").tobytes(),
                    EMBED_DIM, self.model_name,
                )
            except Exception:
                log.exception("store_embedding failed for fts_id=%s", fts_id)

    def _sleep(self, seconds: float):
        end = time.time() + seconds
        while time.time() < end:
            if self._stop.is_set():
                return
            time.sleep(0.1)

    # --- model loading & inference --------------------------------------

    def _try_load(self) -> bool:
        """Best-effort model load.  Never throws on import errors."""
        try:
            import numpy as np  # noqa: F401
        except Exception:
            log.warning("numpy not available — semantic search disabled")
            return False
        try:
            import onnxruntime as ort  # noqa: F401
        except Exception:
            log.warning(
                "onnxruntime not available — install with `pip install onnxruntime` "
                "to enable semantic search; lexical search still works."
            )
            return False

        # Find or download the ONNX model + tokenizer.
        model_path = self.model_dir / "model.onnx"
        tokenizer_path = self.model_dir / "tokenizer.json"
        if not model_path.exists() or not tokenizer_path.exists():
            log.info("bge-small not found locally; attempting download to %s",
                     self.model_dir)
            try:
                self._download_model()
            except Exception:
                log.exception(
                    "model download failed — semantic search will run lexical-only. "
                    "To install manually: place model.onnx + tokenizer.json under %s",
                    self.model_dir,
                )
                return False

        try:
            from onnxruntime import InferenceSession, SessionOptions, GraphOptimizationLevel
            opts = SessionOptions()
            opts.graph_optimization_level = GraphOptimizationLevel.ORT_ENABLE_ALL
            opts.intra_op_num_threads = 2  # Pi-friendly
            self._session = InferenceSession(
                str(model_path), sess_options=opts, providers=["CPUExecutionProvider"],
            )
        except Exception:
            log.exception("onnxruntime InferenceSession creation failed")
            return False

        # Tokenizer — prefer HF tokenizers (rust-backed, fast)
        try:
            from tokenizers import Tokenizer
            self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
            self._tokenizer.enable_truncation(max_length=512)
            self._tokenizer.enable_padding(length=None)  # dynamic per batch
        except Exception:
            log.exception("tokenizer load failed (need `pip install tokenizers`)")
            return False
        return True

    def _download_model(self):
        """Download model + tokenizer using huggingface_hub if available, else manual."""
        self.model_dir.mkdir(parents=True, exist_ok=True)
        # Preferred path: huggingface_hub
        try:
            from huggingface_hub import hf_hub_download
            for fname in ["onnx/model.onnx", "tokenizer.json"]:
                local = hf_hub_download(
                    repo_id=self.model_name, filename=fname,
                    local_dir=str(self.model_dir), local_dir_use_symlinks=False,
                )
                # Move ONNX up one dir for convenience
                import shutil
                if fname.endswith("model.onnx"):
                    target = self.model_dir / "model.onnx"
                    if Path(local) != target:
                        shutil.copyfile(local, target)
            return
        except Exception:
            log.warning("huggingface_hub download failed; trying urllib fallback")

        # Manual fallback — direct URLs
        import urllib.request
        base = f"https://huggingface.co/{self.model_name}/resolve/main"
        for url, target in [
            (f"{base}/onnx/model.onnx", self.model_dir / "model.onnx"),
            (f"{base}/tokenizer.json", self.model_dir / "tokenizer.json"),
        ]:
            log.info("downloading %s -> %s", url, target)
            urllib.request.urlretrieve(url, target)

    def _encode_batch(self, texts: list[str]):
        """Run a batch through the model. Returns np.ndarray (B, 384) or None."""
        if not self._session or not self._tokenizer:
            return None
        if not texts:
            return None
        import numpy as np

        # bge-small expects "represent this sentence: ..." style prompts only
        # for queries when used for retrieval; for indexing, raw text is fine.
        encoded = self._tokenizer.encode_batch([t[:2000] for t in texts])
        # Build numpy arrays
        ids = np.array([e.ids for e in encoded], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
        # bge models also expect token_type_ids
        type_ids = np.zeros_like(ids, dtype=np.int64)

        feeds = {"input_ids": ids, "attention_mask": mask, "token_type_ids": type_ids}
        # Filter to inputs the model actually expects (older exports may lack token_type_ids)
        wanted = {i.name for i in self._session.get_inputs()}
        feeds = {k: v for k, v in feeds.items() if k in wanted}

        try:
            outs = self._session.run(None, feeds)
        except Exception:
            log.exception("onnx run() failed")
            return None
        # Sentence-transformer ONNX exports vary: some return last_hidden_state,
        # some return pooled output directly. Pool with mean+mask if needed.
        out = outs[0]
        if out.ndim == 3:  # (B, T, H) — apply mean pooling with attention mask
            mask_f = mask.astype(np.float32)[..., None]
            summed = (out * mask_f).sum(axis=1)
            denom = np.clip(mask_f.sum(axis=1), 1e-6, None)
            pooled = summed / denom
        else:
            pooled = out
        # L2 normalize
        norm = np.linalg.norm(pooled, axis=1, keepdims=True)
        norm = np.clip(norm, 1e-8, None)
        return (pooled / norm).astype(np.float32)


# ---------- helpers used by the API for kNN against the BLOB table ----------

def topk_cosine(query_vec, db, k: int = 50) -> list[tuple[int, float]]:
    """Brute-force cosine top-K against every embedding in the DB.

    For tens of thousands of vectors on a Pi 5 this is fine — numpy can
    multiply a (N, 384) matrix by a (384,) vector in <50 ms. If the count
    grows past ~200k, swap in sqlite-vss or annoy.
    """
    try:
        import numpy as np
    except Exception:
        return []
    q = np.asarray(query_vec, dtype=np.float32)
    qn = float(np.linalg.norm(q))
    if qn < 1e-8:
        return []
    q = q / qn

    ids: list[int] = []
    rows: list[bytes] = []
    for fts_id, blob in db.fetch_all_embeddings():
        ids.append(fts_id)
        rows.append(blob)
    if not ids:
        return []
    mat = np.frombuffer(b"".join(rows), dtype=np.float32).reshape(len(ids), -1)
    if mat.shape[1] != q.shape[0]:
        return []
    # Vectors stored already-normalized -> dot product == cosine similarity.
    sims = mat @ q
    if k >= len(ids):
        order = np.argsort(-sims)
    else:
        # argpartition for top-k, then sort just those
        idx = np.argpartition(-sims, k)[:k]
        order = idx[np.argsort(-sims[idx])]
    out = []
    for i in order:
        out.append((ids[int(i)], float(sims[int(i)])))
    return out
