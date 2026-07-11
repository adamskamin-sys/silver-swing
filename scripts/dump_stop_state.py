"""Dump raw stop-loss config + state for one or more live products so we can
compute the effective stop the same way the bot does.

Usage:
    python3 scripts/dump_stop_state.py                            # all live
    python3 scripts/dump_stop_state.py NGS-28JUL26-CDE            # one product
    python3 scripts/dump_stop_state.py CU NGS SLR                 # partial-match
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from state_store import make_store  # noqa: E402


def main() -> int:
    store = make_store("./data")
    tenant = "adam-live"
    filter_needles = sys.argv[1:]
    symbols = [s for s in store.list_symbols(tenant) if not s.startswith("__")]
    if filter_needles:
        keep = []
        for sym in symbols:
            for needle in filter_needles:
                if needle in sym:
                    keep.append(sym)
                    break
        symbols = keep
    for sym in symbols:
        cfg = store.get_config(tenant, sym) or {}
        state = store.get_state(tenant, sym) or {}
        sleeves = cfg.get("sleeves") or []
        if not sleeves:
            continue
        for sleeve in sleeves:
            sid = sleeve.get("id")
            ss = (state.get("sleeves") or {}).get(sid, {})
            print(f"{sym}  sleeve={sid}")
            print(f"  stop_loss_enabled:            {sleeve.get('stop_loss_enabled')}")
            print(f"  stop_loss_px (base):          {sleeve.get('stop_loss_px')}")
            print(f"  stop_loss_ratchet_enabled:    {sleeve.get('stop_loss_ratchet_enabled')}")
            print(f"  stop_loss_ratchet_distance:   {sleeve.get('stop_loss_ratchet_distance')}")
            print(f"  stop_loss_ratchet_activation: {sleeve.get('stop_loss_ratchet_activation')}")
            print(f"  protect_realized_enabled:     {sleeve.get('stop_loss_protect_realized_enabled')}")
            print(f"  protect_realized_frac:        {sleeve.get('stop_loss_protect_realized_frac')}")
            print(f"  --- state ---")
            print(f"  stop_loss_hwm:                {ss.get('stop_loss_hwm')}")
            print(f"  own_avg_entry:                {ss.get('own_avg_entry')}")
            print(f"  realized_pnl:                 {ss.get('realized_pnl')}")
            print(f"  cycles:                       {ss.get('cycles')}")
            # Compute what the dashboard SHOULD show under the new activation-gated logic.
            base = float(sleeve.get("stop_loss_px") or 0)
            hwm = float(ss.get("stop_loss_hwm") or 0)
            rdist = float(sleeve.get("stop_loss_ratchet_distance") or 0)
            ract = float(sleeve.get("stop_loss_ratchet_activation") or 0)
            avg = float(ss.get("own_avg_entry") or 0)
            unrealized = (hwm - avg) if avg > 0 else 0
            armed = avg > 0 and unrealized >= ract
            ratcheted_floor = (hwm - rdist) if (sleeve.get("stop_loss_ratchet_enabled") and hwm > 0 and rdist > 0 and armed) else 0
            effective = max(base, ratcheted_floor)
            show_arrow = ratcheted_floor > base
            print(f"  --- computed effective stop ---")
            print(f"  ratchet armed? {armed} (unrealized {unrealized:.4f} vs activation {ract})")
            print(f"  ratcheted_floor:              {ratcheted_floor}")
            print(f"  effective stop:               {effective}")
            print(f"  should show ↑ badge?          {show_arrow}")
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
