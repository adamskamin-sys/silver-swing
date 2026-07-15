"""Replay the auto-recovery discovery logic + print exactly what it finds.

Adam 2026-07-15: auto-recovery keeps finding only AVE, even after
per-product try/except fix. This diag runs the SAME logic as
_maybe_recover_dead_tracks would, step by step, and prints
every check + result — so we can see exactly which check filters
out the other 8 products.

Read-only. Usage:
    python3 diag_discovery_replay.py
"""
from __future__ import annotations
import os
import sys


def main() -> None:
    tenant = "adam-live"
    # In live_runner these come from env; hard-code equivalents here
    SYMBOL = os.getenv("SWING_SYMBOL", "SLR-27AUG26-CDE")

    print("=" * 100)
    print(f"DISCOVERY REPLAY — tenant={tenant}  SYMBOL={SYMBOL}")
    print("=" * 100)

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))

    should_track_critical: set[str] = set()
    should_track_regular: set[str] = set()

    # Phase 1: Held positions
    print(f"\n[PHASE 1] Held positions from __portfolio__:")
    try:
        pf = store.get_state(tenant, "__portfolio__") or {}
        print(f"  __portfolio__ has {len(pf)} keys total")
        for sym, snap in pf.items():
            if sym.startswith("__") or sym == SYMBOL:
                print(f"    · {sym}: SKIPPED ({'starts with __' if sym.startswith('__') else 'is primary'})")
                continue
            if not isinstance(snap, dict):
                print(f"    · {sym}: not a dict, type={type(snap).__name__}")
                continue
            qty = float(snap.get("position_qty") or 0)
            if qty != 0:
                should_track_critical.add(sym)
                print(f"    ✓ {sym}: HELD (qty={qty}) → critical")
            else:
                pass  # not held, don't clutter output
    except Exception as e:
        print(f"  ✗ error: {type(e).__name__}: {e}")

    print(f"\n  should_track_critical: {sorted(should_track_critical)}")

    # Phase 2: Armed sleeves
    print(f"\n[PHASE 2] Armed sleeves — list_symbols + per-product get_state:")
    try:
        symbols = list(store.list_symbols(tenant))
        print(f"  list_symbols returned {len(symbols)} symbols: {symbols}")
    except Exception as e:
        print(f"  ✗ list_symbols failed: {type(e).__name__}: {e}")
        return

    for sym in symbols:
        if sym.startswith("__"):
            print(f"    · {sym}: SKIP (__)")
            continue
        if sym == SYMBOL:
            print(f"    · {sym}: SKIP (primary)")
            continue
        if sym in should_track_critical:
            print(f"    · {sym}: SKIP (already in critical)")
            continue
        try:
            st = store.get_state(tenant, sym) or {}
            sleeves = st.get("sleeves") or {}
            if not sleeves:
                print(f"    · {sym}: no sleeves")
                continue
            found_armed = False
            armed_sleeves = []
            all_states = []
            for sid, ss in sleeves.items():
                sstate = str(ss.get("state") or "")
                all_states.append(f"{sid}={sstate!r}")
                if sstate in ("ARMED_BUY", "ARMED_SELL"):
                    found_armed = True
                    armed_sleeves.append(sid)
            if found_armed:
                should_track_regular.add(sym)
                print(f"    ✓ {sym}: ARMED via {armed_sleeves} → regular (states: {all_states})")
            else:
                print(f"    · {sym}: sleeves not armed (states: {all_states})")
        except Exception as e:
            print(f"    ✗ {sym}: EXCEPTION {type(e).__name__}: {e}")

    print(f"\n  should_track_regular: {sorted(should_track_regular)}")
    print(f"\n[TOTAL] Products auto-recovery SHOULD discover: "
          f"{sorted(should_track_critical | should_track_regular)}")
    print(f"  count: {len(should_track_critical | should_track_regular)}")
    print("=" * 100)


if __name__ == "__main__":
    main()
