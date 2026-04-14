# 2026-04-13 Session Artifacts — GMRS Prototype Archive

Experimental files from the 2026-04-13 session that briefly coupled ScanPi with Heimdall.
Reverted per Patrick's direction — ScanPi and Heimdall are independent projects.

- `gmrs_intel.py` — Heimdall gateway module adding POST /v1/gmrs/event + stats + UI.
  Was deployed to Spark at ~/heimdall/scripts/gmrs_intel.py then removed.
- `patch_context.py` — wired get_gmrs_activity() into heimdall_context.build_full_context.
  Was applied then rolled back via the .bak backup.

Kept for reference only. If we ever want Heimdall to pull GMRS events from ScanPi,
start here — but the correct path is probably the other direction (Heimdall fills
its own GMRS capability gap using its own keeper hardware).
