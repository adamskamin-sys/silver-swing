"""Identify + clear the ghost SLR sleeve.

Adam 2026-07-19 (from screenshot):
  Position: 1 LONG · avg $57.2975
  Scanner 22:46: CONTR 1, POS_AVG $57.297, STOP LOSS 🚨 NOT PLACED
  Scanner 23:45: CONTR 1, POS_AVG $57.297, STOP LOSS $55.025

Two sleeves each claiming 1 contract → bot believes it holds 2 total.
Coinbase says 1 LONG. One sleeve is a ghost.

The one with STOP LOSS "NOT PLACED" is the safety net correctly refusing
to place a stop because when it computes its slice of the position, the
math comes out zero or negative. That's the ghost.

Root cause: multi-sleeve credit race on a buy fill — `_sleeve_on_fill`
fired against BOTH sleeves instead of only the one whose live_order_id
matched. Both `own_avg_entry` got written but only one contract exists.

This diag:
  1. Fetches actual Coinbase position for SLR (source of truth §3.14)
  2. Sums qty across all sleeves with own_avg_entry set
  3. If sum > actual position → ghost detected
  4. Identifies the ghost = the sleeve with no resting_stop_oid AND
     no live_order_id (the one auto-heal correctly gave up on)
  5. Dry-run shows proposed clear (own_avg → None, resting fields → None,
     KEEPS cycles + realized_pnl for accounting integrity)
  6. --apply persists

Read-only by default. Usage:

    python3 diag_slr_ghost_detector.py                # dry-run
    python3 diag_slr_ghost_detector.py --apply        # persist
"""
from __future__ import annotations
import os
import sys


TENANT = "adam-live"
PRODUCT_ID = "SLR-27AUG26-CDE"


def _q(v, d=0.0):
    try: return float(v) if v is not None else d
    except (TypeError, ValueError): return d


