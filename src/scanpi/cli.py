"""CLI entry point."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        prog="scanpi",
        description="ScanPi — self-contained Pi 5 radio scanner",
    )
    parser.add_argument(
        "-c", "--config",
        type=Path,
        help="Path to config.toml (default: ~/scanpi/config.toml)",
    )
    parser.add_argument(
        "-p", "--port",
        type=int,
        help="Web UI port (default: 8080)",
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="Create default config and exit",
    )
    parser.add_argument(
        "--survey-only",
        action="store_true",
        help="Run one survey sweep and exit",
    )

    args = parser.parse_args()

    if args.init:
        from .config import ScanConfig
        cfg = ScanConfig()
        cfg.save()
        print(f"Config written to: {cfg.data_dir / 'config.toml'}")
        print(f"Data directory: {cfg.data_dir}")
        print("Edit config.toml then run: scanpi")
        return

    if args.survey_only:
        import asyncio
        from .config import ScanConfig
        from .db import ScanPiDB
        from .surveyor import Surveyor
        import logging
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

        cfg = ScanConfig.load(args.config)
        db = ScanPiDB(cfg.db_path)
        db.connect()
        s = Surveyor(cfg, db)
        detections = asyncio.run(s.full_survey())
        print(f"\nFound {len(detections)} signals:")
        for d in sorted(detections, key=lambda x: -x.snr_db)[:20]:
            print(f"  {d.freq_hz/1e6:10.4f} MHz  SNR={d.snr_db:5.1f} dB  Power={d.power_db:6.1f} dBFS")
        db.close()
        return

    from .app import run
    run(args.config)


if __name__ == "__main__":
    main()
