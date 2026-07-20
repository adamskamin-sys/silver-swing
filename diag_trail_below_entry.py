"""Reveal whether the 'trail below entry' issue is display or backend.

Adam 2026-07-19: dashboard shows TRAIL EXIT below own_avg for XLP,
ZEC, and possibly others. Two possibilities:

  A) Display bug — dashboard computes trailEff = peak - trail_distance
     with no floor (app.js:6548), but backend places the actual resting
     stop at the proper floor. Fix is dashboard-only.

  B) Backend bug — code at swing_leg.py:4664 only applies break_even
     floor when trail_active (hwm > break_even). On fresh buy hwm ≈
     own_avg, so trail_active=False and we fall through to stop_loss_px
     or 5% fallback. If stop_loss_enabled=False and no stop_loss_px,
     could end up with an unfloored resting stop below own_avg.

This diag dumps, for every held sleeve on adam-live:
  - own_avg_entry
  - resting_stop_px (what's ACTUALLY on Coinbase)
  - resting_stop_stage
  - trail_high_water_price
  - trail_distance (from sleeve config)
  - stop_loss_enabled / stop_loss_px
  - sell_px
  - computed dashboard trailEff = hwm - trail_distance
  - computed break_even_floor = own_avg + fees

Then flags: is actual < own_avg? Is dashboard-shown < own_avg?

Read-only. Usage: python3 diag_trail_below_entry.py
"""
from __future__ import annotations
import os


def _q(v, d=0.0):
    try:
        return float(v) if v is not None else d
    except (TypeError, ValueError):
        return d


