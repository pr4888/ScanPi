"""`scanpi-gmrs` — standalone GMRS/FRS neighborhood activity monitor."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path

import uvicorn

from .api import create_app
from .monitor import MonitorConfig
from .service import GmrsService


DEFAULT_DATA = Path.home() / "scanpi"


def main():
    p = argparse.ArgumentParser(prog="scanpi-gmrs", description="GMRS/FRS neighborhood monitor")
    p.add_argument("--db", type=Path, default=DEFAULT_DATA / "gmrs.db")
    p.add_argument("--audio-dir", type=Path, default=DEFAULT_DATA / "gmrs_audio")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--gain", type=float, default=40.0)
    p.add_argument("--squelch-db", type=float, default=-30.0)
    p.add_argument("--gateway-url", default=os.environ.get("HEIMDALL_GATEWAY_URL"),
                   help="e.g. http://spark-heimdall:8900 (or set HEIMDALL_GATEWAY_URL)")
    p.add_argument("--gateway-token", default=os.environ.get("HEIMDALL_GATEWAY_TOKEN"))
    p.add_argument("--keeper-id", default=os.environ.get("SCANPI_KEEPER_ID", "scanpi"))
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    cfg = MonitorConfig(rtl_gain=args.gain, squelch_db=args.squelch_db)
    service = GmrsService(
        db_path=args.db,
        audio_dir=args.audio_dir,
        cfg=cfg,
        gateway_url=args.gateway_url,
        gateway_token=args.gateway_token,
        keeper_id=args.keeper_id,
    )

    def _shutdown(signum, frame):
        logging.info("signal %s — stopping", signum)
        service.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    service.start()
    app = create_app(service)
    logging.info("GMRS monitor UI: http://%s:%d/  gateway=%s",
                 args.host, args.port,
                 "ENABLED" if (args.gateway_url and args.gateway_token) else "disabled")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
