"""Enumerate reconciliation-monitor findings so the operator can
decide which are ghost data (from retired products) vs real issues.

Adam 2026-07-16: dashboard shows the counter (e.g. 6 critical + 4 warn)
but doesn't say WHICH ones or which are real. This diag reads the
trade log for reconciliation_* events over the last N hours and
groups by kind + symbol, with per-finding heuristics for likelihood:

  🟢 LIKELY GHOST — retired product / cleared sleeve / stale state
  🟠 NEEDS REVIEW — could be real, worth eyeballing
  🔴 LIKELY REAL — matches a known bug class

Kinds enumerated:
  - reconciliation_position_mismatch  (state ≠ Coinbase)
  - reconciliation_state_config_drift (state.swing_qty > config.swing_qty)
  - reconciliation_stale_entry        (sleeve idle > threshold)
  - reconciliation_safety_halt        (sleeve in HALTED)
  - reconciliation_orphan_order       (Coinbase order not in state)
  - reconciliation_missing_order      (state expects order Coinbase doesn't have)
  - reconciliation_duplicate_order    (2+ orders on same sleeve)

Read-only. No writes.

Usage:
    python3 diag_reconcile_events.py                  # last 4h
    python3 diag_reconcile_events.py --hours 24
    python3 diag_reconcile_events.py --kind position_mismatch
"""
from __future__ import annotations
import argparse
import os
import sys
import time
from collections import defaultdict


RECON_EVENT_PREFIX = "reconciliation_"
KNOWN_KINDS = [
    "position_mismatch",
    "state_config_drift",
    "stale_entry",
    "safety_halt",
    "orphan_order",
    "missing_order",
    "duplicate_order",
]


