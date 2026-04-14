"""HTTP Range-aware audio serving helper.

Why: FastAPI/Starlette's FileResponse does not natively implement the
Range request semantics HTML5 `<audio>` elements use to seek/stream.
Without Range support, some browsers stop playback after the first chunk,
giving the illusion that the file is truncated.

Usage (FastAPI):

    from fastapi import FastAPI, Request, HTTPException
    from audio_range_serve import serve_file_with_range

    app = FastAPI()

    @app.get("/audio/{name}")
    def audio(name: str, request: Request):
        path = f"/var/rfforge/audio/{name}"
        if not os.path.exists(path):
            raise HTTPException(404)
        return serve_file_with_range(path, "audio/wav", request)

Also works with Flask — the `request` arg just needs a `.headers.get("range")`
method. Return value has `.content`, `.status_code`, `.headers` compatible
with Starlette/FastAPI. Adapt as needed for other frameworks.

Keeps the whole file in memory for deterministic repeat-fetch behavior —
WAV clips are tens to hundreds of KB, so this is cheap. For large files
(> ~10 MB), switch to `open()+seek()` inside the response body.
"""
from __future__ import annotations

import os


def serve_file_with_range(path: str, media_type: str, request):
    """Return a Response that supports HTTP Range requests.

    If no Range header: 200 OK full file with Accept-Ranges: bytes.
    If Range "bytes=N-M" (or "N-" or "-N"): 206 Partial Content with slice.
    If Range unparseable or out of bounds: 416.
    """
    from fastapi.responses import Response

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

    # RFC 7233 "bytes=START-END" with either side optional.
    try:
        _, rng = range_header.split("=", 1)
        start_s, end_s = rng.split("-", 1)
        if start_s == "" and end_s != "":
            # Suffix form "-N" = last N bytes.
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
