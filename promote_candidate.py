#!/usr/bin/env python3
"""Promote a CC-approved candidate to the live config.

Applies the same variant transformation the CC runner uses (scales sell_px /
buy_px around the mid, scales trail_distance) to the CURRENT LIVE CONFIG for
the given symbol. Writes to the same store the bot reads from, so the next
loop tick picks up the new values.

Safety:
  - Default is PREVIEW mode. Shows the exact before → after diff, writes
    nothing. Requires --confirm to actually mutate live state.
  - --confirm always backs up the old config to /tmp/promote_backup_<sym>_<ts>.json
    first. Revert is a one-liner (see the printed instructions).
  - Not idempotent — running --confirm twice scales twice. Preview first.

Usage:
    # Preview PT tighter (0.75x band)
    SWING_TENANT=adam-live python3 promote_candidate.py \\
        --symbol PT-28SEP26-CDE --mult 0.75

    # Confirm and write
    SWING_TENANT=adam-live python3 promote_candidate.py \\
        --symbol PT-28SEP26-CDE --mult 0.75 --confirm

    # Revert from backup
    SWING_TENANT=adam-live python3 promote_candidate.py \\
        --symbol PT-28SEP26-CDE --revert /tmp/promote_backup_PT_28SEP26_CDE_XXXX.json --confirm
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from datetime import datetime, timezone


def _scale_node(node: dict, mult: float) -> None:
    """Same scaler used by run_champion_challenger.py _variant()."""
    sell = node.get("sell_px"); buy = node.get("buy_px")
    if sell is not None and buy is not None:
        s = float(sell); b = float(buy)
        if s > b:
            mid = (s + b) / 2.0
            half = ((s - b) / 2.0) * mult
            node["sell_px"] = round(mid + half, 6)
            node["buy_px"] = round(mid - half, 6)
    td = node.get("trail_distance")
    if td is not None:
        node["trail_distance"] = round(float(td) * mult, 6)


def _apply_variant(cfg: dict, mult: float) -> dict:
    c = copy.deepcopy(cfg)
    _scale_node(c, mult)
    for s in (c.get("sleeves") or []):
        if isinstance(s, dict):
            _scale_node(s, mult)
    return c


def _pick_tenant(store) -> str:
    env = os.getenv("SWING_TENANT")
    if env:
        return env
    tenants = list(store.list_tenants() or [])
    live = [t for t in tenants if t.endswith("-live")]
    return live[0] if live else "adam"


def _diff_row(label: str, before: dict, after: dict) -> None:
    fields = ["sell_px", "buy_px", "trail_distance"]
    any_change = False
    for f in fields:
        b = before.get(f); a = after.get(f)
        if b != a and (b is not None or a is not None):
            if not any_change:
                print(f"  {label}:")
                any_change = True
            print(f"    {f}: {b} → {a}")


def _print_diff(before: dict, after: dict) -> None:
    _diff_row("ROOT (primary trader)", before, after)
    b_sleeves = {}
    for s in (before.get("sleeves") or []):
        if isinstance(s, dict) and s.get("id"):
            b_sleeves[s["id"]] = s
    a_sleeves = {}
    for s in (after.get("sleeves") or []):
        if isinstance(s, dict) and s.get("id"):
            a_sleeves[s["id"]] = s
    for sid, b in b_sleeves.items():
        a = a_sleeves.get(sid)
        if a is None:
            continue
        _diff_row(f"SLEEVE {sid} ({b.get('name') or ''})", b, a)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--mult", type=float,
                    help="Variant multiplier: 0.75 = tighter, 1.25 = wider")
    ap.add_argument("--revert", help="Path to backup .json to restore from")
    ap.add_argument("--confirm", action="store_true",
                    help="Actually write to the store (default is preview-only)")
    args = ap.parse_args()

    if not args.mult and not args.revert:
        ap.error("either --mult or --revert is required")
    if args.mult and args.revert:
        ap.error("--mult and --revert are mutually exclusive")

    data_dir = os.getenv("SWING_DATA_DIR", "data")

    from state_store import make_store
    store = make_store(data_dir)
    tenant = _pick_tenant(store)
    print(f"[diag] tenant={tenant!r}  symbol={args.symbol!r}")

    current = store.get_config(tenant, args.symbol)
    if not current:
        print(f"ERROR: no config for {tenant}/{args.symbol}")
        return 1

    if args.revert:
        try:
            with open(args.revert) as f:
                new = json.load(f)
            print(f"Restoring from backup {args.revert}")
        except Exception as e:
            print(f"ERROR reading backup {args.revert}: {type(e).__name__}: {e}")
            return 1
    else:
        new = _apply_variant(current, args.mult)
        direction = "TIGHTER" if args.mult < 1 else ("WIDER" if args.mult > 1 else "NO-CHANGE")
        print(f"Applying variant mult={args.mult} ({direction})")

    print("\n=== BEFORE → AFTER ===")
    _print_diff(current, new)
    if current == new:
        print("  (no differences — nothing to change)")
        return 0

    if not args.confirm:
        print("\n(preview only — pass --confirm to write)")
        return 0

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_sym = args.symbol.replace("-", "_").replace("/", "_")
    backup_path = f"/tmp/promote_backup_{safe_sym}_{ts}.json"
    try:
        with open(backup_path, "w") as f:
            json.dump(current, f, indent=2, default=str)
        print(f"\nBackup written: {backup_path}")
    except Exception as e:
        print(f"ERROR writing backup: {type(e).__name__}: {e}")
        print("REFUSING to promote without a backup.")
        return 1

    try:
        store.put_config(tenant, args.symbol, new)
    except Exception as e:
        print(f"ERROR writing config: {type(e).__name__}: {e}")
        return 1
    print(f"Promoted: {tenant}/{args.symbol}")
    print(f"\nRevert (if you regret it):")
    print(f"  SWING_TENANT={tenant} python3 promote_candidate.py "
          f"--symbol {args.symbol} --revert {backup_path} --confirm")
    return 0


if __name__ == "__main__":
    sys.exit(main())
