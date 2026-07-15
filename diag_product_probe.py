"""Probe products' Coinbase side: contract_spec + latest ticker.

Adam 2026-07-15: AVE, HYF, NGS spawn successfully but their Tracks
immediately zombie because feeds produce no tickers. Probe each:
  * Does contract_spec succeed? (product exists on Coinbase)
  * Does session_open=True? (product currently tradeable)
  * Can we get a ticker snapshot? (feed subscription works)
  * Days-to-expiry (near-expiry products may have quiet feeds)

Read-only. Usage:
    python3 diag_product_probe.py PRODUCT_ID
    python3 diag_product_probe.py AVE-20DEC30-CDE HYF-31JUL26-CDE NGS-28JUL26-CDE
"""
from __future__ import annotations
import os
import sys
import time


def probe(pid: str) -> None:
    print(f"\n─── {pid} ───")
    try:
        from broker import BrokerConfig, CoinbaseBroker
        b = CoinbaseBroker(BrokerConfig(product_id=pid))
    except Exception as e:
        print(f"  ✗ broker construction failed: {type(e).__name__}: {e}")
        return

    # contract_spec
    try:
        spec = b.contract_spec()
        print(f"  contract_spec: OK ({len(spec)} fields)")
        print(f"    tick_size={spec.get('tick_size')}")
        print(f"    contract_size={spec.get('contract_size')}")
        print(f"    session_open={spec.get('session_open')}")
        print(f"    contract_expiry={spec.get('contract_expiry')}")
        expiry = spec.get('contract_expiry')
        if expiry:
            try:
                from datetime import datetime, timezone
                s = str(expiry).replace("Z", "+00:00")
                dt = datetime.fromisoformat(s)
                days = (dt - datetime.now(timezone.utc)).days
                print(f"    days_to_expiry={days} {'← NEAR EXPIRY' if days <= 5 else ''}")
            except Exception:
                pass
    except Exception as e:
        print(f"  ✗ contract_spec FAILED: {type(e).__name__}: {e}")
        print(f"    → product may be delisted or product_id wrong")
        return

    # position query
    try:
        pos = b.position_qty()
        print(f"  position_qty: {pos}")
    except Exception as e:
        print(f"  ✗ position_qty failed: {type(e).__name__}: {e}")

    # Live feed test — subscribe + wait for one tick
    try:
        from feed import LiveTickerFeed
        feed = LiveTickerFeed(pid)
        feed.start()
        print(f"  feed.start() OK — waiting up to 10s for first ticker...")
        got = None
        for _ in range(20):
            t = feed.latest_ticker()
            if t is not None:
                got = t
                break
            time.sleep(0.5)
        try:
            feed.stop()
        except Exception:
            pass
        if got:
            print(f"  ✓ ticker received: price={got.get('price')} ts={got.get('time')}")
        else:
            print(f"  ✗ NO ticker received in 10s")
            print(f"    → feed subscribed but Coinbase isn't publishing tickers")
            print(f"    → product may be inactive / no market maker / quiet spot")
    except Exception as e:
        print(f"  ✗ feed test failed: {type(e).__name__}: {e}")


def main() -> None:
    if len(sys.argv) < 2:
        print("USAGE: python3 diag_product_probe.py PRODUCT_ID [PRODUCT_ID ...]")
        return
    print("=" * 100)
    print(f"PRODUCT PROBE — {len(sys.argv) - 1} product(s)")
    print("=" * 100)
    for pid in sys.argv[1:]:
        probe(pid)
    print("=" * 100)


if __name__ == "__main__":
    main()
