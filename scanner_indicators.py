"""Per-contract market-microstructure indicators for the scanner.

Adam 2026-07-20 §3.15: scanner should rank contracts using the same
academic-literature indicators the exit/entry experts use — otherwise
we're picking targets on one metric (24h vol) and executing on another
(expert consensus). This module computes the shared inputs:

    average_true_range        — Wilder (1978) 2N canonical
    amihud_illiquidity        — Amihud (2002) J.Fin.Markets 5:31-56
    roll_effective_spread     — Roll (1984) J.Finance 39(4):1127
    kyle_lambda_proxy         — Kyle (1985) Econometrica 53(6):1315
    yang_zhang_volatility     — Yang & Zhang (2000) J.Business 73(3):477
    hasbrouck_effective_cost  — Hasbrouck (2009) J.Finance 64(3):1445
    ofi_from_ohlcv            — Cartea-Jaimungal-Penalva (2015) ch.8
                                 (proxy from candle-close direction × volume)

Pure functions. No I/O, no state, no side effects. Each returns 0.0
when inputs are insufficient (< 3 bars, missing fields, etc.) so
callers can safely feed the results to experts which then no-op.

Bar shape (Coinbase get_candles):
    {"start": epoch_seconds, "open": ..., "high": ..., "low": ...,
     "close": ..., "volume": ...}
"""
from __future__ import annotations
import math
import statistics
from typing import Iterable


def _f(v, d: float = 0.0) -> float:
    """Coerce Coinbase's string/None-mixed candle fields to float."""
    if v is None:
        return d
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _normalize_bars(bars: Iterable[dict]) -> list[dict]:
    """Coerce every candle to plain floats with a consistent shape.
    Drops bars with H<L or close<=0 (invalid). Returns list, oldest first.

    Assumes caller already ordered chronologically (see scanner's
    Coinbase-descending → sorted-ascending pipeline).
    """
    out: list[dict] = []
    for b in (bars or []):
        if not isinstance(b, dict):
            continue
        o, h, l, c, v = (_f(b.get(k)) for k in ("open", "high", "low",
                                                 "close", "volume"))
        if c <= 0 or h < l or h <= 0:
            continue
        out.append({"open": o, "high": h, "low": l, "close": c,
                    "volume": v, "start": _f(b.get("start"))})
    return out


def average_true_range(bars: list[dict], period: int = 14) -> float:
    """Wilder (1978) N-period Average True Range.

    TR_t = max(H_t - L_t, |H_t - C_{t-1}|, |L_t - C_{t-1}|)
    ATR  = simple mean of last N TRs

    Returns 0.0 if fewer than 3 usable bars. Full Wilder smoothing uses
    an EMA; simple mean over the last N is the standard scanner-grade
    approximation.
    """
    b = _normalize_bars(bars)
    if len(b) < 3:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(b)):
        h, l = b[i]["high"], b[i]["low"]
        c_prev = b[i - 1]["close"]
        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        trs.append(tr)
    if not trs:
        return 0.0
    window = trs[-period:] if len(trs) >= period else trs
    return sum(window) / len(window)


def amihud_illiquidity(bars: list[dict]) -> float:
    """Amihud (2002) illiquidity ratio: mean(|return_t| / dollar_volume_t).

    Uses close-to-close returns and close-price × volume as dollar_volume
    (Coinbase futures volume is contract count; caller should account for
    contract_size when interpreting the tier, or pass dollar_volume if
    available).
    """
    b = _normalize_bars(bars)
    if len(b) < 3:
        return 0.0
    ratios: list[float] = []
    for i in range(1, len(b)):
        c_prev = b[i - 1]["close"]
        c_curr = b[i]["close"]
        vol = b[i]["volume"]
        if c_prev <= 0 or c_curr <= 0 or vol <= 0:
            continue
        try:
            ret = abs(math.log(c_curr / c_prev))
        except (ValueError, OverflowError):
            continue
        dv = vol * c_curr
        if dv > 0:
            ratios.append(ret / dv)
    if not ratios:
        return 0.0
    return sum(ratios) / len(ratios)


def roll_effective_spread(bars: list[dict]) -> float:
    """Roll (1984) implicit effective spread.

    spread = 2 √(−cov(Δp_t, Δp_{t-1}))

    Returns 0.0 when serial covariance is non-negative (trending market —
    Roll estimator is undefined). Caller falls back to other experts.
    """
    b = _normalize_bars(bars)
    if len(b) < 4:
        return 0.0
    closes = [x["close"] for x in b]
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    if len(deltas) < 2:
        return 0.0
    lag = deltas[:-1]
    now = deltas[1:]
    m_lag = sum(lag) / len(lag)
    m_now = sum(now) / len(now)
    cov = sum((lag[i] - m_lag) * (now[i] - m_now) for i in range(len(lag))) / len(lag)
    if cov >= 0:
        return 0.0
    return 2.0 * math.sqrt(-cov)


