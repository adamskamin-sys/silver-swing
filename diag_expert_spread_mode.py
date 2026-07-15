"""Read or set the tenant's __expert_spread_mode__ scope.

Adam 2026-07-15: expert_spread SHADOW → EXPERT rollout gate.

Modes:
  off     (default) — Avellaneda-Stoikov code path does not run
  shadow  — compute AS spread + log alongside legacy; NEVER apply
  expert  — compute AS spread + APPLY to buy/sell (future)

Read-only when called with no args. Pass a mode arg to set.
Write is done via a state_patch so it's picked up on the bot's
next tick without a restart.

Usage:
    python3 diag_expert_spread_mode.py                     # read current
    python3 diag_expert_spread_mode.py shadow --apply      # enable shadow
    python3 diag_expert_spread_mode.py expert --apply      # enable expert
    python3 diag_expert_spread_mode.py off --apply         # disable
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
        candidate = sys.argv[1].lower()
        if candidate in ("off", "shadow", "expert"):
            new_mode = candidate
            apply = "--apply" in sys.argv
        else:
            print(f"USAGE: python3 diag_expert_spread_mode.py [off|shadow|expert] [--apply]")
            return

    print("=" * 78)
    print(f"EXPERT SPREAD MODE — tenant={tenant}")
    print("=" * 78)

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))
    current = (store.get_state(tenant, "__expert_spread_mode__") or {}).get("mode") or "off"
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

    # Write via state_patch on the __expert_spread_mode__ scope so the
    # bot picks it up cleanly. Uses put_state directly since this scope
    # is a config-level flag, not per-sleeve state.
    store.put_state(tenant, "__expert_spread_mode__", {
        "mode": new_mode,
        "changed_at": int(time.time()),
        "changed_by": "diag_expert_spread_mode.py",
    })

    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        log.record(
            "expert_spread_mode_changed",
            tenant=tenant,
            old_mode=current, new_mode=new_mode,
            severity="info",
            reason="manual change via diag_expert_spread_mode.py",
        )
    except Exception as e:
        print(f"\n(note: trade log record failed: {e})")

    print(f"\n✓ APPLIED. __expert_spread_mode__ = {new_mode}")
    if new_mode == "shadow":
        print(f"\n  Bot will now compute Avellaneda-Stoikov spread on each")
        print(f"  post-sell reanchor and log 'expert_spread_shadow_decision'")
        print(f"  events showing AS vs legacy. No orders will change.")
        print(f"\n  After 24-48h, run:")
        print(f"    python3 diag_expert_spread_review.py")
        print(f"  to compare AS vs legacy across all sleeves before flipping")
        print(f"  to 'expert' mode per-sleeve.")
    elif new_mode == "expert":
        print(f"\n  ⚠ EXPERT MODE not yet fully wired for APPLY step.")
        print(f"  This flag is reserved for the future commit that adds")
        print(f"  the buy_px/sell_px write-back. For now behaves like shadow.")
    print("=" * 78)


if __name__ == "__main__":
    main()