def main() -> None:
    apply = "--apply" in sys.argv

    print("=" * 78)
    print(f"SLR GHOST-SLEEVE DETECTOR {'(APPLY)' if apply else '(dry-run)'} — "
          f"{TENANT}/{PRODUCT_ID}")
    print("=" * 78)

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))
    state = store.get_state(TENANT, PRODUCT_ID) or {}
    sleeves = state.get("sleeves") or {}

    if not sleeves:
        print(f"\n✗ No sleeves for {TENANT}/{PRODUCT_ID}")
        return

    # 1. Coinbase truth
    from broker import BrokerConfig, CoinbaseBroker
    b = CoinbaseBroker(BrokerConfig(product_id=PRODUCT_ID))
    try:
        actual_qty = int(b.position_qty())
    except Exception as e:
        print(f"\n✗ position_qty() failed: {e}")
        return
    print(f"\nCoinbase position for {PRODUCT_ID}: {actual_qty} contracts (LONG)")

    # 2. Sum sleeve-claimed qty
    holding_sleeves = []
    for sid, ss in sleeves.items():
        own_avg = _q(ss.get("own_avg_entry"))
        if own_avg > 0:
            qty = int(_q(ss.get("qty"), 1) or 1)
            holding_sleeves.append({
                "sid": sid,
                "name": ss.get("name"),
                "own_avg": own_avg,
                "qty": qty,
                "resting_stop_oid": ss.get("resting_stop_oid"),
                "resting_stop_px": _q(ss.get("resting_stop_px")),
                "resting_stop_stage": ss.get("resting_stop_stage"),
                "live_order_id": ss.get("live_order_id"),
                "cycles": int(_q(ss.get("cycles"), 0) or 0),
                "realized_pnl": _q(ss.get("realized_pnl")),
            })
    total_claimed = sum(s["qty"] for s in holding_sleeves)

    print(f"\nSleeves with own_avg_entry SET ({len(holding_sleeves)}):")
    for s in holding_sleeves:
        print(f"  sleeve {s['sid']} ({s['name']}):")
        print(f"    own_avg:            ${s['own_avg']:.6f}")
        print(f"    qty:                {s['qty']}")
        print(f"    resting_stop_oid:   {s['resting_stop_oid'] or '—'}")
        print(f"    resting_stop_px:    ${s['resting_stop_px']:.6f}")
        print(f"    resting_stop_stage: {s['resting_stop_stage'] or '—'}")
        print(f"    live_order_id:      {s['live_order_id'] or '—'}")
        print(f"    cycles:             {s['cycles']}")
        print(f"    realized_pnl:       ${s['realized_pnl']:.2f}")
    print(f"\nSum of sleeve-claimed qty: {total_claimed}")
    print(f"Coinbase actual:           {actual_qty}")

    if total_claimed <= actual_qty:
        print(f"\n✓ No ghost — sleeve accounting matches or under-counts Coinbase.")
        return

    delta = total_claimed - actual_qty
    print(f"\n⚠ MISMATCH: sleeves claim {delta} more contract(s) than Coinbase.")
    print(f"  {delta} sleeve(s) are ghosts (own_avg set but no real position).")

    # 3. Identify ghost = no resting_stop_oid AND no live_order_id
    # (auto-heal correctly gave up on this sleeve because its slice
    # came out zero/negative when computing the exchange stop)
    ghosts = [s for s in holding_sleeves
              if not s["resting_stop_oid"] and not s["live_order_id"]]
    real = [s for s in holding_sleeves if s not in ghosts]

    print(f"\nGhost candidates ({len(ghosts)}) — no resting stop AND no live order:")
    for g in ghosts:
        print(f"  ✗ {g['sid']} ({g['name']}) — auto-heal correctly refused stop placement")
    print(f"\nReal holders ({len(real)}):")
    for r in real:
        print(f"  ✓ {r['sid']} ({r['name']}) — has resting_stop_oid={r['resting_stop_oid']}")

    if len(ghosts) != delta:
        print(f"\n⚠ Ghost count ({len(ghosts)}) != mismatch ({delta}). "
              f"Manual review — script won't auto-clear.")
        return

    print(f"\nPROPOSED CORRECTION (accounting-safe — no order actions):")
    for g in ghosts:
        print(f"  sleeve {g['sid']}:")
        print(f"    own_avg_entry:     ${g['own_avg']:.6f} → None")
        print(f"    resting_stop_oid:  {g['resting_stop_oid']} → None")
        print(f"    resting_stop_px:   ${g['resting_stop_px']:.6f} → None")
        print(f"    resting_stop_stage:{g['resting_stop_stage']} → None")
        print(f"    live_order_id:     {g['live_order_id']} → None")
        print(f"  KEPT (accounting integrity):")
        print(f"    cycles:            {g['cycles']}")
        print(f"    realized_pnl:      ${g['realized_pnl']:.2f}")
        print(f"    name / config:     unchanged")

    print(f"\nAfter apply:")
    print(f"  - Sum of sleeve qty matches Coinbase ({actual_qty})")
    print(f"  - Ghost sleeve advances to ARMED_BUY on next tick + arms for its next entry")
    print(f"  - Real sleeve keeps its live_order_id + resting stop untouched")

    if not apply:
        print(f"\n(dry-run — pass --apply to persist)")
        return

    # 4. Apply — accounting-only clear
    for g in ghosts:
        ss = sleeves[g["sid"]]
        ss["own_avg_entry"] = None
        ss["resting_stop_oid"] = None
        ss["resting_stop_px"] = None
        ss["resting_stop_stage"] = None
        ss["live_order_id"] = None
        # HWM must reset too — leftover HWM would produce wrong trail math
        ss["trail_high_water_price"] = 0.0
        ss["trail_armed"] = False
        # State back to ARMED_BUY so it can enter a new cycle cleanly
        ss["state"] = "ARMED_BUY"
        sleeves[g["sid"]] = ss

    state["sleeves"] = sleeves
    store.put_state(TENANT, PRODUCT_ID, state)

    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        for g in ghosts:
            log.record(
                "slr_ghost_sleeve_cleared",
                tenant=TENANT, symbol=PRODUCT_ID, sleeve_id=g["sid"],
                sleeve_name=g["name"],
                own_avg_before=g["own_avg"],
                resting_stop_oid_before=g["resting_stop_oid"],
                live_order_id_before=g["live_order_id"],
                cycles_kept=g["cycles"],
                realized_pnl_kept=g["realized_pnl"],
                actual_position=actual_qty,
                sleeves_claimed=total_claimed,
                reason=("multi-sleeve credit race: this sleeve had own_avg "
                        "set but no live stop nor live order — auto-heal "
                        "correctly refused stop placement because its "
                        "position-slice math came out zero. Cleared to "
                        "ARMED_BUY so it can re-enter cleanly. Cycles + "
                        "realized_pnl preserved."),
                severity="warn",
            )
    except Exception as e:
        print(f"\n(note: trade log record failed: {e})")

    print(f"\n✓ APPLIED. Ghost sleeve(s) cleared. Bot picks up on next tick.")
    print(f"  Verify: sleeve editor should show sum-of-CONTR matches "
          f"Position ({actual_qty} LONG).")


if __name__ == "__main__":
    main()
