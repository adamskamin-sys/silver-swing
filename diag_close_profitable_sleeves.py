"""Flatten every sleeve currently in net unrealized profit — clean-slate
for Phase A bracket implementation to take effect on fresh cycles.

Adam 2026-07-21: "close all sleeves that currently have a net profit
and let's start fresh with the phase A implementations."

For each sleeve on adam-live currently holding a LONG position with
positive unrealized P&L:

  1. Cancel any resting SELL orders (both stop-limit AND limit — bracket
     legs from Phase A + any pre-Phase-A trail-breach residue).
  2. Place a MARKET SELL for the position quantity.
  3. Report the realized dollars taken.

Uses correct contract_size per broker.contract_spec (accounting for
the b09b46d contract_size bug that under-reported profits on
ENA/NER/ONDO/LINK/HYPE/AAVE/SLR).

DEFAULT: dry-run. Shows what WOULD be closed. Does not touch broker.
--apply flag: actually cancel resting orders + place market sells.

Read-only by default. Run:
    python3 diag_close_profitable_sleeves.py           # dry-run
    python3 diag_close_profitable_sleeves.py --apply   # execute
"""
from __future__ import annotations
import os
import sys
import json
from typing import Any


def _dump(o: Any) -> dict:
    if hasattr(o, "to_dict"):
        try:
            return o.to_dict()
        except Exception:
            pass
    if isinstance(o, dict):
        return o
    return {}


def _fmt_money(x: float, nd: int = 2) -> str:
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.{nd}f}"