def main() -> None:
    print("=" * 78)
    print("TRAIL-BELOW-ENTRY DIAGNOSTIC — is it display or backend?")
    print("=" * 78)

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))
    raw = store._load()

    tenant = "adam-live"
    tdata = raw.get(tenant) or {}

    from broker import BrokerConfig, CoinbaseBroker
    findings = []

    for symbol, entry in tdata.items():
        if symbol == "__portfolio__" or not isinstance(entry, dict):
            continue
        state = entry.get("state") or {}
        config = entry.get("config") or {}
        sleeves = state.get("sleeves") or {}
        sleeves_cfg_list = config.get("sleeves") or []
        cfg_by_id = {}
        for scfg in sleeves_cfg_list:
            if isinstance(scfg, dict) and scfg.get("id"):
                cfg_by_id[str(scfg["id"])] = scfg

        # Only interested in sleeves with a position (own_avg set)
        held = [(sid, ss) for sid, ss in sleeves.items()
                if isinstance(ss, dict) and _q(ss.get("own_avg_entry")) > 0]
        if not held:
            continue

        # Get contract_size from broker (source of truth per constitution §3.14)
        contract_size = 0.0
        try:
            b = CoinbaseBroker(BrokerConfig(product_id=symbol))
            spec = b.contract_spec()
            contract_size = float((spec or {}).get("contract_size") or 0)
        except Exception as e:
            print(f"\n{symbol}: contract_spec failed: {e}")

        fee_rt = _q(config.get("fee_per_contract_roundtrip"))

        print(f"\n{'=' * 78}")
        print(f"{symbol}  (contract_size={contract_size})")
        print(f"{'=' * 78}")

        for sid, ss in held:
            scfg = cfg_by_id.get(str(sid), {})
            own_avg = _q(ss.get("own_avg_entry"))
            resting_px = _q(ss.get("resting_stop_px"))
            resting_stage = ss.get("resting_stop_stage") or "—"
            resting_oid = ss.get("resting_stop_oid") or "—"
            hwm = _q(ss.get("trail_high_water_price"))
            qty = int(_q(ss.get("qty"), 1) or 1)
            trail_dist = _q(scfg.get("trail_distance"))
            sell_px = _q(scfg.get("sell_px"))
            sl_enabled = scfg.get("stop_loss_enabled", True)
            sl_px = _q(scfg.get("stop_loss_px"))

            # Backend's break_even_floor formula (from swing_leg.py:4608)
            fee_price = (fee_rt / contract_size / max(1, qty)) if contract_size > 0 else 0.0
            tick = _q(scfg.get("tick_size")) or 0.01
            break_even_floor = own_avg + fee_price + max(tick, own_avg * 0.0005) if own_avg > 0 else 0

            # Dashboard's raw trailEff (from app.js:6548 — NO floor)
            dashboard_trail = hwm - trail_dist if (hwm > 0 and trail_dist > 0) else 0

            print(f"\n  sleeve {sid} (name: {scfg.get('name', ss.get('name'))}):")
            print(f"    own_avg_entry:            ${own_avg:.6f}")
            print(f"    qty:                      {qty}")
            print(f"    trail_high_water_price:   ${hwm:.6f}")
            print(f"    trail_distance (config):  ${trail_dist:.6f}")
            print(f"    sell_px (config):         ${sell_px:.6f}  ({'set' if sell_px > 0 else '— unset'})")
            print(f"    stop_loss_enabled:        {sl_enabled}")
            print(f"    stop_loss_px (config):    ${sl_px:.6f}")
            print(f"    fee_per_contract_rt:      ${fee_rt}")
            print(f"    → fee_price/unit:         ${fee_price:.8f}")
            print(f"    → break_even_floor:       ${break_even_floor:.6f}  (own_avg + fees)")
            print(f"")
            print(f"    ACTUAL Coinbase resting stop (from Redis):")
            print(f"      resting_stop_oid:       {resting_oid}")
            print(f"      resting_stop_px:        ${resting_px:.6f}")
            print(f"      resting_stop_stage:     {resting_stage}")
            print(f"")
            print(f"    DASHBOARD shows (hwm − trail_dist, no floor):")
            print(f"      trailEff:               ${dashboard_trail:.6f}")

            # The verdict
            actual_below = resting_px > 0 and own_avg > 0 and resting_px < own_avg
            display_below = dashboard_trail > 0 and own_avg > 0 and dashboard_trail < own_avg

            print(f"")
            if actual_below and display_below:
                print(f"    ⚠ CASE B: BACKEND places stop BELOW own_avg "
                      f"(${resting_px:.6f} < ${own_avg:.6f}). §3.4 violation.")
                findings.append(("backend", symbol, sid, own_avg, resting_px, resting_stage))
            elif not actual_below and display_below:
                print(f"    ⚠ CASE A: DISPLAY BUG — actual ${resting_px:.6f} is safe "
                      f"(≥ own_avg or 0), dashboard misleadingly shows ${dashboard_trail:.6f}.")
                findings.append(("display", symbol, sid, own_avg, resting_px, resting_stage))
            elif actual_below and not display_below:
                print(f"    ⚠ CASE C: Weird — backend below entry but display isn't. "
                      f"Investigate.")
                findings.append(("weird", symbol, sid, own_avg, resting_px, resting_stage))
            else:
                print(f"    ✓ Both actual and display are at/above own_avg.")

    print(f"\n{'=' * 78}")
    print("VERDICT")
    print(f"{'=' * 78}")
    if not findings:
        print("✓ No trail-below-entry issue found on any held sleeve.")
        return
    backend_cases = [f for f in findings if f[0] == "backend"]
    display_cases = [f for f in findings if f[0] == "display"]
    if backend_cases and not display_cases:
        print(f"→ BACKEND bug on {len(backend_cases)} sleeves. Fix swing_leg.py "
              f"to apply own_avg+fees floor even when trail_active=False.")
    elif display_cases and not backend_cases:
        print(f"→ DISPLAY bug on {len(display_cases)} sleeves. Fix app.js:6548 "
              f"to apply own_avg+fees floor to trailEff.")
    else:
        print(f"→ MIXED: {len(backend_cases)} backend + {len(display_cases)} display. "
              f"Both fixes needed.")


if __name__ == "__main__":
    main()
