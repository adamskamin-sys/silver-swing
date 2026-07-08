"""
scanner.py — periodic scanner of Coinbase derivatives (CFM futures) that
ranks products by realized 24h volatility.

Runs periodically from inside an existing bot process (paper worker is a
good choice — it already has an authenticated Coinbase client). Writes the
top-N ranking to Redis under a well-known key so the dashboard can render it
independently, no auth flow duplicated on the Node side.

Volatility metric = (high_24h - low_24h) / mid × 100 — the day range as a
percentage of mid. Simple, robust, and pre-computed by Coinbase in every
product record (no need to fetch candles or build a rolling window).
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def compute_ranking(products: list[dict], top_n: int = 10) -> list[dict]:
    """Given a list of Coinbase product dicts, return the top-N ranked by
    24h range %. Each entry has product_id, price, high, low, vol_pct, and
    volume_24h_usd (for tiebreak transparency).
    """
    scored = []
    for p in products:
        pid = p.get("product_id") or p.get("product_type_id")
        if not pid:
            continue
        price = _f(p.get("price"))
        high = _f(p.get("price_percentage_change_24h_high")) or _f(p.get("high_24_h")) or _f(p.get("high_24h"))
        low = _f(p.get("low_24_h")) or _f(p.get("low_24h"))
        if price is None or high is None or low is None or price <= 0:
            continue
        mid = (high + low) / 2 if (high > 0 and low > 0) else price
        if mid <= 0:
            continue
        rng = high - low
        vol_pct = (rng / mid) * 100
        vol_24h_usd = _f(p.get("approximate_quote_24h_volume")) or _f(p.get("volume_24h")) or 0
        # Contract specs — piggyback on what get_products returns so the
        # scanner-detail modal doesn't have to make another Coinbase call to
        # show tick_size / contract_size / margin / expiry.
        details = p.get("future_product_details") or {}
        tick = _f(p.get("price_increment"))
        contract_size = _f(details.get("contract_size"))
        contract_expiry = details.get("contract_expiry")
        intraday_margin = _f(details.get("intraday_margin_rate"))
        overnight_margin = _f(details.get("overnight_margin_rate"))
        scored.append({
            "product_id": pid,
            "price": price,
            "high_24h": high,
            "low_24h": low,
            "vol_pct": round(vol_pct, 3),
            "volume_24h": vol_24h_usd,
            "tick_size": tick,
            "contract_size": contract_size,
            "tick_value": (tick * contract_size) if (tick and contract_size) else None,
            "contract_expiry": contract_expiry,
            "intraday_margin_rate": intraday_margin,
            "overnight_margin_rate": overnight_margin,
        })
    scored.sort(key=lambda r: (-r["vol_pct"], -r["volume_24h"]))
    return scored[:top_n]


def fetch_and_rank(coinbase_client, top_n: int = 10) -> list[dict]:
    """Fetch all CFM futures from Coinbase and return top-N by 24h range %.
    Falls back to spot if futures listing is unavailable.
    """
    products = []
    for product_type in ("FUTURE", "SPOT"):
        try:
            resp = coinbase_client.get_products(product_type=product_type)
            payload = resp.to_dict() if hasattr(resp, "to_dict") else resp
            got = payload.get("products") or []
            products.extend(got)
            if product_type == "FUTURE" and got:
                # We only care about spot if futures returned nothing — but
                # keep the CFM futures first as they're the intended target.
                break
        except Exception:
            continue
    return compute_ranking(products, top_n=top_n)


REDIS_KEY = "silver-swing:scanner"


def write_ranking_to_redis(url: str, ranking: list[dict], generated_at: Optional[float] = None) -> None:
    """Publish the ranking under REDIS_KEY. Dashboard reads from here."""
    import redis
    r = redis.Redis.from_url(url, decode_responses=True)
    payload = {
        "generated_at": generated_at if generated_at is not None else time.time(),
        "top": ranking,
    }
    r.set(REDIS_KEY, json.dumps(payload))


def read_ranking_from_redis(url: str) -> Optional[dict]:
    import redis
    r = redis.Redis.from_url(url, decode_responses=True)
    raw = r.get(REDIS_KEY)
    return json.loads(raw) if raw else None
