"""Force-seed missing configs + prime for spawn.

Adam 2026-07-15: after multiple auto-recovery fix attempts, the 9
dead products still won't spawn. This diag directly seeds any missing
top-level config from Coinbase specs for every product that has
armed sleeves but no config — the specific bug from 96ae887, applied
manually to bypass whatever's blocking the in-process auto-seed.

After this runs + you wait ~30s, live_runner's auto-recovery
should be able to spawn Tracks because the config gate no longer
refuses them. If Tracks STILL don't spawn, the issue is deeper
(feed init, auth) and we'll see track_spawn_failed events.

Read-only by default. Pass --apply to actually write.

Usage:
    python3 diag_force_track_revive.py                # dry-run
    python3 diag_force_track_revive.py --apply        # write configs
"""
from __future__ import annotations
import os
import sys
import time


def main() -> None:
    apply = "--apply" in sys.argv
    tenant = "adam-live"
    print("=" * 100)
    print(f"FORCE TRACK REVIVE {'(APPLY)' if apply else '(dry-run)'} — {tenant}")
    print("=" * 100)

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))
    from broker import BrokerConfig, CoinbaseBroker

    products = [s for s in store.list_symbols(tenant) if not s.startswith("__")]
    revive_list = []
    for sym in sorted(products):
        cfg = store.get_config(tenant, sym) or {}
        state = store.get_state(tenant, sym) or {}
        sleeves = state.get("sleeves") or {}
        armed = any(str(ss.get("state") or "") in ("ARMED_BUY", "ARMED_SELL")
                    for ss in sleeves.values())
        if armed and not cfg:
            revive_list.append(sym)

    print(f"\nProducts with armed sleeves but NO top-level config: {len(revive_list)}")
    for sym in revive_list:
        print(f"  · {sym}")

    if not revive_list:
        print(f"\n✓ Every armed product already has a config. Nothing to do here.")
        print(f"  If Tracks still won't spawn, the issue is elsewhere (feed init,")
        print(f"  auth, etc). Check for track_spawn_failed events in trade log.")
        return

    if not apply:
        print(f"\n(dry-run — pass --apply to seed configs)")
        return

    print(f"\n[APPLY] Seeding minimal configs from Coinbase specs...")
    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    except Exception:
        log = None

    seeded = 0
    failed = 0
    for sym in revive_list:
        try:
            b = CoinbaseBroker(BrokerConfig(product_id=sym))
            spec = b.contract_spec() or {}
            seeded_cfg = {
                "product_id": sym,
                "tick_size": float(spec.get("tick_size") or 0.01),
                "contract_size": float(spec.get("contract_size") or 1),
                "fee_per_contract_roundtrip": 0.5,
                "swing_qty": 0,
                "core_qty": 0,
                "abort_above": 0,
                "abort_below": 0,
                "sleeves": [],
                "_auto_seeded": True,
                "_auto_seeded_ts": int(time.time()),
                "_auto_seeded_by": "diag_force_track_revive.py",
            }
            store.put_config(tenant, sym, seeded_cfg)
            if log:
                try:
                    log.record("non_primary_config_manual_seeded",
                               tenant=tenant, symbol=sym, spec=spec,
                               severity="warn",
                               reason="manually seeded via diag_force_track_revive.py")
                except Exception:
                    pass
            print(f"  ✓ {sym}: seeded tick={seeded_cfg['tick_size']} "
                  f"contract={seeded_cfg['contract_size']}")
            seeded += 1
        except Exception as e:
            print(f"  ✗ {sym}: {type(e).__name__}: {e}")
            failed += 1

    print(f"\n[RESULT] seeded {seeded}, failed {failed}")
    if seeded > 0:
        print(f"\n  Wait ~30-60s for live_runner's auto-recovery to notice the")
        print(f"  configs + attempt spawn. Then check:")
        print(f"    python3 diag_track_health_fleet.py 3")
        print(f"    python3 diag_track_recovery_check.py 3")
    print("=" * 100)


if __name__ == "__main__":
    main()
