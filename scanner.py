"""
scanner.py — periodic scanner of Coinbase derivatives (CFM futures) that
ranks products by both realized 24h volatility AND swing-frequency for a
user-configurable set of spreads.

Runs periodically from inside an existing bot process (paper worker is a
good choice — it already has an authenticated Coinbase client). Writes the
top-N ranking to Redis under a well-known key so the dashboard can render it
independently, no auth flow duplicated on the Node side.

Two metrics per product:
  vol_pct   = (high_24h - low_24h) / mid × 100 — amplitude of the day range
  best_score = max over candidate spreads of roundtrip_count × net_per_rt
               — amplitude AND frequency together, in expected $/day terms
Best-score picks the product where a spread that's easy to hit REPEATS
often enough to clear fees — the actual swing-trading target.
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


def compute_roundtrip_metric(prices: list[float], spread: float) -> tuple[int, float, float]:
    """Zig-zag swing detection: walk the price series, count reversals of
    amplitude >= spread. Returns (roundtrip_count, avg_swing_amp, max_swing_amp).

    A "swing leg" = a directional move that reverses by >= spread from its
    extreme. A "roundtrip" = 2 legs (one up + one down), the pattern a swing
    trader completes to pocket one gross spread of profit.
    """
    if len(prices) < 2 or spread <= 0:
        return (0, 0.0, 0.0)
    pivot = prices[0]
    extreme = pivot
    direction = 0  # 0=undecided, 1=up-leg in progress, -1=down-leg in progress
    swings: list[float] = []
    for p in prices[1:]:
        if direction == 0:
            if p >= pivot + spread:
                direction = 1
                extreme = p
            elif p <= pivot - spread:
                direction = -1
                extreme = p
            continue
        if direction == 1:
            if p > extreme:
                extreme = p
            elif p <= extreme - spread:
                swings.append(extreme - pivot)
                pivot = extreme
                extreme = p
                direction = -1
        else:
            if p < extreme:
                extreme = p
            elif p >= extreme + spread:
                swings.append(pivot - extreme)
                pivot = extreme
                extreme = p
                direction = 1
    roundtrips = len(swings) // 2
    if swings:
        avg = sum(swings) / len(swings)
        mx = max(swings)
    else:
        avg = 0.0
        mx = 0.0
    return (roundtrips, avg, mx)


def score_product_swings(
    prices: list[float],
    tick_size: float,
    contract_size: float,
    fee_per_contract_roundtrip: float,
    candidate_spread_mults: tuple[int, ...] = (3, 5, 10, 20, 50),
) -> dict:
    """For a price series, try a small grid of spread candidates (each a
    multiple of tick_size) and return the one with the highest expected $/day.

    Score per spread = roundtrip_count × max(0, spread * contract_size - fee_rt).
    Returns the best {spread, roundtrips, net_per_rt, score, avg_swing} plus
    a compact matrix of all candidates for the UI to expose.
    """
    if not prices or not tick_size or tick_size <= 0 or not contract_size or contract_size <= 0:
        return {"best_spread": None, "best_roundtrips": 0, "best_net_per_rt": 0.0,
                "best_score": 0.0, "best_avg_swing": 0.0, "candidates": []}
    fee_rt = float(fee_per_contract_roundtrip or 0.0)
    candidates = []
    best = None
    for mult in candidate_spread_mults:
        spread = round(mult * tick_size, 6)
        rt, avg, _mx = compute_roundtrip_metric(prices, spread)
        gross = spread * contract_size
        net = gross - fee_rt
        score = rt * max(0.0, net)
        entry = {
            "spread_mult": mult,
            "spread": spread,
            "roundtrips": rt,
            "net_per_rt": round(net, 4),
            "score": round(score, 4),
            "avg_swing": round(avg, 4),
        }
        candidates.append(entry)
        if best is None or score > best["score"]:
            best = entry
    return {
        "best_spread": best["spread"] if best else None,
        "best_spread_mult": best["spread_mult"] if best else None,
        "best_roundtrips": best["roundtrips"] if best else 0,
        "best_net_per_rt": best["net_per_rt"] if best else 0.0,
        "best_score": best["score"] if best else 0.0,
        "best_avg_swing": best["avg_swing"] if best else 0.0,
        "candidates": candidates,
    }


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


def _fetch_recent_closes(coinbase_client, product_id: str, granularity: str = "FIFTEEN_MINUTE",
                          lookback_secs: int = 24 * 3600) -> list[float]:
    """Fetch recent candle closes for a product. Returns [] on error rather
    than raising — swing-scoring for one product shouldn't break the whole scan.
    """
    try:
        end = int(time.time())
        start = end - lookback_secs
        resp = coinbase_client.get_candles(
            product_id=product_id,
            start=str(start), end=str(end),
            granularity=granularity,
        )
        d = resp.to_dict() if hasattr(resp, "to_dict") else resp
        raws = d.get("candles") or []
        # Coinbase returns descending; sort ascending for our walk.
        raws = sorted(raws, key=lambda r: float(r.get("start", 0)))
        closes = []
        for r in raws:
            c = r.get("close")
            if c is None:
                continue
            try:
                closes.append(float(c))
            except (TypeError, ValueError):
                continue
        return closes
    except Exception:
        return []


def fetch_and_rank(
    coinbase_client,
    top_n: int = 10,
    swing_fee_per_contract_roundtrip: float = 0.5,
    swing_lookback_secs: int = 24 * 3600,
    swing_granularity: str = "FIFTEEN_MINUTE",
) -> list[dict]:
    """Fetch all CFM futures from Coinbase, score each on both amplitude
    (24h range %) AND swing frequency (roundtrips per lookback window at a
    grid of spread candidates), return the top-N sorted by best_score
    (expected $/day at the best spread) with vol_pct as a secondary sort.

    Falls back to spot if futures listing is unavailable. Per-product candle
    fetch failures degrade to vol_pct-only scoring for that product.
    """
    products = []
    for product_type in ("FUTURE", "SPOT"):
        try:
            resp = coinbase_client.get_products(product_type=product_type)
            payload = resp.to_dict() if hasattr(resp, "to_dict") else resp
            got = payload.get("products") or []
            products.extend(got)
            if product_type == "FUTURE" and got:
                break
        except Exception:
            continue
    ranking = compute_ranking(products, top_n=max(top_n * 3, 30))
    for entry in ranking:
        pid = entry.get("product_id")
        tick = entry.get("tick_size")
        csize = entry.get("contract_size")
        if not pid or not tick or not csize:
            entry.update({"best_score": 0.0, "best_roundtrips": 0,
                          "best_spread": None, "best_net_per_rt": 0.0,
                          "swing_candidates": [], "swing_lookback_secs": swing_lookback_secs})
            continue
        closes = _fetch_recent_closes(coinbase_client, pid,
                                      granularity=swing_granularity,
                                      lookback_secs=swing_lookback_secs)
        swing = score_product_swings(closes, tick, csize, swing_fee_per_contract_roundtrip)
        entry.update({
            "best_score": swing["best_score"],
            "best_roundtrips": swing["best_roundtrips"],
            "best_spread": swing["best_spread"],
            "best_spread_mult": swing.get("best_spread_mult"),
            "best_net_per_rt": swing["best_net_per_rt"],
            "best_avg_swing": swing["best_avg_swing"],
            "swing_candidates": swing["candidates"],
            "swing_lookback_secs": swing_lookback_secs,
            "swing_bars": len(closes),
        })
        # Courtesy pause between candle calls — one product ~= one API request,
        # ~30 products/scan × 60s cadence = well under Coinbase's rate limit,
        # but a tiny sleep prevents burst spikes.
        time.sleep(0.03)
    # Rank by expected $/day at best spread; tie-break on 24h range.
    ranking.sort(key=lambda r: (-(r.get("best_score") or 0.0),
                                -(r.get("vol_pct") or 0.0),
                                -(r.get("volume_24h") or 0.0)))
    return ranking[:top_n]


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