def main() -> None:
    apply = "--apply" in sys.argv
    mode = "APPLY" if apply else "DRY-RUN"
    print("=" * 96)
    print(f"CLOSE-PROFITABLE-SLEEVES  ({mode})")
    print("=" * 96)
    if not apply:
        print("\n(DRY-RUN — nothing will be sent to Coinbase. Re-run with "
              "--apply to execute.)")

    import redis
    url = os.environ.get("REDIS_URL") or os.environ.get("REDIS_INTERNAL_URL")
    if not url:
        print("\n✗ REDIS_URL not set — must run on Render shell")
        return
    r = redis.Redis.from_url(url, decode_responses=True)
    store = json.loads(r.get("silver-swing:store") or "{}")

    tenant = "adam-live"
    tbody = store.get(tenant) or {}
    products = sorted([p for p in tbody.keys()
                       if not p.startswith("__") and isinstance(tbody.get(p), dict)])

    from broker import BrokerConfig, CoinbaseBroker

    total_realized = 0.0
    total_closed = 0
    total_skipped = 0
    total_at_loss = 0

    for pid in products:
        block = tbody[pid] or {}
        cfg = block.get("config") or {}
        state = block.get("state") or {}
        sleeves_cfg = cfg.get("sleeves") or []
        sleeves_state = state.get("sleeves") or {}

        # Product-level: get real position + mark + contract_size
        try:
            b = CoinbaseBroker(BrokerConfig(product_id=pid))
        except Exception as e:
            print(f"\n  {pid}: broker init failed: {e}")
            continue

        try:
            spec = b.contract_spec() if hasattr(b, "contract_spec") else {}
            contract_size = float((spec or {}).get("contract_size") or 1)
        except Exception:
            contract_size = 1.0

        # Position + mark
        try:
            positions = _dump(b.client.list_futures_positions()).get("positions") or []
            pos = next((p for p in positions if p.get("product_id") == pid), None)
        except Exception as e:
            print(f"\n  {pid}: list_futures_positions failed: {e}")
            continue
        if not pos:
            continue  # not holding a futures position; skip

        side = str(pos.get("side") or "").upper()
        pqty = int(float(pos.get("number_of_contracts") or 0))
        pavg = float(pos.get("avg_entry_price") or 0)
        if side != "LONG" or pqty <= 0 or pavg <= 0:
            continue

        # Fetch current mark for unrealized calc
        try:
            book = _dump(b.client.get_best_bid_ask(product_ids=[pid]))
            pricebooks = book.get("pricebooks") or []
            if pricebooks:
                pb = pricebooks[0]
                bid = float((pb.get("bids") or [{}])[0].get("price") or 0)
                ask = float((pb.get("asks") or [{}])[0].get("price") or 0)
                mark = (bid + ask) / 2.0 if bid and ask else max(bid, ask)
            else:
                mark = 0.0
        except Exception as e:
            print(f"\n  {pid}: mark fetch failed: {e}")
            mark = 0.0
        if mark <= 0:
            print(f"\n  {pid}: no mark available; skipping")
            total_skipped += 1
            continue

        unrealized = (mark - pavg) * contract_size * pqty

        # Estimate fees for the exit
        try:
            fee_rt = float(cfg.get("fee_per_contract_roundtrip") or 0)
        except (TypeError, ValueError):
            fee_rt = 0.0
        half_fee = (fee_rt / 2.0) * pqty if fee_rt > 0 else 0.0
        net_realized = unrealized - half_fee

        print(f"\n{'─' * 96}")
        print(f"{pid}")
        print("─" * 96)
        print(f"  position:      LONG {pqty} @ ${pavg:.6f}   contract_size={contract_size}")
        print(f"  mark:          ${mark:.6f}")
        print(f"  unrealized:    {_fmt_money(unrealized)}  (before fees)")
        print(f"  exit fee est:  {_fmt_money(half_fee)}")
        print(f"  NET REALIZED:  {_fmt_money(net_realized)}  "
              f"({'✓ PROFIT' if net_realized > 0 else '✗ LOSS'})")

        if net_realized <= 0:
            print(f"  → SKIP: at/below break-even; not closing per Adam's directive")
            total_at_loss += 1
            continue

        # List resting orders that need cancelling
        try:
            resp = b.client.list_orders(product_id=pid, order_status="OPEN", limit=100)
            open_orders = _dump(resp).get("orders") or []
        except Exception as e:
            print(f"  ✗ list_orders failed: {e}; SKIP")
            total_skipped += 1
            continue

        sell_orders = [o for o in open_orders
                       if str(o.get("side") or "").upper() == "SELL"]
        buy_orders = [o for o in open_orders
                      if str(o.get("side") or "").upper() == "BUY"]

        print(f"  open SELL orders: {len(sell_orders)}   open BUY orders: {len(buy_orders)}")
        for o in sell_orders:
            oid = str(o.get("order_id") or "")[:20]
            ocfg = o.get("order_configuration") or {}
            type_key = ""
            px = ""
            for k, v in (ocfg.items() if isinstance(ocfg, dict) else []):
                type_key = k
                if isinstance(v, dict):
                    px = str(v.get("stop_price") or v.get("limit_price") or "")
                    break
            print(f"    SELL  {oid}...  type={type_key}  px={px}")

        if not apply:
            print(f"  [DRY-RUN] Would cancel {len(sell_orders)} SELL orders + "
                  f"place MARKET SELL for {pqty} contracts")
            continue

        # === APPLY MODE ===
        print(f"  [APPLY] cancelling {len(sell_orders)} SELL orders...")
        for o in sell_orders:
            oid = o.get("order_id")
            if not oid:
                continue
            try:
                b.cancel(oid)
                print(f"    ✓ cancelled {str(oid)[:20]}...")
            except Exception as _ce:
                print(f"    ✗ cancel failed {str(oid)[:20]}...: {_ce}")

        print(f"  [APPLY] placing MARKET SELL for {pqty} contracts...")
        try:
            mkt_oid = b.place_market("SELL", pqty)
            print(f"    ✓ MARKET SELL placed: {mkt_oid}")
            total_closed += 1
            total_realized += net_realized
        except Exception as _me:
            print(f"    ✗ MARKET SELL FAILED: {_me}")
            total_skipped += 1

    print("\n" + "=" * 96)
    print("SUMMARY")
    print("=" * 96)
    print(f"  Mode:              {mode}")
    print(f"  Sleeves closed:    {total_closed}")
    print(f"  Sleeves at loss:   {total_at_loss}   (skipped per directive)")
    print(f"  Sleeves skipped:   {total_skipped}   (broker/mark error)")
    if apply and total_closed > 0:
        print(f"  Total realized:    {_fmt_money(total_realized)}  "
              "(approximate; actual = market fill prices)")
    if not apply:
        print(f"\n  To execute, re-run with --apply flag:")
        print(f"    python3 diag_close_profitable_sleeves.py --apply")


if __name__ == "__main__":
    main()
