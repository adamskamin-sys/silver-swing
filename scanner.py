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


# Per-product fee cache. Keyed by product_id → (per_fill_commission, ts).
# TTL 24h — Coinbase adjusts fee tiers rarely enough that a daily refresh is
# plenty, and avoids N preview calls per scan.
_FEE_CACHE: dict[str, tuple[float, float]] = {}
_FEE_TTL_SECS = 24 * 3600


def _fetch_per_fill_commission(coinbase_client, product_id: str) -> Optional[float]:
    """Preview a 1-contract SELL far above market so nothing can fill, read
    commission_total. Cache per product for 24h. Returns per-fill commission
    in USD, or None if the preview call fails (product ineligible, expired,
    auth quirk). Callers must fall back sanely on None."""
    cached = _FEE_CACHE.get(product_id)
    now = time.time()
    if cached and (now - cached[1]) < _FEE_TTL_SECS:
        return cached[0]
    try:
        preview = coinbase_client.preview_limit_order_gtc_sell(
            product_id=product_id, base_size="1", limit_price="999999.99",
        )
        pd = preview.to_dict() if hasattr(preview, "to_dict") else preview
        per_fill = float(pd.get("commission_total") or 0.0)
    except Exception:
        return cached[0] if cached else None
    if per_fill <= 0:
        return cached[0] if cached else None
    _FEE_CACHE[product_id] = (per_fill, now)
    return per_fill


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


# Coinbase's Advanced Trade candles endpoint caps at ~350 candles per call,
# so lookbacks that would exceed that at the chosen granularity have to page.
_GRANULARITY_SECS = {
    "ONE_MINUTE": 60,
    "FIVE_MINUTE": 300,
    "FIFTEEN_MINUTE": 900,
    "THIRTY_MINUTE": 1800,
    "ONE_HOUR": 3600,
    "TWO_HOUR": 7200,
    "SIX_HOUR": 21600,
    "ONE_DAY": 86400,
}


def _fetch_recent_closes(coinbase_client, product_id: str, granularity: str = "FIFTEEN_MINUTE",
                          lookback_secs: int = 24 * 3600) -> list[float]:
    """Fetch recent candle closes for a product. Pages transparently when the
    lookback would exceed Coinbase's ~350-candle-per-call cap so a caller can
    request 30 days at 1H without thinking about it. Returns [] on error
    rather than raising — swing-scoring for one product shouldn't break the
    whole scan.
    """
    try:
        per = _GRANULARITY_SECS.get(granularity, 900)
        page_seconds = per * 300  # stay comfortably under the 350 cap
        end = int(time.time())
        start = end - lookback_secs
        all_raws: list[dict] = []
        cursor = start
        while cursor < end:
            page_end = min(cursor + page_seconds, end)
            resp = coinbase_client.get_candles(
                product_id=product_id,
                start=str(cursor), end=str(page_end),
                granularity=granularity,
            )
            d = resp.to_dict() if hasattr(resp, "to_dict") else resp
            for r in (d.get("candles") or []):
                all_raws.append(r)
            cursor = page_end
            # Small pause between pages to spread API calls out.
            if cursor < end:
                time.sleep(0.02)
        # Coinbase returns descending; de-dup by start-ts and sort ascending.
        seen: set = set()
        raws: list[dict] = []
        for r in sorted(all_raws, key=lambda x: float(x.get("start", 0))):
            ts = float(r.get("start", 0))
            if ts in seen:
                continue
            seen.add(ts)
            raws.append(r)
        closes = []
        for r in raws:
            c = r.get("close")
            if c is None:
                continue
            try:
                closes.append(float(c))
            except (TypeError, ValueError):
                continue
        if not closes:
            print(f"[scanner] no candles returned for {product_id} @ {granularity} over {lookback_secs}s",
                  flush=True)
        return closes
    except Exception as e:
        print(f"[scanner] candle fetch failed for {product_id} @ {granularity}: "
              f"{type(e).__name__}: {e}", flush=True)
        return []


