"""Moskowitz-Ooi-Pedersen — Time Series Momentum (JFE 2012).

The single most-cited edge in the trend literature. MOP show that an
asset's past 12-month return predicts its next-month return with pooled
t-stat = 4.34 across 58 futures. Hurst-Ooi-Pedersen (2017 JPM) replicate
over a century.

Practical implication (Kaminski-Lo 2014): stops only ADD value when
combined with a trend filter. Without one, stops cut winners and let
losers run. Our bot ships stops (Van Tharp, Le Beau chandelier,
protect-half) but has NO trend-entry filter — so this closes a real gap.

Adapted for our 5s-tick timescale: MOP's 12-month lookback is too long
for our cycle rate. Default to 30-day trailing return (well-supported by
Hurst-Ooi-Pedersen 2017 who show TS-momentum works at multiple horizons
including 1-month).

Ships as:
  1. `compute_ts_momentum(candles, lookback_days)` — the return signal
  2. `ts_momentum_signal(ret)` — bullish/bearish/neutral map
  3. `ts_momentum_ok_for_buy(candles, ...)` — gate helper for _sleeve_arm
  4. Scanner boost for positive-momentum products (mirror funding_boost)
"""

from __future__ import annotations

import math
from typing import Optional


def compute_ts_momentum(prices: list[float],
                        lookback_bars: int = 30) -> Optional[float]:
    """Log return of the last `lookback_bars` closes. Returns None if
    insufficient data.

    prices: list of close prices in chronological order (oldest first,
    newest last). lookback_bars = number of bars to look back. For our
    ~1h candles used by the scanner, 30 bars = ~30 hours of trading.
    Adjust upward (say 30 * 24 = 720 for 1h bars over 30 days) at the
    caller if longer horizons desired.
    """
    if not prices or len(prices) < lookback_bars + 1:
        return None
    start = prices[-lookback_bars - 1]
    end = prices[-1]
    if start <= 0 or end <= 0:
        return None
    return math.log(end / start)


def ts_momentum_signal(log_return: Optional[float],
                       neutral_band: float = 0.001) -> str:
    """Map log return → 'bullish' / 'bearish' / 'neutral'.

    neutral_band: |return| below this is considered non-directional. 0.1%
    default — anything smaller is noise at our tick cadence.
    """
    if log_return is None:
        return "neutral"
    if log_return > neutral_band:
        return "bullish"
    if log_return < -neutral_band:
        return "bearish"
    return "neutral"


def ts_momentum_ok_for_buy(prices: list[float],
                           lookback_bars: int = 30,
                           neutral_band: float = 0.001) -> tuple[bool, Optional[float]]:
    """MOP entry filter: BLOCK new BUY arms when trailing return is
    strongly negative. Returns (ok, log_return).

    Permissive-default: True when insufficient data. Bullish + neutral →
    allow. Bearish (log_return < -neutral_band) → block.
    """
    lr = compute_ts_momentum(prices, lookback_bars)
    if lr is None:
        return (True, None)  # permissive default
    return (lr >= -neutral_band, lr)


def scanner_boost(log_return: Optional[float],
                  max_boost: float = 0.3) -> float:
    """Return a multiplier in [1 - max_boost, 1 + max_boost] to apply to
    the scanner tile's expected $/day. MOP-positive products rank higher
    (we're a long-biased bot; trending UP is good). Symmetric on the
    downside — actively-falling products get penalized.

    log_return scaled so ±5% (i.e., ±0.05 log return over 30 bars, a
    strong signal) maps to full boost/penalty. Clamped.
    """
    if log_return is None:
        return 1.0
    scale = 0.05
    signal = max(-1.0, min(1.0, log_return / scale))
    return 1.0 + max_boost * signal