def _classify(kind: str, symbol: str, detail: dict,
              retired_syms: set, live_holds: dict) -> tuple[str, str]:
    """Return (icon, one-line reasoning) for a finding."""
    # Retired products: any finding on a symbol we retired is stale
    if symbol in retired_syms:
        return "🟢", f"{symbol} was retired — finding is ghost data"

    # No position held on this symbol + no armed sleeves → likely stale
    pos = live_holds.get(symbol, 0)

    if kind == "state_config_drift":
        state_sq = detail.get("state_swing_qty") if isinstance(detail, dict) else None
        config_sq = detail.get("config_swing_qty") if isinstance(detail, dict) else None
        if pos == 0 and state_sq and config_sq is not None and state_sq > config_sq:
            return "🟠", (f"state.swing_qty={state_sq} vs config={config_sq}; "
                          f"pos=0 → auto-correct should clear (see live_runner:1344)")
        return "🔴", f"state disagrees with config on active-position sym"

    if kind == "position_mismatch":
        if pos == 0:
            return "🟠", "bot state expected a position; Coinbase says 0 — likely stale state"
        return "🔴", f"REAL mismatch on held sym (pos={pos}) — money at risk"

    if kind == "stale_entry":
        return "🟢", "informational — sleeve idle past threshold (normal for waiters)"

    if kind == "safety_halt":
        return "🟠", f"sleeve HALTED — check halt_reason; may need manual resume"

    if kind in ("orphan_order", "missing_order"):
        return "🟠", "order state drift — usually self-heals on next reconcile"

    if kind == "duplicate_order":
        return "🔴", "MULTIPLE orders on same sleeve — real, needs investigation"

    return "?", ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=4.0)
    ap.add_argument("--kind", default=None,
                    help="filter to one kind (e.g. position_mismatch)")
    ap.add_argument("--data-dir", default=os.getenv("SWING_DATA_DIR", "data"))
    args = ap.parse_args()

    cutoff_ts = time.time() - args.hours * 3600.0
    tenant = "adam-live"

    print("=" * 90)
    print(f"RECONCILIATION EVENTS — last {args.hours}h "
          f"{f'(kind={args.kind})' if args.kind else ''}")
    print("=" * 90)

    # Read trade log via the safety layer (same Redis backend the bot uses)
    events = []
    try:
        from safety import make_trade_log
        log = make_trade_log(args.data_dir)
        log_backend = type(log).__name__
        for e in log.events():
            try:
                if float(e.get("ts") or 0) < cutoff_ts:
                    continue
                etype = str(e.get("event_type") or "")
                if not etype.startswith(RECON_EVENT_PREFIX):
                    continue
                kind = etype[len(RECON_EVENT_PREFIX):]
                if args.kind and kind != args.kind:
                    continue
                events.append(e)
            except (ValueError, TypeError):
                pass
    except Exception as e:
        print(f"\n✗ Failed to read trade log: {type(e).__name__}: {e}")
        sys.exit(1)

    # Read state to identify retired products (config exists but no sleeves + swing_qty=0)
    # and current holdings (position_qty from __portfolio__)
    retired_syms = set()
    live_holds: dict = {}
    try:
        from state_store import make_store
        store = make_store(args.data_dir)
        pf = store.get_state(tenant, "__portfolio__") or {}
        for sym, snap in pf.items():
            if sym.startswith("__"):
                continue
            if isinstance(snap, dict):
                try:
                    live_holds[sym] = int(float(snap.get("position_qty") or 0))
                except (TypeError, ValueError):
                    live_holds[sym] = 0
        for sym in store.list_symbols(tenant):
            if sym.startswith("__"):
                continue
            st = store.get_state(tenant, sym) or {}
            cfg = store.get_config(tenant, sym) or {}
            sleeves = st.get("sleeves") or {}
            if (not sleeves and int(cfg.get("swing_qty") or 0) == 0
                    and int(cfg.get("core_qty") or 0) == 0
                    and live_holds.get(sym, 0) == 0):
                retired_syms.add(sym)
    except Exception as e:
        print(f"  (warning) Failed to load state for classification: {e}")

    print(f"\nBackend: {log_backend}  |  {len(events)} matching events  |  "
          f"retired products: {len(retired_syms)}")

    if not events:
        print("\n✓ No reconciliation events in the window. Clean.")
        print("=" * 90)
        return

    # Group by kind → symbol
    by_kind: dict = defaultdict(lambda: defaultdict(list))
    for e in events:
        kind = str(e.get("event_type") or "")[len(RECON_EVENT_PREFIX):]
        sym = str(e.get("symbol") or "?")
        by_kind[kind][sym].append(e)

    ghost = review = real = 0
    for kind in sorted(by_kind.keys()):
        symbols = by_kind[kind]
        total = sum(len(v) for v in symbols.values())
        print(f"\n─── {kind}  ({total} events across {len(symbols)} symbols)")
        for sym in sorted(symbols.keys()):
            evs = symbols[sym]
            most_recent = max(evs, key=lambda e: float(e.get("ts") or 0))
            age = time.time() - float(most_recent.get("ts") or 0)
            severity = most_recent.get("severity", "?")
            detail = most_recent.get("detail") or {}
            icon, reasoning = _classify(kind, sym, detail, retired_syms, live_holds)
            if icon == "🟢": ghost += 1
            elif icon == "🟠": review += 1
            elif icon == "🔴": real += 1
            pos_note = f"pos={live_holds.get(sym, 0)}" if sym in live_holds else "pos=?"
            print(f"  {icon} {sym:32}  [{severity:8}]  {len(evs):3}× last {int(age)}s ago  {pos_note}")
            if reasoning:
                print(f"       → {reasoning}")

    print()
    print("=" * 90)
    print(f"SUMMARY: 🟢 {ghost} likely-ghost   🟠 {review} needs-review   "
          f"🔴 {real} likely-real")
    if real > 0:
        print("  🔴 Real findings deserve immediate attention.")
    if review > 0:
        print("  🟠 Review findings should be spot-checked (may or may not be real).")
    if ghost > 0:
        print("  🟢 Ghost findings are safe to ignore or clean up "
              "via diag_audit_stale_scopes.py.")
    print("=" * 90)


if __name__ == "__main__":
    main()
