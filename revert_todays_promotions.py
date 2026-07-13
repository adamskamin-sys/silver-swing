#!/usr/bin/env python3
"""Revert today's promote_candidate.py changes to the exact pre-promotion
values, using the snapshot file at backups/pre_promote_20260713.json.

Why not just use promote_candidate.py's --revert? That path needs a full-
config backup file (was written to /tmp on the promotion pod, wiped when
Render restarted). This script instead does a SURGICAL revert: reads the
current live config, sets JUST the fields promote_candidate.py touched
back to their pre-promotion values, leaves everything else alone.

Safety:
  - Preview by default (no --confirm) — prints the exact fields it would
    change with before → after values, does not write.
  - --confirm actually writes. Backs up the CURRENT config to /tmp before
    overwriting, so a mis-revert can be reversed.
  - Refuses to revert if the current field values don't match either the
    pre_promotion OR post_promotion snapshot — that means something else
    changed the field and a blind revert would clobber it. Force with
    --force to override that safety.

Usage:
    # Preview PT revert
    SWING_TENANT=adam-live python3 revert_todays_promotions.py --symbol PT-28SEP26-CDE

    # Actually write
    SWING_TENANT=adam-live python3 revert_todays_promotions.py --symbol PT-28SEP26-CDE --confirm

    # Revert all three at once
    SWING_TENANT=adam-live python3 revert_todays_promotions.py --all --confirm
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from datetime import datetime, timezone

SNAPSHOT_PATH = os.path.join(os.path.dirname(__file__), "backups", "pre_promote_20260713.json")


def _pick_tenant(store) -> str:
    env = os.getenv("SWING_TENANT")
    if env:
        return env
    live = [t for t in (store.list_tenants() or []) if t.endswith("-live")]
    return live[0] if live else "adam"


def _load_snapshot() -> dict:
    with open(SNAPSHOT_PATH) as f:
        return json.load(f)


def _revert_one(store, tenant: str, symbol: str, spec: dict,
                confirm: bool, force: bool) -> int:
    """Revert a single symbol. Returns 0 on success, non-zero on error/skip."""
    cfg = store.get_config(tenant, symbol)
    if not cfg:
        print(f"  ERROR: no live config for {tenant}/{symbol}")
        return 1
    new_cfg = copy.deepcopy(cfg)

    # Track proposed field changes for the preview
    changes: list[str] = []

    # Root reverts (only SLR has root fields in this snapshot)
    root_spec = spec.get("root") or {}
    pre = root_spec.get("pre_promotion") or {}
    post = root_spec.get("post_promotion") or {}
    for field, want in pre.items():
        got = cfg.get(field)
        expected_current = post.get(field)
        if not force and got != want and got != expected_current:
            print(f"  MISMATCH root.{field}: current={got} expected pre={want} or post={expected_current}")
            print(f"    (something else changed this field — refusing to revert without --force)")
            return 2
        if got != want:
            changes.append(f"    root.{field}: {got} → {want}")
            new_cfg[field] = want

    # Sleeve reverts
    sleeve_specs = spec.get("sleeves") or []
    cfg_sleeves = {s.get("id"): s for s in (cfg.get("sleeves") or []) if isinstance(s, dict)}
    new_sleeves = new_cfg.get("sleeves") or []
    new_sleeve_by_id = {s.get("id"): s for s in new_sleeves if isinstance(s, dict)}

    for ss in sleeve_specs:
        sid = ss.get("id")
        if sid not in cfg_sleeves:
            print(f"  WARNING: sleeve {sid} in snapshot but not in current config; skipping")
            continue
        cur_sleeve = cfg_sleeves[sid]
        target_sleeve = new_sleeve_by_id[sid]
        pre_s = ss.get("pre_promotion") or {}
        post_s = ss.get("post_promotion") or {}
        for field, want in pre_s.items():
            got = cur_sleeve.get(field)
            expected_current = post_s.get(field)
            if not force and got != want and got != expected_current:
                print(f"  MISMATCH sleeve[{sid}].{field}: current={got} expected pre={want} or post={expected_current}")
                print(f"    (something else changed this field — refusing to revert without --force)")
                return 2
            if got != want:
                changes.append(f"    sleeve[{sid}].{field}: {got} → {want}")
                target_sleeve[field] = want

    if not changes:
        print(f"  {symbol}: already at pre-promotion values (no changes)")
        return 0

    print(f"  {symbol}: {len(changes)} field(s) to revert:")
    for c in changes:
        print(c)

    if not confirm:
        print(f"  (preview only for {symbol} — pass --confirm to write)")
        return 0

    # Back up current state to /tmp first (temporary net if the revert was a mistake)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_sym = symbol.replace("-", "_").replace("/", "_")
    backup_path = f"/tmp/revert_backup_{safe_sym}_{ts}.json"
    try:
        with open(backup_path, "w") as f:
            json.dump(cfg, f, indent=2, default=str)
    except Exception as e:
        print(f"  ERROR writing backup: {type(e).__name__}: {e}")
        print(f"  REFUSING to revert {symbol} without a backup.")
        return 1
    print(f"  backup written: {backup_path}")
    try:
        store.put_config(tenant, symbol, new_cfg)
    except Exception as e:
        print(f"  ERROR writing config: {type(e).__name__}: {e}")
        return 1
    print(f"  reverted: {tenant}/{symbol}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", help="Revert just this symbol")
    ap.add_argument("--all", action="store_true",
                    help="Revert every symbol in the snapshot")
    ap.add_argument("--confirm", action="store_true",
                    help="Actually write to the store (default is preview)")
    ap.add_argument("--force", action="store_true",
                    help="Revert even if the current field doesn't match the "
                         "snapshot's pre or post value (dangerous — may clobber "
                         "another agent's change)")
    args = ap.parse_args()

    if not args.symbol and not args.all:
        ap.error("--symbol or --all is required")

    snap = _load_snapshot()
    symbols_spec = snap.get("symbols") or {}
    if args.symbol:
        if args.symbol not in symbols_spec:
            print(f"ERROR: {args.symbol} not in snapshot. Known: {list(symbols_spec.keys())}")
            return 1
        targets = [args.symbol]
    else:
        targets = list(symbols_spec.keys())

    data_dir = os.getenv("SWING_DATA_DIR", "data")
    from state_store import make_store
    store = make_store(data_dir)
    tenant = _pick_tenant(store)
    print(f"[diag] tenant={tenant!r}  targets={targets}  confirm={args.confirm}  force={args.force}")

    rc = 0
    for sym in targets:
        print(f"\n--- {sym} ---")
        r = _revert_one(store, tenant, sym, symbols_spec[sym], args.confirm, args.force)
        if r != 0:
            rc = r
    return rc


if __name__ == "__main__":
    sys.exit(main())
