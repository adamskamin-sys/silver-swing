"""Enable/disable per-sleeve adaptive_trail_enabled + tune adaptive_trail_k.

Adam 2026-07-15: live-adaptive trail_distance (commit 8458a66) is off
by default. This diag flips it per sleeve without dashboard clicks
and without a bot restart.

Read-only when called with no toggle args. Pass on/off + optional k
to change. Writes via state_patch queue so it lands on the bot's
next tick.

Usage:
    python3 diag_adaptive_trail_toggle.py                                    # list all
    python3 diag_adaptive_trail_toggle.py PRODUCT_ID SLEEVE_ID on --apply    # enable
    python3 diag_adaptive_trail_toggle.py PRODUCT_ID SLEEVE_ID off --apply   # disable
    python3 diag_adaptive_trail_toggle.py PRODUCT_ID SLEEVE_ID on 2.0 --apply  # enable + set k=2.0
"""
from __future__ import annotations
import os
import sys
import time


def main() -> None:
    tenant = "adam-live"
    args = sys.argv[1:]
    apply = "--apply" in args
    if apply:
        args.remove("--apply")

    print("=" * 100)
    print(f"ADAPTIVE TRAIL TOGGLE — tenant={tenant}")
    print("=" * 100)

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))

    # Listing (no args)
    if len(args) == 0:
        print(f"\n{'PRODUCT':22s} {'SLEEVE':14s} {'ADAPTIVE':10s} {'k':>6s} {'static_dist':>12s}")
        print(f"{'-' * 70}")
        for tid in store.list_tenants():
            if tid != tenant:
                continue
            for sym in store.list_symbols(tid):
                if sym.startswith("__"):
                    continue
                cfg = store.get_config(tid, sym) or {}
                for sc in (cfg.get("sleeves") or []):
                    enabled = sc.get("adaptive_trail_enabled", False)
                    k = sc.get("adaptive_trail_k", 2.5)
                    dist = sc.get("trail_distance", 0)
                    flag = "ENABLED" if enabled else "off"
                    print(f"{sym:22s} {sc.get('id', '?'):14s} {flag:10s} "
                          f"{k:>6.2f} ${dist:>11.4f}")
        print(f"\n(read-only — pass PRODUCT SLEEVE on|off [k] --apply to change)")
        return

    if len(args) < 3:
        print("USAGE: diag_adaptive_trail_toggle.py PRODUCT_ID SLEEVE_ID on|off [k] [--apply]")
        return
    product_id, sleeve_id, on_off = args[0], args[1], args[2].lower()
    if on_off not in ("on", "off"):
        print(f"ERROR: 3rd arg must be 'on' or 'off', got '{on_off}'")
        return
    new_enabled = (on_off == "on")
    new_k = None
    if len(args) >= 4:
        try:
            new_k = float(args[3])
        except ValueError:
            print(f"ERROR: 4th arg (k) must be a number, got '{args[3]}'")
            return

    cfg = store.get_config(tenant, product_id) or {}
    sleeves = list(cfg.get("sleeves") or [])
    target = None
    for sc in sleeves:
        if sc.get("id") == sleeve_id:
            target = sc
            break
    if target is None:
        print(f"\n✗ sleeve {sleeve_id} not in {product_id}")
        print(f"  Existing sleeves: {[s.get('id') for s in sleeves]}")
        return

    current_enabled = bool(target.get("adaptive_trail_enabled", False))
    current_k = float(target.get("adaptive_trail_k", 2.5) or 2.5)
    print(f"\nCURRENT: adaptive_trail_enabled={current_enabled}  k={current_k}")
    print(f"REQUEST: adaptive_trail_enabled={new_enabled}"
          + (f"  k={new_k}" if new_k is not None else ""))

    if not apply:
        print("\n(dry-run — pass --apply to persist)")
        return

    target["adaptive_trail_enabled"] = new_enabled
    if new_k is not None:
        target["adaptive_trail_k"] = new_k
    cfg["sleeves"] = sleeves
    store.put_config(tenant, product_id, cfg)

    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        log.record(
            "adaptive_trail_config_changed",
            tenant=tenant, symbol=product_id, sleeve_id=sleeve_id,
            old_enabled=current_enabled, new_enabled=new_enabled,
            old_k=current_k, new_k=(new_k if new_k is not None else current_k),
            severity="info",
            reason="manual change via diag_adaptive_trail_toggle.py",
        )
    except Exception:
        pass

    print(f"\n✓ APPLIED to config. Bot picks up on next config-refresh tick.")
    if new_enabled:
        print(f"\n  From next tick, trail_distance for this sleeve will re-tune")
        print(f"  live: k×σ×mid_price, capped at 5% of mark, floored at 2 ticks.")
        print(f"  Look for 'trail_distance_adapted' events in the trade log")
        print(f"  when the live estimate diverges >20% from static.")
    print("=" * 100)


if __name__ == "__main__":
    main()
