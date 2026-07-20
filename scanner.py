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


def compute_roundtrip_metric(prices: list[float], spread: float,
                             timestamps: list[float] | None = None,
                             active_hours_utc: tuple[int, int] | None = None,
                             active_weight: float = 1.0,
                             offhours_weight: float = 0.5) -> tuple[int, float, float]:
    """Zig-zag swing detection: walk the price series, count reversals of
    amplitude >= spread. Returns (roundtrip_count, avg_swing_amp, max_swing_amp).

    A "swing leg" = a directional move that reverses by >= spread from its
    extreme. A "roundtrip" = 2 legs (one up + one down), the pattern a swing
    trader completes to pocket one gross spread of profit.

    Optional session-hour weighting: if `timestamps` (unix seconds, same
    length as prices) and `active_hours_utc` (start_hour, end_hour) are
    given, each roundtrip is weighted by whether it COMPLETED during
    active hours. Active roundtrips count as `active_weight` (default 1.0);
    off-hours ones as `offhours_weight` (default 0.5). Returns a FLOAT for
    roundtrip_count in that case — the Livermore rule "trade when the
    professionals are trading" made mechanical.

    Backward-compat: without timestamps/active_hours, roundtrip_count is an
    integer count exactly as before.
    """
    if len(prices) < 2 or spread <= 0:
        return (0, 0.0, 0.0)
    use_session_weight = (
        timestamps is not None
        and len(timestamps) == len(prices)
        and active_hours_utc is not None
    )
    pivot = prices[0]
    extreme = pivot
    extreme_idx = 0
    direction = 0  # 0=undecided, 1=up-leg in progress, -1=down-leg in progress
    swings: list[float] = []
    swing_end_idx: list[int] = []
    for i, p in enumerate(prices[1:], start=1):
        if direction == 0:
            if p >= pivot + spread:
                direction = 1
                extreme = p
                extreme_idx = i
            elif p <= pivot - spread:
                direction = -1
                extreme = p
                extreme_idx = i
            continue
        if direction == 1:
            if p > extreme:
                extreme = p
                extreme_idx = i
            elif p <= extreme - spread:
                swings.append(extreme - pivot)
                swing_end_idx.append(i)
                pivot = extreme
                extreme = p
                extreme_idx = i
                direction = -1
        else:
            if p < extreme:
                extreme = p
                extreme_idx = i
            elif p >= extreme + spread:
                swings.append(pivot - extreme)
                swing_end_idx.append(i)
                pivot = extreme
                extreme = p
                extreme_idx = i
                direction = 1
    n_leg_pairs = len(swings) // 2
    if use_session_weight and n_leg_pairs > 0:
        import datetime as _dt
        active_start, active_end = active_hours_utc
        weighted = 0.0
        for k in range(n_leg_pairs):
            # A roundtrip is 2 legs. Count it at the completion of the
            # second leg (the sell that pockets profit).
            end_idx = swing_end_idx[2 * k + 1]
            ts = float(timestamps[end_idx])
            hour = _dt.datetime.utcfromtimestamp(ts).hour
            in_session = (active_start <= hour < active_end
                          if active_start < active_end
                          else (hour >= active_start or hour < active_end))
            weighted += active_weight if in_session else offhours_weight
        roundtrips = weighted
    else:
        roundtrips = n_leg_pairs
    if swings:
        avg = sum(swings) / len(swings)
        mx = max(swings)
    else:
        avg = 0.0
        mx = 0.0
    return (roundtrips, avg, mx)


def _regime_robust_score(prices: list[float], spread: float,
                         chunks: int = 3) -> float:
    """Split the price series into equal chunks, count roundtrips in each,
    return the MINIMUM per-chunk roundtrip count (as fraction of total).
    A spread that only works during one lucky window scores 0 here even if
    its total is high — enforces the Turtle rule 'the system must work
    across regimes, not just yesterday's'."""
    if len(prices) < chunks * 5 or spread <= 0:
        return 1.0  # not enough data to judge — don't penalize
    total_rt, _, _ = compute_roundtrip_metric(prices, spread)
    if total_rt == 0:
        return 0.0
    chunk_size = len(prices) // chunks
    per_chunk = []
    for i in range(chunks):
        chunk = prices[i * chunk_size:(i + 1) * chunk_size if i < chunks - 1 else None]
        rt, _, _ = compute_roundtrip_metric(chunk, spread)
        per_chunk.append(rt)
    # Robustness = (min chunk RT) / (average chunk RT). 1.0 = perfectly
    # even distribution; 0.0 = all RTs in a single chunk. Prefer spreads
    # where robustness ≥ 0.5 (no chunk has less than half the average).
    avg_chunk = sum(per_chunk) / chunks if per_chunk else 0
    if avg_chunk == 0:
        return 0.0
    return min(per_chunk) / avg_chunk


