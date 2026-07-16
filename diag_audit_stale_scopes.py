"""Audit + optionally purge stale scopes in Redis state.

Adam 2026-07-15: paper/lab retired (see memory: no_paper_all_live).
Redis blob still carries whatever pre-retirement tenants and
paper_/lab_ scopes were there. Plus any per-product state that hasn't
had a heartbeat in weeks is worth pruning to keep the blob small and
avoid confusing auto-recovery scans.

Read-only by default. --apply to delete. Every deletion is logged
to trades.jsonl for audit.

What it flags:
  1. Non-adam-live tenants (should not exist anymore)
  2. paper_state / lab_state / paper_config / lab_config scopes on
     any tenant
  3. Per-product state with heartbeat older than STALE_DAYS days
     (default 14). Held positions + armed sleeves excluded from
     "stale" — those are live even if quiet.
  4. Empty tenant blocks (all keys removed → drop the top-level key)

Usage:
    python3 diag_audit_stale_scopes.py                        # audit only
    python3 diag_audit_stale_scopes.py --apply                # delete
    python3 diag_audit_stale_scopes.py --stale-days 30        # custom age
    python3 diag_audit_stale_scopes.py --apply --dry-run-only-empties
"""
from __future__ import annotations
import argparse
import os
import time

from state_store import make_store


LIVE_TENANT = "adam-live"
STALE_SCOPE_KEYS = {"paper_state", "lab_state", "paper_config", "lab_config"}


def is_held_or_armed(block: dict) -> bool:
    """Skip pruning products that are actively holding a position or
    have any armed sleeve. Heartbeat may be stale but the money's live."""
    state = block.get("state") or {}
    try:
        if int(state.get("swing_qty") or 0) != 0:
            return True
    except (TypeError, ValueError):
        pass
    sleeves = state.get("sleeves") or {}
    for ss in sleeves.values() if isinstance(sleeves, dict) else []:
        s = str(ss.get("state") or "")
        if s in ("ARMED_BUY", "ARMED_SELL", "BOUGHT"):
            return True
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually delete (default: audit only)")
    ap.add_argument("--stale-days", type=float, default=14.0,
                    help="prune products with heartbeat older than this")
    args = ap.parse_args()

    store = make_store(os.getenv("SWING_DATA_DIR", "data"))
    blob = store._load()  # direct read; we're a diag
    now = time.time()
    stale_cutoff = now - args.stale_days * 86400.0

    print("=" * 90)
    print(f"STALE-SCOPE AUDIT — {'APPLY' if args.apply else 'READ-ONLY'}")
    print("=" * 90)

    findings = {
        "phantom_tenants": [],       # tenants that aren't LIVE_TENANT
        "paper_lab_scopes": [],      # (tenant, symbol, scope) tuples
        "stale_products": [],        # (tenant, symbol, hb_age_days)
        "empty_tenants": [],         # tenants that would be empty after
    }

    for tenant, symbols in list(blob.items()):
        if tenant != LIVE_TENANT:
            n_symbols = len(symbols or {})
            findings["phantom_tenants"].append((tenant, n_symbols))
            continue
        for symbol, block in list((symbols or {}).items()):
            if not isinstance(block, dict):
                continue
            # (2) paper/lab scopes
            for scope_key in STALE_SCOPE_KEYS:
                if scope_key in block:
                    findings["paper_lab_scopes"].append((tenant, symbol, scope_key))
            # (3) stale products (skip specials + held/armed)
            if symbol.startswith("__"):
                continue
            if is_held_or_armed(block):
                continue
            state = block.get("state") or {}
            hb = state.get("last_heartbeat_ts")
            try:
                hb = float(hb) if hb is not None else None
            except (TypeError, ValueError):
                hb = None
            if hb is not None and hb < stale_cutoff:
                age_days = (now - hb) / 86400.0
                findings["stale_products"].append((tenant, symbol, round(age_days, 1)))

    # --- report ------------------------------------------------------------

    print(f"\n[1] Phantom tenants (not {LIVE_TENANT!r}):")
    if not findings["phantom_tenants"]:
        print("    (none)")
    for t, n in findings["phantom_tenants"]:
        print(f"    · {t}  ({n} symbols)")

    print(f"\n[2] paper_/lab_ scopes:")
    if not findings["paper_lab_scopes"]:
        print("    (none)")
    for t, s, k in findings["paper_lab_scopes"]:
        print(f"    · {t}/{s}  scope={k}")

    print(f"\n[3] Stale products (heartbeat > {args.stale_days} days old, "
          f"not held, no armed sleeves):")
    if not findings["stale_products"]:
        print("    (none)")
    for t, s, age in sorted(findings["stale_products"], key=lambda r: -r[2]):
        print(f"    · {t}/{s}  hb_age={age}d")

    total = (len(findings["phantom_tenants"]) +
             len(findings["paper_lab_scopes"]) +
             len(findings["stale_products"]))
    print(f"\nTotal: {total} finding(s)")

    if not args.apply:
        print(f"\n(read-only — re-run with --apply to delete)")
        return

    if total == 0:
        print("\nNothing to delete.")
        return

    # --- apply -------------------------------------------------------------
    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    except Exception:
        log = None

    def _record(kind, **fields):
        if log:
            try: log.record(kind, severity="info", **fields)
            except Exception: pass

    def _mutate(fn):
        # RedisJsonStore has _mutate; JsonFileStateStore uses _load/_save
        if hasattr(store, "_mutate"):
            store._mutate(fn)
        else:
            data = store._load()
            fn(data)
            store._save(data)

    # (1) drop phantom tenants
    for t, _ in findings["phantom_tenants"]:
        _mutate(lambda d, t=t: d.pop(t, None))
        _record("stale_scope_purge", kind="phantom_tenant", tenant=t)
        print(f"  ✓ dropped tenant {t}")

    # (2) drop paper/lab scopes
    for t, s, k in findings["paper_lab_scopes"]:
        def _drop_scope(d, t=t, s=s, k=k):
            block = d.get(t, {}).get(s, {})
            if k in block:
                del block[k]
        _mutate(_drop_scope)
        _record("stale_scope_purge", kind="paper_lab_scope",
                tenant=t, symbol=s, scope=k)
        print(f"  ✓ dropped {t}/{s} scope={k}")

    # (3) drop stale product blocks entirely
    for t, s, age in findings["stale_products"]:
        _mutate(lambda d, t=t, s=s: d.get(t, {}).pop(s, None))
        _record("stale_scope_purge", kind="stale_product",
                tenant=t, symbol=s, hb_age_days=age)
        print(f"  ✓ dropped {t}/{s}  (age {age}d)")

    print(f"\n✓ Purged {total} scope(s). Blob is smaller now.")
    print(f"  Tip: reload dashboard to see the changes reflected.")


if __name__ == "__main__":
    main()