# ---------------------------------------------------------------------------
# Long-horizon canonical trend filter (Option D-1, 2026-07-19)
# ---------------------------------------------------------------------------
#
# The block above ships a SHORT-horizon (30-bar) momentum signal used by the
# scanner. That's a scoring boost, not a hard entry gate — the docstring in
# expert_params.py flags a "time-series-momentum / 200-day-SMA trend ENTRY
# filter" as the biggest evidence-backed gap in the whole system.
#
# This section adds the canonical long-horizon filter:
#   - Faber (2007 J.Wealth Management): 10-month (~200 trading day) SMA.
#     Long when close > SMA, flat otherwise. Robust across a century of
#     data (Hurst-Ooi-Pedersen 2017 JPM).
#   - Moskowitz-Ooi-Pedersen (2012 J.Finance): 12-month time-series
#     momentum. Sign of trailing 252-day return predicts next-month sign
#     with pooled t~4.34 across 58 futures.
#   - Kaminski-Lo (2014): stops help ONLY under momentum, HURT under
#     mean-reversion. Motivates the gate.
#
# Computed on DAILY closes (canonical granularity for both filters).
# Ships with a feature flag OFF by default per the same discipline that
# gated Kaufman ER — code lands, backtest-referee sign-off + grid-search
# validation required before Adam flips the flag.

import os as _os
import time as _time


_TREND_CACHE_SYMBOL = "__trend_state__"
_TREND_CACHE_TTL_SECS = 12 * 3600.0
_TREND_DEFAULT_TSM_DAYS = 252   # ~12 months (canonical MOP)
_TREND_DEFAULT_SMA_DAYS = 200    # Faber 10-month
_TREND_DEFAULT_MODE = "either"   # 'sma' | 'tsm' | 'both' | 'either'


def long_trend_flag_enabled() -> bool:
    """Master switch. Off by default per 2026-07-19 backtest-referee
    discipline — the module ships, feature flag prevents it from
    touching live behavior until a proper backtest study runs.

    Enable path:
      1. Backtest 30-90d on adam-live's held products against a control
         with the flag OFF. Compare cycles/day + net $/day.
      2. Confirm no false-positive filter blocks during obvious trend-up
         days (regression against past 6 months).
      3. Verify verdict cache is refreshed at expected cadence and
         doesn't silently freeze on Coinbase daily-candle outages.
      4. Set SWING_TREND_FILTER_ENABLED=1
    """
    return _os.getenv("SWING_TREND_FILTER_ENABLED", "0").lower() in ("1", "true", "yes", "on")


def compute_tsm_sign(daily_closes: list[float],
                      lookback_days: int = _TREND_DEFAULT_TSM_DAYS) -> Optional[int]:
    """MOP 12-month time-series momentum sign. +1 up, -1 down, 0 flat,
    None on insufficient data."""
    if not daily_closes or len(daily_closes) < lookback_days + 1:
        return None
    old = daily_closes[-(lookback_days + 1)]
    new = daily_closes[-1]
    if old <= 0 or new <= 0:
        return None
    if new > old:
        return 1
    if new < old:
        return -1
    return 0


def compute_faber_gap(daily_closes: list[float],
                       window: int = _TREND_DEFAULT_SMA_DAYS) -> Optional[float]:
    """Faber 10-month SMA gap: (last_close − SMA) / SMA. Positive =
    price above trend (BUY OK), negative = price below trend (BUY
    blocked). None when insufficient data."""
    if not daily_closes or len(daily_closes) < window + 1:
        return None
    tail = daily_closes[-window:]
    sma = sum(tail) / len(tail)
    if sma <= 0:
        return None
    last = daily_closes[-1]
    return (last - sma) / sma


def long_trend_verdict(daily_closes: list[float],
                        mode: str = _TREND_DEFAULT_MODE,
                        tsm_lookback: int = _TREND_DEFAULT_TSM_DAYS,
                        sma_window: int = _TREND_DEFAULT_SMA_DAYS) -> dict:
    """Combine TSM sign + Faber gap into a verdict.

    mode:
      'sma'    — Faber only
      'tsm'    — MOP only
      'both'   — AND (conservative — needs BOTH agreeing)
      'either' — OR (permissive — either agreeing is fine)

    Insufficient data → buy_ok=True (matches Faber's paper: rule
    reverts to buy-and-hold when signal unavailable).
    """
    tsm_sign = compute_tsm_sign(daily_closes, lookback_days=tsm_lookback)
    faber_gap = compute_faber_gap(daily_closes, window=sma_window)
    sma_ok = None if faber_gap is None else faber_gap > 0
    tsm_ok = None if tsm_sign is None else tsm_sign > 0

    if mode == "sma":
        buy_ok = True if sma_ok is None else sma_ok
    elif mode == "tsm":
        buy_ok = True if tsm_ok is None else tsm_ok
    elif mode == "both":
        buy_ok = (sma_ok is not False) and (tsm_ok is not False)
    else:  # 'either'
        if sma_ok is None and tsm_ok is None:
            buy_ok = True
        else:
            buy_ok = bool(sma_ok) or bool(tsm_ok)

    return {
        "buy_ok": bool(buy_ok),
        "mode": mode,
        "tsm_sign": tsm_sign,
        "faber_gap": round(faber_gap, 6) if faber_gap is not None else None,
        "computed_at": _time.time(),
    }


