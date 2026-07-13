"""Clear the portfolio-risk halt for a tenant, reset the peak baseline.

Usage:  python3 diag_clear_halt.py [TENANT]
Default TENANT = adam-live.

Rewrites the __portfolio_risk__ scope: halted=false, halt_reason=None,
peak_pnl reset to current_pnl (so the next tick doesn't immediately re-halt
against a stale peak), peak_ts reset to now.

This is a one-shot unblocker. The durable fix is in the DEFAULT_CONFIG and
drawdown-baseline changes in portfolio_risk.py (raised noise floor + account
equity floor for the % denominator).
"""
from __future__ import annotations
import os
import sys
import time

from state_store import make_store
import portfolio_risk


def main() -> None:
    tenant = sys.argv[1] if len(sys.argv) > 1 else "adam-live"
    store = make_store(os.getenv("SWING_DATA_DIR", "data"))

    before = store.get_state(tenant, "__portfolio_risk__") or {}
    print(f"BEFORE ({tenant}):")
    print(f"  halted        = {before.get('halted')}")
    print(f"  halt_reason   = {before.get('halt_reason')}")
    print(f"  peak_pnl      = {before.get('peak_pnl')}")
    print(f"  current_pnl   = {before.get('current_pnl')}")
    print(f"  drawdown_pct  = {before.get('drawdown_pct')}")
    print(f"  drawdown_$    = {before.get('drawdown_dollars')}")

    current, _ = portfolio_risk._aggregate_swing_pnl(store, tenant)
    now = time.time()
    st = dict(before)
    st["halted"] = False
    st["last_halt_reason"] = before.get("halt_reason")
    st["halt_reason"] = None
    st["peak_pnl"] = current
    st["peak_ts"] = now
    st["resumed_ts"] = now
    st["current_pnl"] = current
    st["drawdown_pct"] = 0.0
    st["drawdown_dollars"] = 0.0
    store.put_state(tenant, "__portfolio_risk__", st)

    print(f"\nAFTER ({tenant}):")
    print(f"  halted        = False")
    print(f"  peak_pnl      = {current:.2f} (reset to current)")
    print(f"  drawdown_pct  = 0.0")
    print("  → sleeves can now arm rebuys; the durable config change will "
          "prevent re-halting on noise.")


if __name__ == "__main__":
    main()
