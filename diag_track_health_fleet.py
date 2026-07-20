"""Fleet-wide Track health scan — how many products have dead Tracks?

Adam 2026-07-15: HYF's Track was dead 9+ hours silently. His instinct
is right: probably not the only one. This diag scans EVERY product
on the tenant, computes sleeve-scoped event count in the window, and
flags any where the SwingTrader.step has clearly not been ticking.

Detection logic (same as per-product diag_track_lifecycle):
  sleeve-scoped events > 0 → Track ALIVE
  sleeve-scoped events = 0 → Track DEAD
  track_spawn_failed > 0    → Track failed to spawn (root cause visible)

Reports each product's status + overall fleet %-alive so we can see
at-a-glance whether one bad Track or a systemic failure.

Read-only. Usage:
    python3 diag_track_health_fleet.py                  # 24h window
    python3 diag_track_health_fleet.py 60               # last 60min
"""
from __future__ import annotations
import os
import sys
import time
from collections import defaultdict


def _fmt_age(ts) -> str:
    try:
        age = int(time.time() - float(ts))
        if age < 60:
            return f"{age}s"
        if age < 3600:
            return f"{age // 60}m"
        return f"{age // 3600}h"
    except Exception:
        return "?"


def main() -> None:
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 1440.0
    tenant = "adam-live"

    print("=" * 118)
    print(f"FLEET-WIDE TRACK HEALTH — tenant={tenant}  window={minutes:.0f}min")
    print("=" * 118)

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))

    # Load all events in window, group by product
    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    except Exception as e:
        print(f"\n✗ trade log load failed: {e}")
        return

    cutoff = time.time() - minutes * 60
    events_by_product = defaultdict(list)
    for e in log.events():
        if not isinstance(e, dict):
            continue
        ts = float(e.get("ts") or 0)
        if ts < cutoff:
            continue
        sym = str(e.get("symbol") or "")
        if not sym or sym.startswith("__"):
            continue
        events_by_product[sym].append(e)

    # Get all products the tenant has configs for
    all_products = set()
    for tid in store.list_tenants():
        if tid != tenant:
            continue
        for sym in store.list_symbols(tid):
            if sym.startswith("__"):
                continue
            all_products.add(sym)

    # Also include any product with events (in case config was removed)
    all_products.update(events_by_product.keys())

    # Analyze each product
    products_data = []
    for sym in sorted(all_products):
        events = events_by_product.get(sym, [])
        total = len(events)
        sleeve_events = sum(1 for e in events if e.get("sleeve_id"))
        spawn_failed = sum(1 for e in events if e.get("event_type") == "track_spawn_failed")
        recon = sum(1 for e in events
                     if str(e.get("event_type", "")).startswith("reconciliation_"))
        # Newest sleeve-scoped event ts (Track's last known tick)
        last_sleeve_ts = 0
        for e in events:
            if e.get("sleeve_id"):
                ts = float(e.get("ts") or 0)
                if ts > last_sleeve_ts:
                    last_sleeve_ts = ts

        # State check: does this product have any ARMED sleeve that SHOULD tick?
        state = store.get_state(tenant, sym) or {}
        sleeves_state = state.get("sleeves") or {}
        has_armed_sleeve = any(
            str(ss.get("state") or "") in ("ARMED_BUY", "ARMED_SELL")
            for ss in sleeves_state.values()
        )
        held_position = False
        try:
            pf = store.get_state(tenant, "__portfolio__") or {}
            snap = pf.get(sym) or {}
            held_position = float(snap.get("position_qty") or 0) != 0
        except Exception:
            pass

        # Verdict — Adam 2026-07-19 FIX: prior code said "ALIVE" for any
        # product with sleeve_events > 0 in the WHOLE window (24h default).
        # That labelled ZEC "ALIVE" while its LAST TICK was 1h ago. A
        # Track that hasn't ticked in >60s can't ratchet stops, can't
        # detect fills, can't apply HWM floor — it is NOT alive
        # operationally, even if it emitted 378 events earlier today.
        FRESH_TICK_MAX_SECS = 60
        SILENT_ZOMBIE_MAX_SECS = 600
        age = (time.time() - last_sleeve_ts) if last_sleeve_ts > 0 else float("inf")
        if spawn_failed > 0:
            status = "SPAWN-FAIL"
        elif has_armed_sleeve or held_position:
            if last_sleeve_ts == 0:
                status = "⚠ DEAD"
            elif age <= FRESH_TICK_MAX_SECS:
                status = "ALIVE"
            elif age <= SILENT_ZOMBIE_MAX_SECS:
                status = "⚠ SILENT"  # ticked recently but not now
            else:
                status = "💀 ZOMBIE"  # silent longer than auto-recovery threshold
        elif sleeve_events > 0:
            status = "ALIVE"  # ticked at least once, no current need to tick
        else:
            status = "idle"  # nothing armed, expected quiet

        products_data.append({
            "symbol": sym,
            "status": status,
            "sleeve_events": sleeve_events,
            "total_events": total,
            "spawn_failed": spawn_failed,
            "recon": recon,
            "last_sleeve_ts": last_sleeve_ts,
            "has_armed": has_armed_sleeve,
            "held": held_position,
            "sleeve_count": len(sleeves_state),
        })

    # Print
    print(f"\n{'PRODUCT':22s} {'STATUS':12s} {'SLEEVE-EVTS':>12s} "
          f"{'SPAWN-FAIL':>11s} {'RECON':>7s} {'LAST TICK':>12s} "
          f"{'ARMED?':>7s} {'HELD?':>6s}")
    print("-" * 118)
    for d in products_data:
        last_tick = _fmt_age(d["last_sleeve_ts"]) + " ago" if d["last_sleeve_ts"] else "—"
        armed = "yes" if d["has_armed"] else ""
        held = "yes" if d["held"] else ""
        print(f"{d['symbol']:22s} {d['status']:12s} {d['sleeve_events']:>12d} "
              f"{d['spawn_failed']:>11d} {d['recon']:>7d} {last_tick:>12s} "
              f"{armed:>7s} {held:>6s}")

    print(f"\n[SUMMARY]")
    alive = sum(1 for d in products_data if d["status"] == "ALIVE")
    silent = sum(1 for d in products_data if d["status"] == "⚠ SILENT")
    zombie = sum(1 for d in products_data if d["status"] == "💀 ZOMBIE")
    dead = sum(1 for d in products_data if d["status"] == "⚠ DEAD")
    spawn_fail = sum(1 for d in products_data if d["status"] == "SPAWN-FAIL")
    idle = sum(1 for d in products_data if d["status"] == "idle")
    total = len(products_data)
    print(f"  ALIVE:      {alive:>3d}  ({100.0*alive/total if total else 0:.0f}%)  ← ticked in last 60s (constitution §3.2)")
    print(f"  ⚠ SILENT:   {silent:>3d}  ({100.0*silent/total if total else 0:.0f}%)  ← ticked in the past but > 60s ago (stops not ratcheting)")
    print(f"  💀 ZOMBIE:  {zombie:>3d}  ({100.0*zombie/total if total else 0:.0f}%)  ← silent > 600s, auto-recovery should have fired")
    print(f"  ⚠ DEAD:     {dead:>3d}  ({100.0*dead/total if total else 0:.0f}%)  ← never ticked with an armed sleeve")
    print(f"  SPAWN-FAIL: {spawn_fail:>3d}  ({100.0*spawn_fail/total if total else 0:.0f}%)  ← tried to spawn but errored")
    print(f"  idle:       {idle:>3d}  ({100.0*idle/total if total else 0:.0f}%)  ← nothing to tick (no armed sleeve, no position)")

    if silent > 0 or zombie > 0 or dead > 0 or spawn_fail > 0:
        problem_syms = [d["symbol"] for d in products_data
                        if d["status"] in ("⚠ SILENT", "💀 ZOMBIE", "⚠ DEAD", "SPAWN-FAIL")]
        print(f"\n⚠ FLEET-WIDE FIX NEEDED — §3.2 violation")
        print(f"  {silent + zombie + dead + spawn_fail} product(s) not ticking despite "
              f"armed sleeves / held positions:")
        for sym in problem_syms:
            print(f"    - {sym}")
        print(f"")
        print(f"  Force-respawn without Render restart:")
        print(f"    python3 diag_force_track_respawn.py {' '.join(problem_syms)}")
        print(f"    python3 diag_force_track_respawn.py {' '.join(problem_syms)} --apply")
        print(f"  Then re-run this diag in 30s to confirm they came back.")
    else:
        print(f"\n✓ Fleet healthy — every product with an armed sleeve is ticking (< 60s).")
    print("=" * 118)


if __name__ == "__main__":
    main()