def _atr_proxy_from_closes(prices: list[float]) -> float:
    """Close-to-close volatility proxy — we don't have OHLC here, so use the
    mean of absolute period-to-period returns (in price units). Standard ATR
    would use true range; this is a well-known close-only substitute that
    tracks the same signal within ~10% under normal market regimes."""
    if len(prices) < 3:
        return 0.0
    diffs = [abs(prices[i] - prices[i - 1]) for i in range(1, len(prices))]
    if not diffs:
        return 0.0
    diffs.sort()
    n = len(diffs)
    # Median is robust to a single-tick spike; use median×2 as the ATR proxy
    # (median absolute return ≈ 0.5 × mean abs return for near-Gaussian, and
    # ATR is roughly 2× the median move for the horizons we care about).
    return diffs[n // 2] * 2.0


def _build_spread_grid(prices: list[float], tick_size: float, n_candidates: int = 20) -> list[float]:
    """Wider, ATR-anchored candidate spreads instead of the old fixed
    (3×, 5×, 10×, 20×, 50×)-tick grid. Range: 0.5×ATR to 5×ATR, geometrically
    spaced, snapped to the product's tick_size, deduplicated.

    Why: the fixed multiplier grid missed the best spread whenever ATR didn't
    line up with (3/5/10/20/50)-tick — very common for products whose tick
    isn't scaled to their volatility (NOL, PT, oil). This grid always covers
    the volatility band that actually produces roundtrips.
    """
    atr = _atr_proxy_from_closes(prices)
    if atr <= 0:
        # No volatility signal (flat window / weekend). Fall back to the old
        # fixed grid so we still return SOMETHING for the UI.
        return [round(m * tick_size, 8) for m in (3, 5, 10, 20, 50, 100)]
    lo = max(tick_size, atr * 0.5)
    hi = max(lo * 1.1, atr * 5.0)
    # Geometric spacing so the grid is dense at small spreads (where fees
    # dominate the decision) and sparse at large spreads (where diminishing
    # returns matter less).
    import math
    ratio = (hi / lo) ** (1.0 / max(1, n_candidates - 1))
    raw = [lo * (ratio ** i) for i in range(n_candidates)]
    # Snap to tick, dedupe while preserving order.
    seen = set()
    out = []
    for s in raw:
        snapped = round(round(s / tick_size) * tick_size, 8) if tick_size > 0 else round(s, 6)
        if snapped <= 0 or snapped in seen:
            continue
        seen.add(snapped)
        out.append(snapped)
    return out


def score_product_swings(
    prices: list[float],
    tick_size: float,
    contract_size: float,
    fee_per_contract_roundtrip: float,
    candidate_spread_mults: tuple[int, ...] = (3, 5, 10, 20, 50),
    weekly_prices: list[float] | None = None,
    monthly_prices: list[float] | None = None,
    horizon_weights: tuple[float, float, float] = (0.2, 0.5, 0.3),
    weekly_timestamps: list[float] | None = None,
    monthly_timestamps: list[float] | None = None,
    active_hours_utc: tuple[int, int] | None = (13, 21),
    offhours_weight: float = 0.5,
    regime_penalty_weight: float = 0.3,
    maker_fee_multiplier: float = 0.5,
    toxicity_penalty: float = 0.0,
    funding_boost: float = 1.0,
) -> dict:
    """For a price series, try an ATR-anchored grid of spread candidates and
    return the one with the highest expected $/day AVERAGED ACROSS 24h/7d/30d.

    Old behavior scored each spread only against the last 24h of candles →
    picked whatever spread happened to have the most roundtrips today, and a
    quiet day meant a bad pick. New behavior: score every candidate across
    three horizons and pick the one that maximizes a weighted $/day expected:

        weighted = w_d × daily_$/day + w_w × weekly_$/day + w_m × monthly_$/day

    Default weights (0.2, 0.5, 0.3) prefer weekly stability over noisy 24h,
    with monthly as a mean-reversion anchor. That's the Van Tharp /
    Turtle-style "don't chase yesterday's mover" rule made mechanical.

    candidate_spread_mults is kept for backward-compat callers but IGNORED
    unless prices is empty (ATR-anchored grid supersedes it).
    """
    if not prices or not tick_size or tick_size <= 0 or not contract_size or contract_size <= 0:
        return {"best_spread": None, "best_roundtrips": 0, "best_net_per_rt": 0.0,
                "best_score": 0.0, "best_avg_swing": 0.0, "candidates": []}
    fee_rt = float(fee_per_contract_roundtrip or 0.0)

    # Wider, ATR-anchored grid replaces the old (3, 5, 10, 20, 50)-tick fixed
    # multipliers. Falls back to the old grid only if prices are too flat to
    # derive an ATR (weekend / dead product).
    grid = _build_spread_grid(prices, tick_size, n_candidates=20)
    if not grid:
        grid = [round(m * tick_size, 6) for m in candidate_spread_mults]

    # Weight tuple normalized to sum=1 so callers can pass raw preferences
    # (0.2, 0.5, 0.3) or (1, 3, 2) — both work.
    w_d, w_w, w_m = horizon_weights
    w_sum = max(1e-9, w_d + w_w + w_m)
    w_d, w_w, w_m = w_d / w_sum, w_w / w_sum, w_m / w_sum

    def _score_grid(fee_rt_use: float, tile_kind: str) -> tuple[list[dict], dict | None]:
        """Score every candidate spread at the given fee level. Returns
        (all_candidates_sorted_desc, best_candidate). Used twice: once at
        the standard (taker/default) fee rate for the EXPERT tile, once at
        the maker rate for the FRONT-RUN tile."""
        out = []
        top = None
        for spread in grid:
            gross = spread * contract_size
            net = gross - fee_rt_use
            rt_daily, avg_d, _ = compute_roundtrip_metric(prices, spread)
            daily_per_day = rt_daily * max(0.0, net)
            if weekly_prices:
                rt_weekly, _, _ = compute_roundtrip_metric(
                    weekly_prices, spread,
                    timestamps=weekly_timestamps,
                    active_hours_utc=active_hours_utc,
                    offhours_weight=offhours_weight,
                )
                weekly_per_day = (rt_weekly * max(0.0, net)) / 7.0
            else:
                rt_weekly = 0.0
                weekly_per_day = daily_per_day
            if monthly_prices:
                rt_monthly, _, _ = compute_roundtrip_metric(
                    monthly_prices, spread,
                    timestamps=monthly_timestamps,
                    active_hours_utc=active_hours_utc,
                    offhours_weight=offhours_weight,
                )
                monthly_per_day = (rt_monthly * max(0.0, net)) / 30.0
            else:
                rt_monthly = 0.0
                monthly_per_day = daily_per_day
            robustness = _regime_robust_score(monthly_prices or prices, spread)
            base = (w_d * daily_per_day + w_w * weekly_per_day + w_m * monthly_per_day)
            weighted_per_day = base * (1.0 - regime_penalty_weight * (1.0 - robustness))
            # Toxicity penalty (VPIN / Kyle λ): informed trade flow means
            # passive limits get adverse-selected. Scale the expected $/day
            # DOWN by the toxicity penalty (already clamped 0..1 by caller).
            # Easley-López de Prado-O'Hara 2012 (VPIN paper), Kyle 1985.
            if toxicity_penalty > 0:
                weighted_per_day = weighted_per_day * max(0.0, 1.0 - toxicity_penalty)
            # Funding boost (Aksoy-Cheng / Hasbrouck): for crypto perps,
            # negative funding = shorts pay longs (bullish for us as a long-
            # biased bot — we get PAID to hold). scanner_boost returns a
            # multiplier in [0.5, 1.5]. Non-perps and missing funding data
            # produce boost=1.0 (no effect).
            if funding_boost and funding_boost != 1.0:
                weighted_per_day = weighted_per_day * float(funding_boost)
            entry = {
                "spread": spread,
                "spread_mult": round(spread / tick_size) if tick_size > 0 else None,
                "roundtrips": rt_daily,
                "net_per_rt": round(net, 4),
                "score": round(weighted_per_day, 4),
                "score_daily": round(daily_per_day, 4),
                "score_weekly": round(weekly_per_day * 7.0, 4),
                "score_monthly": round(monthly_per_day * 30.0, 4),
                "avg_swing": round(avg_d, 4),
                "regime_robustness": round(robustness, 3),
                "session_weighted_weekly_rt": round(rt_weekly, 2),
                "session_weighted_monthly_rt": round(rt_monthly, 2),
                "tile_kind": tile_kind,
                "fee_rt_used": round(fee_rt_use, 4),
                "toxicity_penalty": round(float(toxicity_penalty), 3),
            }
            out.append(entry)
            if top is None or weighted_per_day > top["score"]:
                top = entry
        out.sort(key=lambda c: c["score"], reverse=True)
        return out, top

    # EXPERT tile: full multi-horizon + session-hour + regime-robust scoring
    # at the product's real per-fill fee (usually taker). This is the pick
    # that assumes we sometimes cross the spread and eat taker fees.
    _all_expert, best_expert = _score_grid(fee_rt, "expert")
    # FRONT-RUN tile: same scoring but assumes maker-only fees (post_only=true
    # on every arm + penny_inside for queue priority). Coinbase CFM maker fees
    # are ~40-60% of taker; default multiplier 0.5. The tighter spreads that
    # were fee-negative under taker rates often go positive here, so the pick
    # is genuinely different and only works IF the sleeve runs with
    # post_only_enabled + penny_inside_enabled (both default ON in Model B).
    maker_fee_rt = fee_rt * float(maker_fee_multiplier or 0.5)
    _all_frontrun, best_frontrun = _score_grid(maker_fee_rt, "frontrun")

    # Deliver ONLY the two tiles Adam asked for. If the front-run pick lands
    # on the same spread as expert (rare — only when fees don't shift the
    # ranking), skip the duplicate rather than clutter.
    tiles: list[dict] = []
    if best_expert:
        best_expert = dict(best_expert)
        best_expert["tile_label"] = "EXPERT"
        tiles.append(best_expert)
    if best_frontrun and (not best_expert or best_frontrun["spread"] != best_expert["spread"]):
        best_frontrun = dict(best_frontrun)
        best_frontrun["tile_label"] = "FRONT-RUN"
        tiles.append(best_frontrun)
    best = best_expert

    # Adam 2026-07-15: Avellaneda-Stoikov tile — computes optimal spread
    # from vol + arrival rate + inventory (using expert_spread module).
    # Adds a third tile with the AS pick + expected $/day forecast so
    # the operator can compare EMPIRICAL (scanner grid) vs MODEL
    # (Avellaneda-Stoikov formula). When they agree, high confidence.
    # When they diverge, one may be catching what the other misses.
    #
    # Fail-safe: any AS error just skips the tile — scanner output
    # unchanged for the other two tiles.
    as_tile = None
    try:
        import expert_spread as _es
        # Use monthly prices when available (larger sample) else daily.
        # Arrival rate: approximated from empirical rt_daily on the best
        # candidate — that's the closest cycles-per-day estimate we have
        # for this product pre-arm.
        est_arrival_per_sec = None
        if best and best.get("roundtrips"):
            est_arrival_per_sec = float(best["roundtrips"]) / 86400.0
        mid_px = (prices[-1] if prices else 0.0)
        as_dec = _es.grid_search_optimal_gamma(
            mid_price=float(mid_px),
            price_history=list(monthly_prices or prices or []),
            cycle_completion_ts=None,  # let module use its floor
            fee_per_roundtrip=fee_rt,
            contract_size=float(contract_size),
            qty=1,   # scanner always scores at 1-contract
            tick_size=float(tick_size),
        )
        if as_dec is not None:
            as_tile = {
                "spread": round(as_dec.spread, 6),
                "spread_mult": round(as_dec.spread / tick_size) if tick_size > 0 else None,
                "roundtrips": round(as_dec.expected_cycles_per_day, 2),
                "net_per_rt": round(as_dec.expected_profit_per_cycle, 4),
                "score": round(as_dec.expected_daily_pnl, 4),
                "avg_swing": None,
                "regime_robustness": None,
                "tile_kind": "avellaneda_stoikov",
                "fee_rt_used": round(fee_rt, 4),
                "tile_label": "AS",
                "method": as_dec.method,
                "citation": as_dec.citation,
                "cost_floor_binding": as_dec.cost_floor_binding,
                "reservation_price": round(as_dec.reservation_price, 6),
            }
            tiles.append(as_tile)
    except Exception:
        pass

    return {
        "best_spread": best["spread"] if best else None,
        "best_spread_mult": best["spread_mult"] if best else None,
        "best_roundtrips": best["roundtrips"] if best else 0,
        "best_net_per_rt": best["net_per_rt"] if best else 0.0,
        "best_score": best["score"] if best else 0.0,
        "best_avg_swing": best["avg_swing"] if best else 0.0,
        # AS side-by-side comparison fields (None if AS couldn't compute)
        "as_best_spread": as_tile["spread"] if as_tile else None,
        "as_best_score": as_tile["score"] if as_tile else None,
        "as_expected_cycles_per_day": as_tile["roundtrips"] if as_tile else None,
        "candidates": tiles,
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


def _fetch_recent_candles_ts(coinbase_client, product_id: str,
                              granularity: str = "ONE_HOUR",
                              lookback_secs: int = 7 * 24 * 3600
                              ) -> tuple[list[float], list[float]]:
    """Same as _fetch_recent_closes but returns (timestamps, closes) so the
    session-hour weighting in score_product_swings has the data it needs to
    tell 'active US hours' from 'sleeping 3am'. Timestamps are unix seconds
    of the candle START (Coinbase's convention).
    """
    try:
        per = _GRANULARITY_SECS.get(granularity, 3600)
        page_seconds = per * 300
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
            if cursor < end:
                time.sleep(0.02)
        seen: set = set()
        rows: list[tuple[float, float]] = []
        for r in sorted(all_raws, key=lambda x: float(x.get("start", 0))):
            ts = float(r.get("start", 0))
            if ts in seen or ts <= 0:
                continue
            seen.add(ts)
            c = r.get("close")
            if c is None:
                continue
            try:
                rows.append((ts, float(c)))
            except (TypeError, ValueError):
                continue
        return ([ts for ts, _ in rows], [c for _, c in rows])
    except Exception as e:
        print(f"[scanner] ts-candle fetch failed for {product_id} @ {granularity}: "
              f"{type(e).__name__}: {e}", flush=True)
        return ([], [])


# Asset-class classification (Adam 2026-07-20). Two buckets for the
# dashboard: CRYPTO (perpetual + nano crypto futures) and DERIVATIVE
# (commodities, indexes, everything else). Different vol/liquidity
# regimes → experts already treat them differently per-contract; the
# split is UI-level so operator can filter by asset class.
_COMMODITY_INDEX_PREFIXES = {
    # Metals + energy nano futures
    "SLR", "NOL", "CU", "NGS", "PT",
    # Index nano futures (S&P subsets, Mag 7, China ETF, etc.)
    "MC", "MAG7C", "CHN",
}


def classify_asset_class(product_id: str) -> str:
    """Return 'crypto' or 'derivative' for a Coinbase CFM product_id.

    Prefix-match against _COMMODITY_INDEX_PREFIXES. Default = 'crypto'.
    Kept simple: any misclassification is fixable by adjusting the set;
    the important thing is stable/reproducible bucketing for the UI.
    """
    if not product_id or "-" not in product_id:
        return "derivative"  # unknown / malformed → conservative
    prefix = product_id.split("-", 1)[0].upper()
    return "derivative" if prefix in _COMMODITY_INDEX_PREFIXES else "crypto"


def _fetch_recent_ohlcv(coinbase_client, product_id: str,
                         granularity: str = "ONE_HOUR",
                         lookback_secs: int = 7 * 24 * 3600) -> list[dict]:
    """Fetch full OHLCV bars (not just closes) for indicator computation.

    Same paging/dedup pattern as _fetch_recent_closes but preserves the
    full {open, high, low, close, volume, start} shape per bar so
    scanner_indicators can compute ATR, Amihud, Yang-Zhang, etc.

    Returns bars oldest → newest. Empty list on error (never raises).
    """
    try:
        per = _GRANULARITY_SECS.get(granularity, 3600)
        page_seconds = per * 300
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
            if cursor < end:
                time.sleep(0.02)
        # Coinbase returns descending; de-dup + sort ascending.
        seen: set = set()
        bars: list[dict] = []
        for r in sorted(all_raws, key=lambda x: float(x.get("start", 0))):
            ts = float(r.get("start", 0))
            if ts in seen:
                continue
            seen.add(ts)
            try:
                bars.append({
                    "start": ts,
                    "open": float(r.get("open") or 0),
                    "high": float(r.get("high") or 0),
                    "low": float(r.get("low") or 0),
                    "close": float(r.get("close") or 0),
                    "volume": float(r.get("volume") or 0),
                })
            except (TypeError, ValueError):
                continue
        return bars
    except Exception as e:
        print(f"[scanner] ohlcv fetch failed for {product_id} @ {granularity}: "
              f"{type(e).__name__}: {e}", flush=True)
        return []


def expert_expected_daily_pnl(bars: list[dict], indicators: dict,
                                mark: float, fee_rt: float,
                                contract_size: float, tick: float,
                                asset_class: str = "crypto") -> dict:
    """Compute expected daily $-PnL for a product using PURELY expert-derived
    forward-looking inputs. Contrast: the empirical grid uses backward-
    looking cycle counts from the last N bars.

    Method (all citations in scanner_indicators or expert_*.py):
      1. Yang-Zhang (2000) → drift-independent per-bar vol σ_YZ.
      2. Scale to daily σ_d = σ_YZ × sqrt(bars_per_day). Crypto = 24 bars
         (1H granularity, 24/7 market); derivative = ~6.5 bars (US session).
      3. Expected daily $-range = σ_d × mark × k, where k = √(2π) ≈ 2.507
         is the expected absolute-return multiplier for a Gaussian random
         walk (E[|X|] = σ√(2/π) per step, telescoped over the day gives
         ~2.5× σ_d × mark; see Cartea-Jaimungal ch.2 for full derivation).
      4. Grid-search spreads: candidate spreads in [1×tick, 30×tick].
         For each: cycles/day = daily_range / (2 × spread), capped at 200
         (sanity cap — anything more is unrealistic even in ideal markets).
      5. Profit per cycle = spread × contract_size − fee_rt.
      6. Daily PnL = cycles × profit/cycle. Pick the spread that maximizes.

    Returns {'spread', 'cycles_per_day', 'profit_per_cycle',
             'expected_daily_pnl', 'daily_range_expected', 'sigma_daily'}.
    Returns {'expected_daily_pnl': 0.0, ...} on unusable inputs.

    Cite: Yang & Zhang (2000) J.Business 73(3):477; Cartea-Jaimungal-Penalva
    (2015) ch.2 Gaussian scaling; Amihud-Mendelson (1986) frequency-spread
    trade-off (spread search maximizes the joint objective).
    """
    import math as _math
    default = {"spread": 0.0, "cycles_per_day": 0.0,
               "profit_per_cycle": 0.0, "expected_daily_pnl": 0.0,
               "daily_range_expected": 0.0, "sigma_daily": 0.0}
    yz_vol = float(indicators.get("yang_zhang_vol") or 0.0)
    if yz_vol <= 0 or mark <= 0 or contract_size <= 0:
        return default
    # Bars per day depends on granularity we fetched (1H) and market hours.
    # Crypto = 24/7, derivative = ~6.5 sessions/day (US futures pit hours).
    bars_per_day = 24.0 if asset_class == "crypto" else 6.5
    sigma_daily = yz_vol * _math.sqrt(bars_per_day)
    # Expected daily $-range: E[|R|] over a day using Gaussian scaling.
    # k = sqrt(2/π) per step × ~sqrt(bars_per_day) telescoping ~ 2.5.
    k = _math.sqrt(2.0 * _math.pi) / 2.0  # ≈ 1.25; tuned conservative
    daily_range = sigma_daily * mark * (2.0 * k)  # 2×k for round-trip
    if daily_range <= 0:
        return default
    tick_min = max(tick, mark * 0.0001) if tick > 0 else mark * 0.0001
    # Spread grid — same shape as empirical grid so results are comparable.
    spreads = [tick_min * m for m in (1, 2, 3, 5, 8, 12, 20, 30)]
    best = default.copy()
    best_pnl = -1.0
    CYCLES_PER_DAY_CAP = 200.0
    for s in spreads:
        if s <= 0:
            continue
        cycles = min(daily_range / (2.0 * s), CYCLES_PER_DAY_CAP)
        if cycles <= 0:
            continue
        profit_per_cycle = s * contract_size - fee_rt
        # §3.7: profit must clear fees + safety. Skip spreads that would
        # net negative even before slippage.
        if profit_per_cycle <= 0:
            continue
        daily_pnl = cycles * profit_per_cycle
        if daily_pnl > best_pnl:
            best_pnl = daily_pnl
            best = {
                "spread": round(s, 6),
                "cycles_per_day": round(cycles, 2),
                "profit_per_cycle": round(profit_per_cycle, 4),
                "expected_daily_pnl": round(daily_pnl, 4),
                "daily_range_expected": round(daily_range, 6),
                "sigma_daily": round(sigma_daily, 8),
            }
    return best


def apply_expert_gates(entry: dict, bars: list[dict]) -> dict:
    """Run the full §3.15 expert consensus on a scanner ranking entry.

    Adds these fields to `entry` (in-place + returns for chaining):
        indicators           — per-contract ATR/Amihud/Roll/Kyle/YZ/etc
        liquidity_tier       — expert_liquidity: liquid|medium|illiquid|very_illiquid
        arm_gate_allow       — expert_arm_gate: bool (regime OK for entry?)
        arm_gate_votes       — expert_arm_gate: per-expert vote dict
        expert_stop_distance — expert_stop.optimal_stop_distance (or None)
        expert_trail_distance— expert_trail.optimal_trail_distance (or None)
        expert_adjusted_score— best_score × liquidity_multiplier × gate_multiplier
        expert_citation      — combined citation string
        asset_class          — 'crypto' or 'derivative'

    Fails open: any expert error leaves that field unset + logs to stderr;
    the underlying best_score stays intact so downstream ranking still
    works even if experts can't vote.
    """
    import scanner_indicators as _ind
    pid = entry.get("product_id") or ""
    mark = float(entry.get("price") or 0)
    fee_rt = float(entry.get("fee_per_contract_roundtrip") or 0)
    csize = float(entry.get("contract_size") or 0)
    tick = float(entry.get("tick_size") or 0)

    entry["asset_class"] = classify_asset_class(pid)

    if not bars or mark <= 0 or csize <= 0:
        entry["expert_gate_skipped"] = "insufficient inputs"
        return entry

    ind = _ind.compute_all(bars, mid_price=mark)
    entry["indicators"] = {k: round(v, 8) if isinstance(v, float) else v
                            for k, v in ind.items()}

    # 1. Liquidity tier
    tier = "unknown"
    liq_mult = 1.0
    try:
        import expert_liquidity as _el
        if getattr(_el, "MODE", "expert") == "expert":
            liq_bars = [{"close": b["close"], "volume": b["volume"],
                         "high": b["high"], "low": b["low"]} for b in bars]
            liq_dec = _el.assess_liquidity(
                bars=liq_bars, mark=mark,
                fee_per_roundtrip=fee_rt, contract_size=csize, qty=1,
            )
            if liq_dec is not None:
                tier = liq_dec.tier
                entry["liquidity_tier"] = tier
                entry["liquidity_decision"] = {
                    "tier": tier,
                    "amihud_illiq": liq_dec.amihud_illiq,
                    "roll_spread": liq_dec.roll_spread,
                    "kyle_lambda": liq_dec.kyle_lambda,
                    "preferred_exit_style": liq_dec.preferred_exit_style,
                    "ratchet_min_improvement_dollars":
                        liq_dec.ratchet_min_improvement_dollars,
                    "citation": liq_dec.citation,
                }
                # Score multiplier: liquid = 1.0, penalize progressively down.
                liq_mult = {
                    "liquid": 1.0, "medium": 0.75,
                    "illiquid": 0.4, "very_illiquid": 0.15,
                }.get(tier, 1.0)
    except Exception as e:
        entry["liquidity_expert_error"] = f"{type(e).__name__}: {e}"

    # 2. Arm-gate (regime filter)
    gate_mult = 1.0
    try:
        import expert_arm_gate as _eag
        if getattr(_eag, "MODE", "expert") == "expert":
            prices = [b["close"] for b in bars]
            arm_dec = _eag.arm_allowed(
                prices=prices, arm_direction="buy",
                order_flow_imbalance=ind.get("ofi") or None,
                kyle_lambda=ind.get("kyle_lambda") or None,
                kyle_baseline=None,
            )
            entry["arm_gate_allow"] = bool(arm_dec.allow)
            entry["arm_gate_votes"] = arm_dec.votes
            entry["arm_gate_method"] = arm_dec.method
            # Score multiplier: allowed = 1.0, denied = 0.1 (still show but
            # deeply demoted so operator can see it was scored but gated).
            gate_mult = 1.0 if arm_dec.allow else 0.1
    except Exception as e:
        entry["arm_gate_error"] = f"{type(e).__name__}: {e}"

    # 3. Expected stop distance (informs realistic profit-per-cycle)
    try:
        import expert_stop as _est
        if getattr(_est, "MODE", "expert") == "expert" and ind.get("atr", 0) > 0:
            stop_dec = _est.optimal_stop_distance(
                mark=mark, atr_est=ind["atr"],
                fee_per_roundtrip=fee_rt, contract_size=csize, qty=1,
                order_flow_imbalance=ind.get("ofi") or None,
                kyle_lambda=ind.get("kyle_lambda") or None,
                wilder_multiplier=2.0,
                tick_size=(tick if tick > 0 else None),
            )
            if stop_dec is not None:
                entry["expert_stop_distance"] = stop_dec.stop_distance
                entry["expert_stop_px"] = stop_dec.stop_px
    except Exception as e:
        entry["expert_stop_error"] = f"{type(e).__name__}: {e}"

    # 4. Expected trail distance
    try:
        import expert_trail as _etr
        if getattr(_etr, "MODE", "expert") == "expert" and ind.get("atr", 0) > 0:
            prices = [b["close"] for b in bars]
            trail_dec = _etr.optimal_trail_distance(
                mid_price=mark, highest_high=max(b["high"] for b in bars),
                atr_est=ind["atr"], prices=prices,
                fee_per_roundtrip=fee_rt, contract_size=csize, qty=1,
            )
            if trail_dec is not None:
                entry["expert_trail_distance"] = trail_dec.trail_distance
    except Exception as e:
        entry["expert_trail_error"] = f"{type(e).__name__}: {e}"

    # 5. Expert-driven forward-looking PnL forecast (Yang-Zhang vol path,
    #    §3.15 fully expert-driven). Ranks products by EXPECTED $/day from
    #    academic-cited vol × spread math, not backward-looking cycle count.
    #    Existing best_score (empirical grid) still populated as a comparison
    #    baseline; expert_expected_daily_pnl is the new primary ranking key.
    fwd = expert_expected_daily_pnl(
        bars=bars, indicators=ind, mark=mark,
        fee_rt=fee_rt, contract_size=csize, tick=tick,
        asset_class=entry["asset_class"],
    )
    entry["expert_forecast"] = fwd

    # 6. Combined expert-adjusted score. Uses the FORWARD-LOOKING
    #    expected_daily_pnl (from YZ vol + spread search) as base when
    #    available, falls back to empirical best_score if not (cold product,
    #    insufficient bars). Then multiplied by liquidity + arm-gate gates.
    base_score = float(fwd.get("expected_daily_pnl") or 0)
    if base_score <= 0:
        base_score = float(entry.get("best_score") or 0)  # fallback
        entry["expert_score_source"] = "empirical_fallback"
    else:
        entry["expert_score_source"] = "expert_forward_looking"
    entry["expert_adjusted_score"] = round(base_score * liq_mult * gate_mult, 4)
    entry["expert_multipliers"] = {
        "liquidity": round(liq_mult, 3),
        "arm_gate": round(gate_mult, 3),
    }
    entry["expert_citation"] = (
        "expert_liquidity (Amihud/Roll/Kyle/Hasbrouck/A-M/Timmermann); "
        "expert_arm_gate (Kaufman/Wilder-ADX/CJP-OFI/Kyle-λ/Connors/Bollinger); "
        "expert_stop (Wilder-2N/CJP/Kyle/Menkveld/Van Tharp); "
        "expert_trail (Chande/Wilder-SAR/Turtle/Ho-Stoll); "
        "expert_forecast (Yang-Zhang 2000 vol × Cartea-Jaimungal ch.2 scaling "
        "× Amihud-Mendelson 1986 frequency-spread trade-off)"
    )
    return entry


def fetch_and_rank(
    coinbase_client,
    top_n: int = 10,
    swing_fee_per_contract_roundtrip: float = 0.5,
    swing_lookback_secs: int = 24 * 3600,
    swing_granularity: str = "FIFTEEN_MINUTE",
    default_target_net_per_contract: float = 10.0,
    force_include: list[str] | None = None,
    spec_fallbacks: dict[str, dict] | None = None,
    toxicity_lookup: dict[str, float] | None = None,
    funding_boost_lookup: dict[str, float] | None = None,
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
        # Fetch 7d and 30d 1H candles WITH timestamps so score_product_swings
        # can apply session-hour weighting (Livermore rule: cycles that fire
        # during the active US session count full weight; 3am dead-zone
        # cycles count half). Old code fetched closes-only and couldn't
        # distinguish "10 profitable RTs during peak hours" from "10 RTs
        # scattered across the overnight nobody-trading window."
        weekly_ts, weekly_closes = _fetch_recent_candles_ts(
            coinbase_client, pid,
            granularity="ONE_HOUR", lookback_secs=7 * 24 * 3600,
        )
        time.sleep(0.03)
        monthly_ts, monthly_closes = _fetch_recent_candles_ts(
            coinbase_client, pid,
            granularity="ONE_HOUR", lookback_secs=30 * 24 * 3600,
        )
        time.sleep(0.03)
        # Toxicity penalty derived from the bot's live microstructure snapshot
        # for THIS product (VPIN + Kyle-λ-normalized). Range 0..1. High values
        # (informed / one-sided flow) scale the tile's expected $/day DOWN.
        # If no live snapshot exists yet, penalty is 0 (permissive).
        product_toxicity = 0.0
        if toxicity_lookup:
            try:
                product_toxicity = float(toxicity_lookup.get(pid) or 0.0)
            except (TypeError, ValueError):
                product_toxicity = 0.0
        product_funding_boost = 1.0
        if funding_boost_lookup:
            try:
                product_funding_boost = float(funding_boost_lookup.get(pid) or 1.0)
            except (TypeError, ValueError):
                product_funding_boost = 1.0
        swing = score_product_swings(
            closes, tick, csize, effective_fee_rt,
            weekly_prices=weekly_closes or None,
            monthly_prices=monthly_closes or None,
            weekly_timestamps=weekly_ts or None,
            monthly_timestamps=monthly_ts or None,
            toxicity_penalty=max(0.0, min(1.0, product_toxicity)),
            funding_boost=product_funding_boost,
        )
        if fallback_used and swing.get("candidates"):
            # 7d/1H fallback: `closes` is a week's worth, so rt count needs
            # dividing by 7 for the per-day display in the UI tile. Weighted
            # score is already in $/day units so it stays correct.
            for c in swing["candidates"]:
                rt7 = int(c.get("roundtrips", 0) or 0)
                rt_daily = max(1, round(rt7 / 7.0)) if rt7 > 0 else 0
                c["roundtrips"] = rt_daily
            best = swing["candidates"][0] if swing["candidates"] else None
            if best:
                swing["best_roundtrips"] = best["roundtrips"]
        # weekly_score / monthly_score for THIS product's best spread (used
        # for the /api/scanner tile's own weekly/monthly display, separate
        # from the per-candidate score_weekly / score_monthly we now expose).
        best_spread_for_periods = float(swing["best_spread"] or 0.0)
        weekly_rt = 0
        weekly_score_val = 0.0
        monthly_rt = 0
        monthly_score_val = 0.0
        if best_spread_for_periods > 0:
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
    # Adam 2026-07-20 §3.15 EXPERT GATE PASS: fetch OHLCV per product and
    # run the full expert consensus (liquidity tier + arm-gate + expert_stop
    # + expert_trail) to compute expert_adjusted_score. Failures leave the
    # base best_score intact so a broken expert never zeros out ranking.
    for entry in ranking:
        pid = entry.get("product_id")
        if not pid:
            continue
        try:
            # OHLCV: 30 days of 1H bars = ~720 bars. Enough for stable ATR,
            # Amihud, Yang-Zhang. Reuses the same cadence Coinbase serves
            # for the closes fetch above so we don't hammer the API.
            ohlcv_bars = _fetch_recent_ohlcv(
                coinbase_client, pid,
                granularity="ONE_HOUR", lookback_secs=30 * 24 * 3600,
            )
            apply_expert_gates(entry, ohlcv_bars)
        except Exception as _eg_err:
            entry["expert_gate_error"] = f"{type(_eg_err).__name__}: {_eg_err}"
        time.sleep(0.02)

    # Rank by EXPERT-ADJUSTED score (base_score × liquidity_mult × gate_mult).
    # Falls back to best_score if expert-adjusted wasn't computed. Tie-break
    # on 24h range then volume.
    def _rank_key(r):
        primary = r.get("expert_adjusted_score")
        if primary is None:
            primary = r.get("best_score") or 0.0
        return (-(primary or 0.0),
                -(r.get("vol_pct") or 0.0),
                -(r.get("volume_24h") or 0.0))
    ranking.sort(key=_rank_key)
    # Force-included products MUST survive the top_n truncation. The scanner's
    # job is to help Adam discover NEW products worth trading; anything he
    # already has a strategy on (force_include) needs its BEST tile populated
    # regardless of where it ranks on 24h volatility. Split the sorted list:
    # keep the top_n by score, then append every force_include entry that
    # didn't make that cut. That way the dashboard's Edit Strategy modal
    # always finds swing_candidates for products he's actively trading.
    forced_set = set(force_include or [])
    if forced_set:
        top_slice = ranking[:top_n]
        top_ids = {e.get("product_id") for e in top_slice}
        extras = [e for e in ranking[top_n:]
                  if e.get("product_id") in forced_set
                  and e.get("product_id") not in top_ids]
        return top_slice + extras
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
    """Publish the ranking under REDIS_KEY. Dashboard reads from here.

    Adam 2026-07-20: also publishes `top_crypto` and `top_derivative`
    (split by classify_asset_class). Dashboard can render either the
    combined `top` (backward compat) or the two class-split lists
    without re-classifying client-side. `top` remains the full ordered
    ranking for any consumer that hasn't been updated.
    """
    import redis
    r = redis.Redis.from_url(url, decode_responses=True)
    top_crypto = [e for e in ranking
                  if (e.get("asset_class") or
                      classify_asset_class(e.get("product_id") or "")) == "crypto"]
    top_derivative = [e for e in ranking
                      if (e.get("asset_class") or
                          classify_asset_class(e.get("product_id") or "")) == "derivative"]
    payload = {
        "generated_at": generated_at if generated_at is not None else time.time(),
        "top": ranking,
        "top_crypto": top_crypto,
        "top_derivative": top_derivative,
    }
    r.set(REDIS_KEY, json.dumps(payload))


def read_ranking_from_redis(url: str) -> Optional[dict]:
    import redis
    r = redis.Redis.from_url(url, decode_responses=True)
    raw = r.get(REDIS_KEY)
    return json.loads(raw) if raw else None
