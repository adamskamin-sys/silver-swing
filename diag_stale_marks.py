"""Detect stale marks across all products vs. live Coinbase prices.

Adam 2026-07-18: ZEC dashboard showed LIVE mark $548.25 but chart
showed real market reached $552. 24h high/low both $548.25 (impossible
= frozen feed). This is the PLAT stale-mark bleed class from
feedback_refresh_all_marks_not_just_primary.md.

Diag compares __portfolio__ snapshot mark AND per-product state mark
against a FRESH Coinbase call for each held/armed product. Flags any
divergence > 0.5% as stale.

Read-only. No writes.

Usage:
    python3 diag_stale_marks.py
    python3 diag_stale_marks.py --threshold 0.5   # default 0.5%
"""
from __future__ import annotations
import argparse
import os
import sys
import time


TENANT = "adam-live"


def _fresh_mark(pid: str) -> tuple[float, str]:
    """Fetch a fresh mark from Coinbase. Returns (mark, error_or_ok)."""
    try:
        from broker import BrokerConfig, CoinbaseBroker, _dump
        b = CoinbaseBroker(BrokerConfig(product_id=pid))
        # Prefer get_product for freshness (avoids any position-list caching)
        resp = _dump(b.client.get_product(pid)) or {}
        mark = float(resp.get("price") or 0)
        if mark <= 0:
            return 0.0, "no price in get_product"
        return mark, "ok"
    except Exception as e:
        return 0.0, f"{type(e).__name__}: {e}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.5,
                    help="drift threshold %% (default 0.5)")
    ap.add_argument("--data-dir", default=os.getenv("SWING_DATA_DIR", "data"))
    args = ap.parse_args()

    print("=" * 90)
    print(f"STALE-MARK DETECTOR — threshold {args.threshold}%")
    print("=" * 90)

    from state_store import make_store
    store = make_store(args.data_dir)

    # Which products to check? Anything with state OR held OR in portfolio.
    products_to_check: set[str] = set()
    try:
        for sym in store.list_symbols(TENANT):
            if sym.startswith("__"):
                continue
            products_to_check.add(sym)
    except Exception as e:
        print(f"  ✗ list_symbols failed: {e}")
        sys.exit(1)

    pf = store.get_config(TENANT, "__portfolio__") or {}
    pf_ts = pf.get("_refresh_ts")
    pf_age_str = "unknown"
    if pf_ts:
        try:
            age = time.time() - float(pf_ts)
            pf_age_str = f"{int(age)}s ago"
        except (TypeError, ValueError):
            pass
    print(f"\n__portfolio__ last refresh: {pf_age_str}")
    print(f"__portfolio__ _refresh_ok:  {pf.get('_refresh_ok')}")
    if pf.get("_last_error"):
        print(f"__portfolio__ _last_error:  {pf.get('_last_error')}")

    # Build a lookup of what portfolio snapshot says
    pf_marks: dict = {}
    for d in pf.get("derivatives") or []:
        pid = d.get("product_id")
        try:
            pf_marks[pid] = float(d.get("mark") or 0)
        except (TypeError, ValueError):
            pass

    print(f"\n{len(products_to_check)} products to check\n")

    stale = []
    ok = []
    err = []

    for sym in sorted(products_to_check):
        # Per-symbol snapshot mark
        snap = store.get_snapshot(TENANT, sym) if hasattr(store, "get_snapshot") else None
        snap_mark = 0.0
        snap_ts = None
        if isinstance(snap, dict):
            try:
                snap_mark = float(snap.get("last_mark") or snap.get("mark") or 0)
            except (TypeError, ValueError):
                pass
            snap_ts = snap.get("generated_at") or snap.get("mark_ts")

        pf_mark = pf_marks.get(sym, 0.0)
        live_mark, live_err = _fresh_mark(sym)

        if live_err != "ok":
            err.append((sym, live_err))
            print(f"  ✗ {sym:<28}  live fetch failed: {live_err}")
            continue

        # Compare each source to live
        def _pct(a, b):
            if b <= 0: return 0.0
            return abs(a - b) / b * 100.0

        pf_drift = _pct(pf_mark, live_mark) if pf_mark > 0 else None
        snap_drift = _pct(snap_mark, live_mark) if snap_mark > 0 else None

        is_stale = False
        if pf_drift is not None and pf_drift > args.threshold:
            is_stale = True
        if snap_drift is not None and snap_drift > args.threshold:
            is_stale = True

        line = f"  {'🔴' if is_stale else '✓ '} {sym:<28}  live=${live_mark:.4f}"
        if pf_mark > 0:
            line += f"  portfolio=${pf_mark:.4f} ({pf_drift:.2f}%)"
        if snap_mark > 0:
            snap_age = ""
            if snap_ts:
                try:
                    a = time.time() - float(snap_ts)
                    snap_age = f", {int(a)}s ago"
                except (TypeError, ValueError):
                    pass
            line += f"  snapshot=${snap_mark:.4f} ({snap_drift:.2f}%{snap_age})"
        print(line)

        if is_stale:
            stale.append((sym, live_mark, pf_mark, snap_mark))
        else:
            ok.append(sym)

    print()
    print("=" * 90)
    print(f"SUMMARY: 🔴 {len(stale)} STALE   ✓ {len(ok)} fresh   ✗ {len(err)} errored")
    if stale:
        print(f"\n  Stale marks — action items:")
        for sym, live, pfm, snm in stale:
            print(f"    · {sym}: live=${live:.4f}  bot_mark_sources shown above")
        print(f"\n  Per feedback_refresh_all_marks_not_just_primary:")
        print(f"    On drift → force refresh + reconnect feed, NEVER halt.")
        print(f"    If Coinbase infra is degraded (recent 502s), wait it out.")
    print("=" * 90)


if __name__ == "__main__":
    main()
