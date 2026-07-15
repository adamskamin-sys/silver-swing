"""Check whether products have top-level configs (or just sleeve state).

Adam 2026-07-15: verify whether commit 96ae887 auto-seed fired for
the 9 dead products. If configs exist for them, my fix worked and
the issue is elsewhere. If configs still missing, deploy hasn't
landed OR my code has a bug.

Read-only. Usage:
    python3 diag_config_check.py
"""
from __future__ import annotations
import os
import sys
import time


def main() -> None:
    tenant = "adam-live"
    print("=" * 100)
    print(f"CONFIG PRESENCE — tenant={tenant}")
    print("=" * 100)

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))

    products = [s for s in store.list_symbols(tenant)
                if not s.startswith("__")]
    print(f"\n{'PRODUCT':22s} {'HAS CONFIG?':>11s} {'CONFIG SIZE':>11s} "
          f"{'ARMED SLEEVES':>13s}  NOTES")
    print("-" * 100)
    for sym in sorted(products):
        cfg = store.get_config(tenant, sym) or {}
        state = store.get_state(tenant, sym) or {}
        sleeves = state.get("sleeves") or {}
        armed_count = sum(
            1 for ss in sleeves.values()
            if str(ss.get("state") or "") in ("ARMED_BUY", "ARMED_SELL"))
        has_cfg = "yes" if cfg else "NO"
        cfg_size = len(cfg) if cfg else 0
        notes = []
        if not cfg and armed_count > 0:
            notes.append("⚠ armed sleeve BUT no config — spawn blocked")
        if cfg and cfg.get("_auto_seeded"):
            notes.append("auto-seeded")
        note_str = "; ".join(notes)
        print(f"{sym:22s} {has_cfg:>11s} {cfg_size:>11d} {armed_count:>13d}  {note_str}")

    # Also scan trade log for auto-seed events
    print(f"\n[TRADE LOG] recent non_primary_config_auto_seeded events:")
    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        cutoff = time.time() - 600  # 10 min window
        seed_events = []
        seed_fail_events = []
        for e in log.events():
            if not isinstance(e, dict):
                continue
            if float(e.get("ts") or 0) < cutoff:
                continue
            et = str(e.get("event_type") or "")
            if et == "non_primary_config_auto_seeded":
                seed_events.append(e)
            elif et == "non_primary_config_auto_seed_failed":
                seed_fail_events.append(e)
        print(f"  auto_seeded count:        {len(seed_events)}")
        print(f"  auto_seed_failed count:   {len(seed_fail_events)}")
        for e in seed_events[-10:]:
            print(f"    ✓ {e.get('symbol')} @ {time.strftime('%H:%M:%S', time.localtime(e.get('ts', 0)))}")
        for e in seed_fail_events[-5:]:
            print(f"    ✗ {e.get('symbol')} @ {time.strftime('%H:%M:%S', time.localtime(e.get('ts', 0)))}: {e.get('error')}")
    except Exception as e:
        print(f"  trade log scan failed: {e}")

    print("=" * 100)


if __name__ == "__main__":
    main()
