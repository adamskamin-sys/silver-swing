"""Verify a symbol is truly safe to purge — cross-checks state, config,
__portfolio__ snapshot, AND live Coinbase position via broker.

Adam 2026-07-16: after diag_clear_retired_symbol --all-ghost flagged
PT-28SEP26-CDE for deletion despite Adam having held 10 PT contracts
earlier today, we need a stronger verifier. This diag calls the
broker directly (position_qty) to confirm Coinbase's ground truth
before any purge.

Read-only. No writes.

Usage:
    python3 diag_verify_before_purge.py PT-28SEP26-CDE
    python3 diag_verify_before_purge.py PT-28SEP26-CDE NOL-20JUL26-CDE
    python3 diag_verify_before_purge.py --all-ghost
"""
from __future__ import annotations
import argparse
import os
import sys
import time


TENANT = "adam-live"


def _find_ghost_symbols(store) -> list[str]:
    ghosts = []
    try:
        pf = store.get_state(TENANT, "__portfolio__") or {}
        for sym in store.list_symbols(TENANT):
            if sym.startswith("__"):
                continue
            cfg = store.get_config(TENANT, sym) or {}
            st = store.get_state(TENANT, sym) or {}
            sleeves = st.get("sleeves") or {}
            snap = pf.get(sym) or {}
            pq = float(snap.get("position_qty") or 0) if isinstance(snap, dict) else 0
            if (not sleeves and int(cfg.get("swing_qty") or 0) == 0
                    and int(cfg.get("core_qty") or 0) == 0 and pq == 0):
                ghosts.append(sym)
    except Exception:
        pass
    return ghosts


def _live_position_qty(sym: str) -> tuple[int, str]:
    """Ask Coinbase directly. Returns (qty, err_or_ok)."""
    try:
        from broker import CoinbaseBroker, BrokerConfig
        b = CoinbaseBroker(BrokerConfig(product_id=sym))
        qty = int(b.position_qty() or 0)
        return qty, "ok"
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--all-ghost", action="store_true")
    ap.add_argument("--data-dir", default=os.getenv("SWING_DATA_DIR", "data"))
    args = ap.parse_args()

    from state_store import make_store
    store = make_store(args.data_dir)

    if args.all_ghost:
        args.symbols = _find_ghost_symbols(store)
        if not args.symbols:
            print("No ghost symbols found.")
            return
        print(f"[--all-ghost] Discovered {len(args.symbols)} ghost symbols; verifying each...")

    if not args.symbols:
        print("USAGE: python3 diag_verify_before_purge.py SYMBOL [SYMBOL ...]")
        print("   or: python3 diag_verify_before_purge.py --all-ghost")
        sys.exit(2)

    print("=" * 90)
    print(f"VERIFY BEFORE PURGE — tenant={TENANT}")
    print("=" * 90)

    pf = store.get_state(TENANT, "__portfolio__") or {}
    pf_ts = pf.get("__ts__") or pf.get("ts")
    if pf_ts:
        try:
            age = time.time() - float(pf_ts)
            print(f"__portfolio__ snapshot age: {int(age)}s")
        except (TypeError, ValueError):
            pass

    safe = []
    unsafe = []
    for sym in args.symbols:
        st = store.get_state(TENANT, sym) or {}
        cfg = store.get_config(TENANT, sym) or {}
        # Portfolio-snapshot position for this sym
        snap = pf.get(sym) or {}
        pf_qty = 0.0
        pf_ts_sym = None
        if isinstance(snap, dict):
            try:
                pf_qty = float(snap.get("position_qty") or 0)
            except (TypeError, ValueError):
                pass
            pf_ts_sym = snap.get("mark_ts") or snap.get("ts")
        # Live Coinbase position
        live_qty, coinbase_err = _live_position_qty(sym)

        print(f"\n· {sym}")
        print(f"    state.swing_qty     = {st.get('swing_qty')}")
        print(f"    state.sleeves       = {len(st.get('sleeves') or {})}")
        print(f"    config.swing_qty    = {cfg.get('swing_qty')}")
        print(f"    config.core_qty     = {cfg.get('core_qty')}")
        print(f"    portfolio_qty       = {pf_qty}"
              + (f" (mark_ts={pf_ts_sym})" if pf_ts_sym else " (no timestamp)"))
        print(f"    coinbase_live_qty   = {live_qty}  [{coinbase_err}]")

        # Verdict
        if coinbase_err != "ok":
            print(f"    ⚠ CANNOT VERIFY — Coinbase call failed; DO NOT PURGE")
            unsafe.append(sym)
        elif live_qty != 0:
            print(f"    🔴 DO NOT PURGE — Coinbase holds {live_qty} contract(s)!")
            unsafe.append(sym)
        elif int(pf_qty) != 0:
            print(f"    ⚠ __portfolio__ says {pf_qty} but Coinbase says 0 — snapshot stale, but truly no position")
            print(f"    → safe to purge (Coinbase is ground truth)")
            safe.append(sym)
        else:
            print(f"    ✓ safe to purge (0 on both portfolio and Coinbase)")
            safe.append(sym)

    print()
    print("=" * 90)
    print(f"SUMMARY: ✓ {len(safe)} safe to purge   🔴 {len(unsafe)} DO NOT PURGE")
    if unsafe:
        print(f"\n  ⚠ Symbols to EXCLUDE from --apply:")
        for s in unsafe:
            print(f"      {s}")
    if safe:
        print(f"\n  Safe symbols to pass to diag_clear_retired_symbol --apply:")
        print(f"    {' '.join(safe)}")
    print("=" * 90)


if __name__ == "__main__":
    main()
