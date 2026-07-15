"""Read or set the tenant's __regime_router_mode__ scope.

Adam 2026-07-15: regime router (module 3342580, wire ba16aca) rollout
gate. Same off / shadow / expert pattern as __expert_spread_mode__.

Modes:
  off     (default) — regime router does not run
  shadow  — compute regime + adjustments, log only, no state change
  expert  — apply qty_multiplier + should_arm gate to sleeve arms

Usage:
    python3 diag_regime_router_mode.py                       # read
    python3 diag_regime_router_mode.py shadow --apply
    python3 diag_regime_router_mode.py expert --apply
    python3 diag_regime_router_mode.py off --apply
"""
from __future__ import annotations
import os
import sys
import time


def main() -> None:
    tenant = "adam-live"
    new_mode = None
    apply = False
    if len(sys.argv) > 1:
        cand = sys.argv[1].lower()
        if cand in ("off", "shadow", "expert"):
            new_mode = cand
            apply = "--apply" in sys.argv
        else:
            print(f"USAGE: python3 diag_regime_router_mode.py [off|shadow|expert] [--apply]")
            return

    print("=" * 78)
    print(f"REGIME ROUTER MODE — tenant={tenant}")
    print("=" * 78)

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))
    current = (store.get_state(tenant, "__regime_router_mode__") or {}).get("mode") or "off"
    print(f"\nCurrent mode: {current}")

    if new_mode is None:
        print("\n(read-only — pass a mode + --apply to change)")
        return
    if new_mode == current:
        print(f"\nAlready in {new_mode} mode. Nothing to change.")
        return

    print(f"\nRequested change: {current} → {new_mode}")
    if not apply:
        print("(dry-run — pass --apply to persist)")
        return

    store.put_state(tenant, "__regime_router_mode__", {
        "mode": new_mode,
        "changed_at": int(time.time()),
        "changed_by": "diag_regime_router_mode.py",
    })

    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        log.record(
            "regime_router_mode_changed",
            tenant=tenant, old_mode=current, new_mode=new_mode,
            severity="info", reason="manual change via diag",
        )
    except Exception as e:
        print(f"(note: trade log record failed: {e})")

    print(f"\n✓ APPLIED. __regime_router_mode__ = {new_mode}")
    if new_mode == "shadow":
        print(f"\n  Bot will now classify regime per sleeve on every ARMED_BUY")
        print(f"  arm attempt (needs ≥40 samples). Log 'regime_router_shadow'")
        print(f"  events with regime + gamma_mult + qty_mult + should_arm.")
        print(f"  No behavior change yet — observe for 24-48h.")
    elif new_mode == "expert":
        print(f"\n  ⚡ EXPERT MODE — arms will be:")
        print(f"    * SKIPPED in chop regime (regime_router_arm_gated)")
        print(f"    * qty-DOWNSCALED per regime + vol_state")
        print(f"    * gamma-adjusted for future AS integration")
        print(f"\n  Flip back if problematic:")
        print(f"    python3 diag_regime_router_mode.py shadow --apply")
    print("=" * 78)


if __name__ == "__main__":
    main()
