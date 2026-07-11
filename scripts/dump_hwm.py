"""Dump stop_loss_hwm for every live sleeve. Diagnostic — use to verify
whether the migration's HWM clear actually stuck to Redis.

Usage:
    python3 scripts/dump_hwm.py                     # dumps all live symbols
    python3 scripts/dump_hwm.py NGS-28JUL26-CDE     # single product
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
    if len(sys.argv) > 1:
        symbols = [sys.argv[1]]
    else:
        symbols = [
            s for s in store.list_symbols(tenant)
            if not s.startswith("__")
        ]
    print(f"tenant: {tenant}")
    print(f"backend: {'Redis' if os.getenv('REDIS_URL') else 'local JSON'}")
    print("-" * 60)
    for sym in symbols:
        state = store.get_state(tenant, sym) or {}
        sleeves = state.get("sleeves") or {}
        if not sleeves:
            print(f"{sym}: no sleeves in state")
            continue
        for sid, ss in sleeves.items():
            hwm = ss.get("stop_loss_hwm")
            flag = "  <-- STALE" if hwm is not None else ""
            print(f"{sym} / {sid}: stop_loss_hwm = {hwm!r}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
