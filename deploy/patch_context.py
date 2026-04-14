#!/usr/bin/env python3
"""Wire gmrs_intel.get_gmrs_activity into heimdall_context.build_full_context."""
import sys

P = '/home/sparks246/heimdall/scripts/heimdall_context.py'

with open(P) as f:
    src = f.read()

if 'gmrs_intel' in src:
    print('CONTEXT_ALREADY_WIRED')
    sys.exit(0)

# 1. Insert helper function right before build_full_context
helper = '''

def get_gmrs_activity():
    """Pull GMRS/FRS TX events logged by scanpi-gmrs forwarder. 24h window."""
    try:
        import gmrs_intel
        return gmrs_intel.get_gmrs_activity(hours=24.0)
    except Exception:
        return None


'''
src = src.replace('def build_full_context(', helper + 'def build_full_context(', 1)

# 2. Add to parallel futures
old_fut = '    futures["modes"] = _executor.submit(get_keeper_modes, keeper_registry)'
new_fut = old_fut + '\n    futures["gmrs"] = _executor.submit(get_gmrs_activity)'
src = src.replace(old_fut, new_fut, 1)

# 3. Add section renderer before the final return
section_block = '''
    # --- GMRS/FRS activity (ScanPi-fed) ---
    gmrs = results.get("gmrs")
    if gmrs and gmrs.get("channels"):
        lines = [f"GMRS/FRS ACTIVITY (24h, {gmrs['total_events']} events, {gmrs['active_channels']} active channels):"]
        for c in gmrs["channels"][:8]:
            lines.append(
                f"  Ch {c['channel']:2d} ({c['freq_mhz']:.4f} MHz, {c['service']}): "
                f"{c['tx_count']} TX, airtime {c['total_airtime_s']:.0f}s, "
                f"peak {c['peak_rssi_max']:.0f} dBFS"
            )
        sections.append("\\n".join(lines))
    elif gmrs is not None:
        sections.append("GMRS/FRS ACTIVITY: no transmissions logged in last 24h.")

'''

needle = 'return "\\n\\n".join(sections)'
if needle in src:
    src = src.replace(needle, section_block + '    ' + needle, 1)
    print('CONTEXT_WIRED')
else:
    print('NEEDLE_NOT_FOUND')
    sys.exit(1)

with open(P, 'w') as f:
    f.write(src)
