"""Clear stale per-symbol snapshots that cause dashboard phantoms.

Adam 2026-07-18: HYP dashboard modal showed "Position: 1 LONG @ $59.95"
when actual Coinbase position was 0 and sleeve own_avg_entry was None.
Root cause: per-symbol snapshot at store.get_snapshot(tenant, HYP) was
4 days stale — from a prior cycle when HYP was held at $59.95.

Because broker.portfolio_snapshot() SKIPS derivatives with 0 contracts
(broker.py:833), HYP wasn't in the __portfolio__ list. Dashboard fell
back to per-symbol snapshot, which had stale position_qty=1 → phantom.

This diag zeroes out the stale snapshot's position_qty / position_avg /
unrealized so the dashboard shows "no position" instead of the phantom.
Only touches snapshots older than --stale-mins (default 300 = 5 hours).

Read-only by default. Pass --apply to actually clear.

Usage:
    python3 diag_clear_stale_snapshot.py                    # scan all, dry-run
    python3 diag_clear_stale_snapshot.py --apply            # clear all stale
    python3 diag_clear_stale_snapshot.py HYP-20DEC30-CDE --apply
    python3 diag_clear_stale_snapshot.py --stale-mins 60 --apply
"""
from __future__ import annotations
import os
import sys
import time


TENANT = "adam-live"


def main() -> None:
    apply = "--apply" in sys.argv
    stale_mins = 300.0
    if "--stale-mins" in sys.argv:
        try:
            stale_mins = float(sys.argv[sys.argv.index("--stale-mins") + 1])
        except (IndexError, ValueError):
            print("✗ --stale-mins needs a numeric value")
            return
    target_product = None
    for a in sys.argv[1:]:
        if a.startswith("--"):
            continue
        if a == "--apply":
            continue
        try:
            float(a)
            continue
        except ValueError:
            target_product = a
            break

    print("=" * 90)
    print(f"CLEAR STALE SNAPSHOTS {'(APPLY)' if apply else '(dry-run)'} — "
          f"stale threshold {stale_mins:.0f}min"
          + (f" · target={target_product}" if target_product else " · all products"))
    print("=" * 90)

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))

    products = []
    if target_product:
        products = [target_product]
    else:
        try:
            for sym in store.list_symbols(TENANT):
                if not sym.startswith("__"):
                    products.append(sym)
        except Exception as e:
            print(f"  ✗ list_symbols failed: {type(e).__name__}: {e}")
            return

    now = time.time()
    cutoff = now - stale_mins * 60.0
    to_clear: list[tuple[str, dict, float]] = []

    for pid in sorted(products):
        snap = store.get_snapshot(TENANT, pid) if hasattr(store, "get_snapshot") else None
        if not isinstance(snap, dict):
            continue
        try:
            gen_at = float(snap.get("generated_at") or 0)
        except (TypeError, ValueError):
            gen_at = 0.0
        age_s = now - gen_at if gen_at else float("inf")
        pos_qty = 0
        pos_avg = 0.0
        try:
            pos_qty = int(float(snap.get("position_qty") or 0))
        except (TypeError, ValueError):
            pass
        try:
            pos_avg = float(snap.get("position_avg") or 0)
        except (TypeError, ValueError):
            pass
        if pos_qty == 0 and pos_avg == 0:
            continue  # already clean
        if gen_at > cutoff:
            continue  # not stale yet
        to_clear.append((pid, snap, age_s))
        print(f"  · {pid:<28}  pos_qty={pos_qty}  pos_avg=${pos_avg:.4f}  "
              f"age={age_s/3600:.1f}h")

    if not to_clear:
        print("\n  (no stale snapshots found — nothing to clear)")
        print("=" * 90)
        return

    print(f"\n  {len(to_clear)} snapshot(s) would be cleared "
          f"(position fields zeroed, mark/generated_at preserved for audit)")

    if not apply:
        print(f"\n(dry-run — pass --apply to zero the position fields)")
        return

    for pid, snap, age_s in to_clear:
        cleared = dict(snap)
        cleared["position_qty"] = 0
        cleared["position_avg"] = 0.0
        cleared["unrealized_pnl"] = 0.0
        cleared["_cleared_by_diag_ts"] = now
        cleared["_cleared_prev_position_qty"] = snap.get("position_qty")
        cleared["_cleared_prev_position_avg"] = snap.get("position_avg")
        store.put_snapshot(TENANT, pid, cleared)
        print(f"  ✓ {pid}: position fields zeroed (was {snap.get('position_qty')} "
              f"@ ${float(snap.get('position_avg') or 0):.4f})")

    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        for pid, snap, age_s in to_clear:
            log.record(
                "stale_snapshot_cleared_via_diag",
                tenant=TENANT, symbol=pid,
                snapshot_age_hours=round(age_s / 3600, 2),
                prev_position_qty=snap.get("position_qty"),
                prev_position_avg=snap.get("position_avg"),
                severity="info",
                reason="manual clear via diag_clear_stale_snapshot.py — "
                       "dashboard phantom position display",
            )
    except Exception:
        pass

    print(f"\n✓ Cleared {len(to_clear)} stale snapshot(s). Dashboard should show")
    print(f"  'no position' for these products on next refresh (5s TTL).")
    print("=" * 90)


if __name__ == "__main__":
    main()
