"""`scanpi-v3` — unified ScanPi v0.3 entry point (tool framework)."""
from __future__ import annotations

import argparse
from pathlib import Path


def main():
    p = argparse.ArgumentParser(prog="scanpi-v3",
                                description="ScanPi v0.3 — unified tool framework")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--data-dir", type=Path, default=Path.home() / "scanpi")
    args = p.parse_args()

    from .app_v3 import run_v3
    run_v3(host=args.host, port=args.port, data_dir=args.data_dir)


if __name__ == "__main__":
    main()
