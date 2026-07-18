"""Query Coinbase directly + all tenants to find where a symbol lives.

Adam 2026-07-17: XLM PERP CDE was actively trading on Coinbase but
diag_symbol_price_trace found no XLM in adam-live's list_symbols.
The trades must originate from somewhere — this diag hunts.

Checks (in this order):
  1. Coinbase get_products search — find every product_id matching
     the substring, so we know what Coinbase calls it internally.
  2. Coinbase list_futures_positions — is there an open position?
  3. Redis: iterate EVERY tenant + symbol, fuzzy-match the substring.
     Catches misspellings, tenant-scope bugs, ghost data.
  4. Trade log: last N hours of any event whose payload contains the
     substring — catches scanner explore mode + non-persistent code
     paths.

Read-only. No writes.

Usage:
    python3 diag_find_symbol.py XLM
    python3 diag_find_symbol.py XLM --hours 6
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("needle")
    ap.add_argument("--hours", type=float, default=4.0)
    ap.add_argument("--data-dir", default=os.getenv("SWING_DATA_DIR", "data"))
    args = ap.parse_args()

    n = args.needle
    n_up = n.upper().replace("-", "").replace("_", "")

    print("=" * 90)
    print(f"HUNT FOR {n!r} — where does this symbol actually live?")
    print("=" * 90)

    # ---- 1. Coinbase get_products across EVERY surface -------------------
    # Per feedback_check_coinbase_product_surfaces: default get_products
    # filters by product_type and can miss perps entirely (2026-07-17 XLM
    # incident). Query each surface explicitly + one unfiltered call, then
    # dedupe. Also try /brokerage/products/list_products with product_type
    # kwargs the SDK exposes.
    print(f"\n[1] Coinbase server-side products matching {n!r}"
          f" — querying every surface")
    try:
        from broker import BrokerConfig, CoinbaseBroker, _dump
        b = CoinbaseBroker(BrokerConfig(product_id="SLR-27AUG26-CDE"))
        all_products: dict[str, dict] = {}
        # Try each product_type filter individually; SDK differences mean
        # some kwargs work, others don't. Fail-open on each attempt.
        surfaces = [
            ("default", {}),
            ("SPOT", {"product_type": "SPOT"}),
            ("FUTURE", {"product_type": "FUTURE"}),
            ("PERPETUAL", {"product_type": "PERPETUAL"}),
            ("INTX_PERP", {"product_type": "INTX_PERPETUAL"}),
        ]
        for label, kwargs in surfaces:
            try:
                resp = _dump(b.client.get_products(**kwargs)) or {}
                products = resp.get("products") or []
                new_count = 0
                for p in products:
                    pid = str(p.get("product_id") or "")
                    if pid and pid not in all_products:
                        all_products[pid] = p
                        new_count += 1
                print(f"  · surface={label:<12} returned {len(products)} products "
                      f"({new_count} new)")
            except Exception as _e:
                print(f"  · surface={label:<12} failed: {type(_e).__name__}: {_e}")
        # Now filter across the union
        matches = []
        for pid, p in all_products.items():
            display = str(p.get("display_name") or p.get("base_display_symbol") or "")
            fields = f"{pid} {display}".upper().replace("-", "").replace("_", "")
            if n_up in fields:
                matches.append((pid, display, p.get("status"), p.get("product_type")))
        print(f"\n  Union: {len(all_products)} unique products across surfaces")
        print(f"  Matching {n!r}: {len(matches)}")
        for pid, display, status, ptype in matches[:30]:
            print(f"    · {pid:<32}  display={display!r:<30}  status={status}  type={ptype}")
        if len(matches) > 30:
            print(f"    ... {len(matches) - 30} more")
    except Exception as e:
        print(f"  ✗ Coinbase query failed: {type(e).__name__}: {e}")

    # ---- 2. Coinbase list_futures_positions (currently held) -------------
    print(f"\n[2] Coinbase list_futures_positions filtered to {n!r}")
    try:
        from broker import BrokerConfig, CoinbaseBroker, _dump
        b = CoinbaseBroker(BrokerConfig(product_id="SLR-27AUG26-CDE"))
        resp = _dump(b.client.list_futures_positions()) or {}
        positions = resp.get("positions") or []
        found = []
        for p in positions:
            pid = str(p.get("product_id") or "")
            if n_up in pid.upper().replace("-", "").replace("_", ""):
                found.append(p)
        if not found:
            print(f"  (no matching positions)")
        for p in found:
            print(f"  · {p.get('product_id')}  qty={p.get('number_of_contracts')}  "
                  f"side={p.get('side')}  avg={p.get('avg_entry_price')}  "
                  f"unreal={p.get('unrealized_pnl')}")
    except Exception as e:
        print(f"  ✗ list_futures_positions failed: {type(e).__name__}: {e}")

    # ---- 3. Redis: every tenant + symbol ---------------------------------
    print(f"\n[3] Redis state — every tenant + symbol matching {n!r}")
    try:
        from state_store import make_store
        store = make_store(args.data_dir)
        found_any = False
        for tenant in store.list_tenants():
            for sym in store.list_symbols(tenant):
                if n_up in sym.upper().replace("-", "").replace("_", ""):
                    found_any = True
                    st = store.get_state(tenant, sym) or {}
                    cfg = store.get_config(tenant, sym) or {}
                    sleeves = st.get("sleeves") or {}
                    print(f"  · {tenant}/{sym}")
                    print(f"      state.state={st.get('state')} "
                          f"swing_qty={st.get('swing_qty')} "
                          f"sleeves={len(sleeves)}")
                    print(f"      cfg.swing_qty={cfg.get('swing_qty')} "
                          f"cfg.core_qty={cfg.get('core_qty')}")
        if not found_any:
            print(f"  (no state/config anywhere matching {n!r})")
    except Exception as e:
        print(f"  ✗ store scan failed: {type(e).__name__}: {e}")

    # ---- 4. Trade log — any event with the needle in payload -------------
    print(f"\n[4] Trade log — events mentioning {n!r} (last {args.hours}h)")
    cutoff = time.time() - args.hours * 3600.0
    matches = []
    try:
        from safety import make_trade_log
        log = make_trade_log(args.data_dir)
        for e in log.events():
            try:
                if float(e.get("ts") or 0) < cutoff:
                    continue
            except (ValueError, TypeError):
                continue
            payload = json.dumps(e, default=str).upper().replace("-", "").replace("_", "")
            if n_up in payload:
                matches.append(e)
    except Exception as e:
        print(f"  ✗ trade log read failed: {type(e).__name__}: {e}")
        sys.exit(1)

    matches.sort(key=lambda e: float(e.get("ts") or 0))
    if not matches:
        print(f"  (no events)")
    else:
        # Group by event_type
        from collections import Counter
        types = Counter(str(e.get("event_type") or "?") for e in matches)
        print(f"  {len(matches)} matching events. By type:")
        for t, c in types.most_common():
            print(f"    {c:>4}× {t}")
        # Show the 10 most recent
        print(f"\n  10 most recent:")
        for e in matches[-10:]:
            ts = float(e.get("ts") or 0)
            age = (time.time() - ts) / 60.0
            etype = e.get("event_type") or "?"
            sym = e.get("symbol") or e.get("product_id") or "-"
            sid = e.get("sleeve_id") or "-"
            print(f"    {age:6.1f}min ago  [{sym}] {etype}"
                  + (f"  sleeve={sid}" if sid != "-" else ""))

    print()
    print("=" * 90)


if __name__ == "__main__":
    main()
