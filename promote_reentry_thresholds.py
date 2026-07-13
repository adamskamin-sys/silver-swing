"""Manual promotion gate for tuned re-entry thresholds (crew).

Reads the JSON report emitted by tune_reentry_thresholds.py, prints a
per-product review, and — ONLY with --confirm — writes the tuned
thresholds to the __tuned_reentry_params__ scope in the state store
(Redis on Render). Mirrors the champion-challenger promote pattern
(promote_candidate.py + revert_todays_promotions.py) so the same
mental model applies: preview → confirm → write, with a durable backup
so revert is one command away.

Rollout stages (Pardo 2008 style, matching your existing CC pattern):
  1. Run tune_reentry_thresholds.py — writes reentry_tuning_report.json
  2. This script (no --confirm) — preview all "PUBLISH" recommendations
  3. This script --confirm --symbol X — promote one product at a time
  4. If the sleeve misbehaves: delete the symbol key in
     __tuned_reentry_params__ (there's a --revert flag below) — the
     orchestrator falls back to config, then to DEFAULT_THRESHOLDS.

References
----------
Pardo, Robert. *The Evaluation and Optimization of Trading Strategies*.
Wiley, 2008. Ch. 13-14 — operational promotion of walk-forward
recommendations to live.

Usage
-----
    python3 promote_reentry_thresholds.py                          # preview all
    python3 promote_reentry_thresholds.py --symbol NOL-20JUL26-CDE # preview one
    python3 promote_reentry_thresholds.py --symbol NOL --confirm   # promote
    python3 promote_reentry_thresholds.py --revert --symbol NOL --confirm
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional

from state_store import make_store


SCOPE = "__tuned_reentry_params__"


def _load_report(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def _load_current(store, tenant: str) -> dict:
    return store.get_state(tenant, SCOPE) or {}


def _save(store, tenant: str, data: dict) -> None:
    store.put_state(tenant, SCOPE, data)


def _print_recommendation(sym: str, r: dict) -> None:
    print(f"\n{sym}")
    print(f"  n = {r.get('n_train', 0) + r.get('n_oos', 0)} "
          f"(train {r.get('n_train')}, OOS {r.get('n_oos')})")
    print(f"  baseline OOS profit = {r.get('baseline_oos_profit'):>10.2f}")
    print(f"  tuned OOS profit    = {r.get('oos_profit'):>10.2f}   "
          f"(Δ = {(r.get('oos_profit', 0) - r.get('baseline_oos_profit', 0)):+.2f})")
    print(f"  OOS/IS ratio        = {r.get('oos_over_is_ratio'):>6.2f}  "
          f"(Bailey guard >= 0.50)")
    print(f"  SQN (OOS)           = {r.get('sqn_oos'):>6.2f}  "
          f"(Van Tharp floor >= 1.00, baseline {r.get('sqn_baseline_oos', 0):.2f})")
    print(f"  status              = {'PUBLISH' if r.get('published') else 'HOLD'} — "
          f"{r.get('publish_reason')}")
    delta = r.get("delta_from_default") or {}
    if delta:
        print(f"  changes vs default:")
        for k, v in delta.items():
            print(f"    {k:<30} → {v}")
    else:
        print(f"  changes vs default: none (defaults already optimal)")


def _tenant_of(sym: str) -> str:
    """Coinbase symbols land in adam-live; spot USD too. Local paper tenant
    only carries SLR + AVE + PEP for testing. Default all to adam-live
    unless overridden."""
    return "adam-live"


def promote(store, sym: str, thresholds: dict, backup_path: str) -> None:
    tenant = _tenant_of(sym)
    current = _load_current(store, tenant)
    prev = current.get(sym)
    # Durable backup — one command to revert.
    backup = {"ts": time.time(), "tenant": tenant, "symbol": sym,
              "previous": prev, "promoted": thresholds}
    with open(backup_path, "w") as f:
        json.dump(backup, f, indent=2, default=str)
    current[sym] = thresholds
    _save(store, tenant, current)
    print(f"  → wrote {sym} thresholds to {tenant}/{SCOPE}. "
          f"Backup at {backup_path}.")


def revert(store, sym: str, backup_path: Optional[str] = None) -> None:
    tenant = _tenant_of(sym)
    current = _load_current(store, tenant)
    prev_value = None
    if backup_path and os.path.exists(backup_path):
        with open(backup_path) as f:
            backup = json.load(f)
        prev_value = backup.get("previous")
    if prev_value is None:
        current.pop(sym, None)
        print(f"  → deleted {sym} from {tenant}/{SCOPE} "
              f"(no backup → orchestrator falls back to defaults).")
    else:
        current[sym] = prev_value
        print(f"  → restored {sym} in {tenant}/{SCOPE} to backup value.")
    _save(store, tenant, current)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default="reentry_tuning_report.json")
    ap.add_argument("--symbol", default=None,
                    help="filter to one symbol (substring match)")
    ap.add_argument("--confirm", action="store_true",
                    help="actually write (default: preview only)")
    ap.add_argument("--revert", action="store_true",
                    help="revert instead of promote")
    ap.add_argument("--backup-dir", default="backups")
    args = ap.parse_args()

    os.makedirs(args.backup_dir, exist_ok=True)
    store = make_store(os.getenv("SWING_DATA_DIR", "data"))

    if args.revert:
        if not args.symbol:
            print("--revert requires --symbol")
            sys.exit(1)
        if not args.confirm:
            print(f"PREVIEW: would revert {args.symbol}. Re-run with --confirm.")
            return
        backup = os.path.join(args.backup_dir, f"reentry_{args.symbol}.json")
        revert(store, args.symbol, backup_path=backup)
        return

    report = _load_report(args.report)
    to_promote: list[tuple[str, dict]] = []
    for sym, r in report.items():
        if args.symbol and args.symbol.upper() not in sym.upper():
            continue
        if r.get("skipped") or r.get("error"):
            print(f"\n{sym}: skipped/error — {r.get('reason') or r.get('error')}")
            continue
        _print_recommendation(sym, r)
        if r.get("published"):
            to_promote.append((sym, r.get("tuned_thresholds") or {}))

    if not args.confirm:
        print(f"\n{len(to_promote)} product(s) eligible to promote. "
              f"Re-run with --confirm to write.")
        return

    for sym, thr in to_promote:
        backup = os.path.join(args.backup_dir, f"reentry_{sym}.json")
        promote(store, sym, thr, backup_path=backup)
    print(f"\nPromoted {len(to_promote)} product(s).")


if __name__ == "__main__":
    main()