def kyle_lambda_proxy(bars: list[dict]) -> float:
    """Kyle (1985) λ proxy — mean price impact per unit of volume.

    Formal Kyle uses signed order flow; we approximate with unsigned
    volume (Amihud-Mendelson interpretation). Higher = more illiquid.
    """
    b = _normalize_bars(bars)
    if len(b) < 3:
        return 0.0
    impacts: list[float] = []
    for i in range(1, len(b)):
        c_prev = b[i - 1]["close"]
        c_curr = b[i]["close"]
        vol = b[i]["volume"]
        if c_prev <= 0 or c_curr <= 0 or vol <= 0:
            continue
        impacts.append(abs(c_curr - c_prev) / vol)
    if not impacts:
        return 0.0
    return sum(impacts) / len(impacts)


def yang_zhang_volatility(bars: list[dict]) -> float:
    """Yang & Zhang (2000) drift-independent volatility estimator.

    Combines overnight-return variance, opening-jump variance, and
    Rogers-Satchell intraday variance for a lower-bias vol estimate
    than close-to-close. Returns 0.0 with <3 bars.

    Formula (Yang-Zhang 2000, eq. 6):
      σ_YZ² = σ_o² + k·σ_c² + (1-k)·σ_rs²
    where k ≈ 0.34 / (1.34 + (N+1)/(N-1)), σ_o = overnight-return stdev,
    σ_c = open-to-close-return stdev, σ_rs = Rogers-Satchell.
    """
    b = _normalize_bars(bars)
    N = len(b)
    if N < 3:
        return 0.0
    # overnight returns: log(open_t / close_{t-1})
    on: list[float] = []
    # open-to-close returns: log(close_t / open_t)
    oc: list[float] = []
    # Rogers-Satchell per-bar variance
    rs_terms: list[float] = []
    for i in range(1, N):
        prev = b[i - 1]
        cur = b[i]
        c_prev = prev["close"]
        o, h, l, c = cur["open"], cur["high"], cur["low"], cur["close"]
        if c_prev <= 0 or o <= 0 or h <= 0 or l <= 0 or c <= 0:
            continue
        try:
            on.append(math.log(o / c_prev))
            oc.append(math.log(c / o))
            rs = math.log(h / c) * math.log(h / o) + math.log(l / c) * math.log(l / o)
            rs_terms.append(rs)
        except (ValueError, OverflowError):
            continue
    if len(on) < 2 or len(oc) < 2 or len(rs_terms) < 2:
        return 0.0
    var_o = statistics.pvariance(on)
    var_c = statistics.pvariance(oc)
    var_rs = sum(rs_terms) / len(rs_terms)
    k_num = 0.34
    k_den = 1.34 + (N + 1) / max(N - 1, 1)
    k = k_num / k_den if k_den > 0 else 0.0
    var_yz = var_o + k * var_c + (1.0 - k) * var_rs
    if var_yz < 0:
        return 0.0
    return math.sqrt(var_yz)


def hasbrouck_effective_cost(bars: list[dict]) -> float:
    """Hasbrouck (2009) effective cost — approximated as
    mean(range / close) per bar. The full estimator is a Gibbs sampler;
    this proxy correlates strongly with Gibbs on higher-frequency data
    per Hasbrouck's empirical validation.
    """
    b = _normalize_bars(bars)
    if len(b) < 3:
        return 0.0
    costs: list[float] = []
    for row in b:
        h, l, c = row["high"], row["low"], row["close"]
        if c <= 0 or h < l:
            continue
        costs.append((h - l) / c)
    if not costs:
        return 0.0
    return sum(costs) / len(costs)


def ofi_from_ohlcv(bars: list[dict]) -> float:
    """Cartea-Jaimungal-Penalva (2015) ch.8 OFI proxy from candle data.

    True OFI needs tick-level bid/ask trade side; from OHLCV alone we
    approximate signed order flow as `sign(close - open) × volume` per
    bar, then normalize to [-1, 1] by dividing by total volume. Positive
    = net buy pressure, negative = net sell pressure.

    Uses the last 20 bars (matches Bollinger/Kaufman conventions).
    """
    b = _normalize_bars(bars)
    if len(b) < 3:
        return 0.0
    window = b[-20:]
    signed = 0.0
    total = 0.0
    for row in window:
        o, c, v = row["open"], row["close"], row["volume"]
        if v <= 0 or o <= 0 or c <= 0:
            continue
        sign = 1.0 if c > o else (-1.0 if c < o else 0.0)
        signed += sign * v
        total += v
    if total <= 0:
        return 0.0
    return max(-1.0, min(1.0, signed / total))


def compute_all(bars: list[dict], mid_price: float = 0.0) -> dict:
    """Compute every indicator + return a single dict. Convenience for
    the scanner path that wants to hand a bundle to multiple experts."""
    atr = average_true_range(bars)
    return {
        "atr": atr,
        "amihud_illiq": amihud_illiquidity(bars),
        "roll_spread": roll_effective_spread(bars),
        "kyle_lambda": kyle_lambda_proxy(bars),
        "yang_zhang_vol": yang_zhang_volatility(bars),
        "hasbrouck_cost": hasbrouck_effective_cost(bars),
        "ofi": ofi_from_ohlcv(bars),
        "bars_used": len(_normalize_bars(bars)),
        "mid_price": float(mid_price or 0.0),
    }