def load_long_trend_verdict(store, tenant: str, product_id: str) -> Optional[dict]:
    """Read cached verdict. Returns None if missing/stale."""
    try:
        blob = store.get_config(tenant, _TREND_CACHE_SYMBOL) or {}
    except Exception:
        return None
    entries = blob.get("entries") or {}
    v = entries.get(product_id)
    if not isinstance(v, dict):
        return None
    ts = float(v.get("computed_at") or 0)
    if ts <= 0 or (_time.time() - ts) > _TREND_CACHE_TTL_SECS:
        return None
    return v


def save_long_trend_verdict(store, tenant: str, product_id: str, verdict: dict) -> None:
    """Cache verdict for later reads. Best-effort."""
    try:
        blob = dict(store.get_config(tenant, _TREND_CACHE_SYMBOL) or {})
        entries = dict(blob.get("entries") or {})
        entries[product_id] = verdict
        blob["entries"] = entries
        blob["last_write_ts"] = _time.time()
        store.put_config(tenant, _TREND_CACHE_SYMBOL, blob)
    except Exception:
        pass


def long_trend_ok_for_buy(store, tenant: str, product_id: str) -> tuple[bool, str]:
    """Gate BUY arms on long-horizon trend. Returns (allowed, reason).

    - Flag OFF                       → (True, "flag_off")
    - No cache entry / stale (12h)   → (True, "no_cache_fail_open")
    - Verdict says trend up          → (True, "trend_up")
    - Verdict says trend down        → (False, "trend_down_<mode>_<detail>")

    Fail-open on cache miss: a Coinbase daily-candle outage should not
    freeze BUY arms indefinitely. Same discipline as reload-on-tick.
    """
    if not long_trend_flag_enabled():
        return True, "flag_off"
    v = load_long_trend_verdict(store, tenant, product_id)
    if v is None:
        return True, "no_cache_fail_open"
    if v.get("buy_ok"):
        return True, "trend_up"
    parts = []
    if v.get("tsm_sign") is not None:
        parts.append(f"tsm={v.get('tsm_sign')}")
    if v.get("faber_gap") is not None:
        parts.append(f"gap={v.get('faber_gap')}")
    return False, f"trend_down_{v.get('mode', 'unknown')}_{'_'.join(parts) or 'no_signal'}"


def refresh_long_trend_verdict(broker, store, tenant: str, product_id: str) -> Optional[dict]:
    """Fetch daily candles + compute + cache a fresh verdict. Meant to be
    called from a slow periodic loop (default every 6h) in live_runner.
    Bounded API cost: 1 daily-candle call per held product per refresh."""
    try:
        from datetime import datetime, timedelta, timezone
        from backtest import fetch_candles
    except Exception:
        return None
    end = datetime.now(timezone.utc)
    # 400 days > max(200 SMA, 252 TSM) with buffer for weekends/holidays.
    start = end - timedelta(days=400)
    try:
        candles = fetch_candles(broker.client, product_id, start, end,
                                 granularity="ONE_DAY")
    except Exception:
        return None
    closes: list[float] = []
    for c in candles or []:
        try:
            v = float(getattr(c, "close", None) or
                       (c.get("close") if hasattr(c, "get") else 0))
        except (TypeError, ValueError):
            v = 0.0
        if v > 0:
            closes.append(v)
    if not closes:
        return None
    verdict = long_trend_verdict(closes)
    save_long_trend_verdict(store, tenant, product_id, verdict)
    return verdict