def fetch_and_rank(
    coinbase_client,
    top_n: int = 10,
    swing_fee_per_contract_roundtrip: float = 0.5,
    swing_lookback_secs: int = 24 * 3600,
    swing_granularity: str = "FIFTEEN_MINUTE",
    default_target_net_per_contract: float = 10.0,
    force_include: list[str] | None = None,
    spec_fallbacks: dict[str, dict] | None = None,
) -> list[dict]:
    """Fetch all CFM futures from Coinbase, score each on both amplitude
    (24h range %) AND swing frequency (roundtrips per lookback window at a
    grid of spread candidates), return the top-N sorted by best_score
    (expected $/day at the best spread) with vol_pct as a secondary sort.

    Falls back to spot if futures listing is unavailable. Per-product candle
    fetch failures degrade to vol_pct-only scoring for that product.

    force_include: product_ids that must appear in the output even if they
    rank below the top-N. Used to ensure every product Adam has an active
    strategy on always gets swing_candidates populated — otherwise the
    Edit modal's "Recommended spreads" tiles disappear for products that
    happen to have low 24h range that day.
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
    # Force-include any user-strategy product not in the natural ranking.
    # Same scoring logic (compute_ranking on the singleton product), then
    # merged in — top_n cap is honored by the natural ranking; forced
    # entries are appended past that cap so ranking stays complete.
    if force_include:
        ranked_ids = {e.get("product_id") for e in ranking}
        for pid in force_include:
            if not pid or pid in ranked_ids:
                continue
            # Find the raw Coinbase product dict and score it.
            raw = next((p for p in products if (p.get("product_id") or p.get("product_type_id")) == pid), None)
            if not raw:
                # Fallback: the initial get_products() call may have missed
                # this product (paginated, expired-looking to Coinbase's
                # filter, wrong product_type, etc.). Try a direct product
                # fetch so held/user-strategy products still get scanned
                # instead of silently dropping.
                try:
                    resp = coinbase_client.get_product(product_id=pid)
                    raw = resp.to_dict() if hasattr(resp, "to_dict") else resp
                except Exception as e:
                    print(f"[scanner] force_include: direct get_product failed for {pid}: "
                          f"{type(e).__name__}: {e}", flush=True)
                    continue
            forced = compute_ranking([raw], top_n=1)
            if forced:
                ranking.extend(forced)
                ranked_ids.add(pid)
            else:
                # compute_ranking's drop trigger is price is None/<=0 OR high/
                # low 24h missing. For low-volume nano futures (NOL, PT, etc.)
                # Coinbase's product dict often omits the 24h range fields.
                # Fall back: derive high/low from recent candles and build a
                # minimal entry so swing scoring below still runs.
                px = _f(raw.get("price")) or 0.0
                details = raw.get("future_product_details") or {}
                tick = _f(raw.get("price_increment"))
                csize = _f(details.get("contract_size"))
                # Store fallback: if Coinbase's raw response is missing spec
                # fields for this product (nano futures often lack contract
                # info until the product is more actively traded), pull from
                # the previously-refreshed store config. Better than dropping.
                fb = (spec_fallbacks or {}).get(pid) or {}
                if not tick:
                    tick = _f(fb.get("tick_size")) or 0.0
                if not csize:
                    csize = _f(fb.get("contract_size")) or 0.0
                salvage_high = None
                salvage_low = None
                try:
                    closes = _fetch_recent_closes(
                        coinbase_client, pid,
                        granularity="ONE_HOUR", lookback_secs=24 * 3600,
                    )
                    if closes:
                        salvage_high = max(closes)
                        salvage_low = min(closes)
                        if not px:
                            px = closes[-1]
                except Exception as e:
                    print(f"[scanner] force_include salvage candles failed for {pid}: "
                          f"{type(e).__name__}: {e}", flush=True)
                if px > 0 and tick and csize and salvage_high and salvage_low:
                    mid = (salvage_high + salvage_low) / 2 or px
                    rng = salvage_high - salvage_low
                    vol_pct = round((rng / mid) * 100, 3) if mid > 0 else 0.0
                    ranking.append({
                        "product_id": pid,
                        "price": px,
                        "high_24h": salvage_high,
                        "low_24h": salvage_low,
                        "vol_pct": vol_pct,
                        "volume_24h": _f(raw.get("approximate_quote_24h_volume")) or 0,
                        "tick_size": tick,
                        "contract_size": csize,
                        "tick_value": tick * csize,
                        "contract_expiry": details.get("contract_expiry"),
                        "intraday_margin_rate": _f(details.get("intraday_margin_rate")),
                        "overnight_margin_rate": _f(details.get("overnight_margin_rate")),
                    })
                    ranked_ids.add(pid)
                    print(f"[scanner] force_include: SALVAGED {pid} via candles "
                          f"(high={salvage_high}, low={salvage_low}, tick={tick}, "
                          f"csize={csize})", flush=True)
                else:
                    print(f"[scanner] force_include: compute_ranking dropped {pid} — "
                          f"price={px}, high={raw.get('high_24_h') or raw.get('high_24h')}, "
                          f"low={raw.get('low_24_h') or raw.get('low_24h')}, "
                          f"tick={tick}, csize={csize}, "
                          f"salvage_high={salvage_high}, salvage_low={salvage_low}",
                          flush=True)
    for entry in ranking:
        pid = entry.get("product_id")
        tick = entry.get("tick_size")
        csize = entry.get("contract_size")
        if not pid or not tick or not csize:
            print(f"[scanner] {pid}: skipping swing scoring — missing tick={tick} "
                  f"or contract_size={csize} (modal will show 'no scanner data')",
                  flush=True)
            entry.update({"best_score": 0.0, "best_roundtrips": 0,
                          "best_spread": None, "best_net_per_rt": 0.0,
                          "swing_candidates": [], "swing_lookback_secs": swing_lookback_secs})
            continue
        closes = _fetch_recent_closes(coinbase_client, pid,
                                      granularity=swing_granularity,
                                      lookback_secs=swing_lookback_secs)
        # Weekend / closed-market fallback: CFM futures are shut Fri 5pm ET
        # → Sun 6pm ET, so a 24h/15-min pull returns 0 bars. Fall back to
        # 1H candles over 7 days so the sleeve editor still gets BEST tiles.
        # Normalize roundtrip counts to per-day (÷7) so the UI's daily×7 /
        # daily×30 weekly/monthly extrapolation stays honest.
        fallback_used = False
        if not closes:
            closes = _fetch_recent_closes(coinbase_client, pid,
                                          granularity="ONE_HOUR",
                                          lookback_secs=7 * 24 * 3600)
            fallback_used = bool(closes)
        # Use the REAL per-product round-trip fee for scoring — otherwise the
        # hardcoded $0.50 default overstates net for anything with expensive
        # fills (XLP: $1.42/RT). Adam clicked BEST expecting "$2 net" and got
        # $1.08 real because the tile lied. Fetch once here, use for both
        # scoring and the modal's fee callout below.
        per_fill_fee = _fetch_per_fill_commission(coinbase_client, pid)
        effective_fee_rt = (per_fill_fee * 2) if (per_fill_fee and per_fill_fee > 0) \
            else float(swing_fee_per_contract_roundtrip or 0.0)
        swing = score_product_swings(closes, tick, csize, effective_fee_rt)
        if fallback_used and swing.get("candidates"):
            fee_rt = effective_fee_rt
            for c in swing["candidates"]:
                rt7 = int(c.get("roundtrips", 0) or 0)
                rt_daily = max(1, round(rt7 / 7.0)) if rt7 > 0 else 0
                c["roundtrips"] = rt_daily
                net = float(c.get("net_per_rt", 0.0) or 0.0)
                c["score"] = round(rt_daily * max(0.0, net), 4)
            best = max(swing["candidates"], key=lambda c: c.get("score", 0.0), default=None)
            if best:
                swing["best_spread"] = best["spread"]
                swing["best_spread_mult"] = best.get("spread_mult")
                swing["best_roundtrips"] = best["roundtrips"]
                swing["best_net_per_rt"] = best["net_per_rt"]
                swing["best_score"] = best["score"]
                swing["best_avg_swing"] = best.get("avg_swing", 0.0)
        # Real-data weekly + monthly: fetch 7d and 30d of hourly candles
        # per product and count roundtrips at the same best spread. Costs a
        # few extra API calls per product per scan but avoids the naive
        # "daily × 7 / × 30" extrapolation which assumes today is a
        # representative day (it usually isn't).
        best_spread_for_periods = float(swing["best_spread"] or 0.0)
        weekly_rt = 0
        weekly_score_val = 0.0
        monthly_rt = 0
        monthly_score_val = 0.0
        if best_spread_for_periods > 0:
            weekly_closes = _fetch_recent_closes(
                coinbase_client, pid,
                granularity="ONE_HOUR", lookback_secs=7 * 24 * 3600,
            )
            time.sleep(0.03)
            monthly_closes = _fetch_recent_closes(
                coinbase_client, pid,
                granularity="ONE_HOUR", lookback_secs=30 * 24 * 3600,
            )
            time.sleep(0.03)
            gross_per_rt = best_spread_for_periods * csize
            net_per_rt = gross_per_rt - effective_fee_rt
            if weekly_closes:
                weekly_rt, _, _ = compute_roundtrip_metric(weekly_closes, best_spread_for_periods)
                weekly_score_val = max(0.0, weekly_rt * net_per_rt)
            if monthly_closes:
                monthly_rt, _, _ = compute_roundtrip_metric(monthly_closes, best_spread_for_periods)
                monthly_score_val = max(0.0, monthly_rt * net_per_rt)
        # "Cycles at your defaults" — the spread that would net Adam's
        # configured target ($10/contract by default). Solves:
        #   spread × contract_size − fee_rt = target
        # for spread, then counts roundtrips at that exact spread. Lets the
        # user compare "amplitude score" (best possible) vs "cycles at what
        # I'd actually set" (what my presets would have caught).
        target = float(default_target_net_per_contract or 0.0)
        if target > 0 and csize > 0:
            default_spread = (target + effective_fee_rt) / csize
            # Snap up to a whole number of ticks so the spread is achievable.
            if tick and tick > 0:
                default_spread = max(tick, round(default_spread / tick) * tick)
            default_rt, _, _ = compute_roundtrip_metric(closes, default_spread)
        else:
            default_spread = 0.0
            default_rt = 0
        # Real weekly / monthly scores (computed above from actual 7d/30d
        # of 1H candles). Also compute default-preset weekly/monthly with
        # the same real data at the target-net spread.
        weekly_default_score = 0.0
        monthly_default_score = 0.0
        if default_spread > 0 and csize > 0:
            gross_per_default_rt = default_spread * csize
            net_per_default_rt = gross_per_default_rt - effective_fee_rt
            if best_spread_for_periods > 0:
                # Reuse the same weekly/monthly closes we already fetched.
                if weekly_score_val > 0 or weekly_rt > 0:
                    weekly_default_rt, _, _ = compute_roundtrip_metric(weekly_closes, default_spread)
                    weekly_default_score = max(0.0, weekly_default_rt * net_per_default_rt)
                if monthly_score_val > 0 or monthly_rt > 0:
                    monthly_default_rt, _, _ = compute_roundtrip_metric(monthly_closes, default_spread)
                    monthly_default_score = max(0.0, monthly_default_rt * net_per_default_rt)
        weekly_score = weekly_score_val
        monthly_score = monthly_score_val
        # per_fill_fee already fetched above (used for effective_fee_rt so the
        # score/net numbers in the tiles match reality). Cached, so no extra
        # API call here.
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
            "default_spread": round(default_spread, 6),
            "default_target_net_per_contract": target,
            "default_roundtrips": default_rt,
            # Real (not extrapolated) — actual 7d / 30d roundtrip counts
            # at the same best_spread as the daily score.
            "weekly_score": round(weekly_score, 2),
            "weekly_roundtrips": weekly_rt,
            "monthly_score": round(monthly_score, 2),
            "monthly_roundtrips": monthly_rt,
            "weekly_default_score": round(weekly_default_score, 2),
            "monthly_default_score": round(monthly_default_score, 2),
            "fee_per_fill": round(per_fill_fee, 4) if per_fill_fee else None,
            "fee_per_contract_roundtrip": round(per_fill_fee * 2, 4) if per_fill_fee else None,
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
REFRESH_KEY = "silver-swing:scanner:refresh_requested"


def request_refresh(url: str, ttl_secs: int = 300) -> None:
    """Set a Redis flag telling the paper worker to run one scan on its next
    loop iteration. Called by the dashboard when the user opens the scanner
    tab so we don't burn API budget when nobody's looking. TTL prevents a
    stale flag from firing scans indefinitely after a worker restart."""
    import redis
    r = redis.Redis.from_url(url, decode_responses=True)
    r.set(REFRESH_KEY, str(int(time.time())), ex=ttl_secs)


def check_and_clear_refresh_request(url: str) -> bool:
    """Atomically check + delete the refresh flag. Returns True if a refresh
    was requested since the last check (i.e., worker should run one scan)."""
    import redis
    r = redis.Redis.from_url(url, decode_responses=True)
    val = r.get(REFRESH_KEY)
    if val is None:
        return False
    r.delete(REFRESH_KEY)
    return True


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
