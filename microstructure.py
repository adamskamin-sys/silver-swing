"""
microstructure.py — five HFT-literature-derived signals wired into the swing bot.

Each signal is a class that maintains rolling state and exposes a current
`value()` and a decision helper (`should_pause()`, `adjusted_px()`, etc.). All
are independently toggleable via env vars so paper testing can isolate one at
a time.

Signals implemented:
  1. EffectiveSpreadEstimator — Roll (1984). Rolling median spread. Feeds the
     adaptive band that replaces hard-coded buy_px/sell_px with mid ± k×spread.
  2. ReturnAutocorrelation — Roll (1984). Rolling lag-1 autocorrelation of
     returns. Negative = bid-ask bounce = mean reversion regime (good for
     swing). Positive/near-zero = trending or random = pause range trades.
  3. OrderBookImbalance — Cont, Kukanov, Stoikov (2014). Top-N depth
     imbalance. When you're about to send a market order, unfavorable OBI
     predicts you'll get a worse fill. Delay the trade one tick.
  4. VPINEstimator — Easley, López de Prado, O'Hara (2012). Volume-clock
     buckets of signed volume; toxicity = |buy_vol - sell_vol| / total_vol.
     High VPIN = informed flow is running, don't rest orders — pause new arms.
  5. KylesLambda — Kyle (1985). Rolling regression |dP| = λ × |signed_vol|.
     Measures price impact per unit volume. High λ = illiquid regime, reduce
     size.

The MicrostructureFilter aggregates enabled signals and exposes:
  - snapshot() — current value of every enabled signal (for dashboard visibility)
  - should_pause_arm(side) — True if any gating signal says stand aside
  - adjusted_buy_px(cfg_buy, mid) / adjusted_sell_px(cfg_sell, mid) — spread band
  - size_scale() — 0..1 multiplier from Kyle λ (1.0 = full size, 0.5 = half)

Env vars (each is 0/1):
  SWING_MS_SPREAD_BAND=1    — signal 1 gating buy_px/sell_px
  SWING_MS_AUTOCORR=1       — signal 2 as regime pause
  SWING_MS_OBI=1            — signal 3 as pre-entry delay
  SWING_MS_VPIN=1           — signal 4 as toxicity pause
  SWING_MS_LAMBDA=1         — signal 5 as size scaling
  SWING_MS_ALL=1            — enable all five at once

Params tunable via env (defaults are conservative starting points):
  SWING_MS_SPREAD_WINDOW=60         — seconds of rolling spread history
  SWING_MS_SPREAD_K=2.0             — multiplier on measured spread for band
  SWING_MS_AUTOCORR_WINDOW=100      — ticks in rolling autocorr
  SWING_MS_AUTOCORR_MAX=0.0         — pause if autocorr > this
  SWING_MS_OBI_LEVELS=5             — depth levels to consider
  SWING_MS_OBI_THRESHOLD=0.5        — |OBI| beyond this delays entry
  SWING_MS_VPIN_BUCKET=50           — contracts per volume bucket
  SWING_MS_VPIN_WINDOW=50           — buckets in rolling VPIN
  SWING_MS_VPIN_MAX=0.7             — pause if VPIN > this
  SWING_MS_LAMBDA_WINDOW=200        — trades in rolling regression
  SWING_MS_LAMBDA_MAX=0.001         — reduce size if λ > this
"""

from __future__ import annotations

import math
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


def _envf(key: str, default: float) -> float:
    v = os.getenv(key)
    if v is None:
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _envi(key: str, default: int) -> int:
    return int(_envf(key, default))


def _envb(key: str) -> bool:
    v = os.getenv(key, "0").strip().lower()
    return v in ("1", "true", "yes", "on")


# ============================================================================
# Signal 1: Effective spread estimator (Roll 1984)
# ============================================================================


class EffectiveSpreadEstimator:
    """Rolling median of the quoted spread (ask - bid). Feeds the adaptive
    band: buy_px = mid - k*spread, sell_px = mid + k*spread. Adapts to
    volatility regime — tight when the book is tight, wide when it isn't."""

    def __init__(self, window_secs: float = 60.0):
        self.window_secs = window_secs
        self._samples: deque[tuple[float, float]] = deque()  # (ts, spread)

    def update(self, best_bid: float, best_ask: float, ts: Optional[float] = None) -> None:
        if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
            return
        now = ts if ts is not None else time.time()
        self._samples.append((now, best_ask - best_bid))
        cutoff = now - self.window_secs
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def value(self) -> Optional[float]:
        """Rolling median spread. Median > mean for spikes-robust."""
        if not self._samples:
            return None
        sorted_spreads = sorted(s for _, s in self._samples)
        n = len(sorted_spreads)
        return sorted_spreads[n // 2] if n % 2 else (
            0.5 * (sorted_spreads[n // 2 - 1] + sorted_spreads[n // 2])
        )


# ============================================================================
# Signal 2: Return autocorrelation (Roll 1984 style regime detector)
# ============================================================================


class ReturnAutocorrelation:
    """Rolling lag-1 autocorrelation of log returns. Interpretation:
      < 0  : bid-ask bounce dominates → mean reversion regime → swing works
      ≈ 0  : random walk / efficient → swing has no edge
      > 0  : trending → swing gets whipsawed, PAUSE

    Uses lag-1 of one-tick returns. Rolling window fixed count of ticks,
    which under variable-rate ticker feeds is a compromise but simple.
    """

    def __init__(self, window: int = 100):
        self.window = window
        self._prices: deque[float] = deque(maxlen=window + 1)

    def update(self, price: float) -> None:
        if price <= 0:
            return
        self._prices.append(price)

    def value(self) -> Optional[float]:
        if len(self._prices) < 20:
            return None
        prices = list(self._prices)
        rets = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))
                if prices[i] > 0 and prices[i - 1] > 0]
        if len(rets) < 10:
            return None
        n = len(rets)
        mean = sum(rets) / n
        var = sum((r - mean) ** 2 for r in rets) / n
        if var == 0:
            return 0.0
        cov = sum((rets[i] - mean) * (rets[i - 1] - mean) for i in range(1, n)) / n
        return cov / var


# ============================================================================
# Signal 3: Order book imbalance (Cont, Kukanov, Stoikov 2014)
# ============================================================================


@dataclass
class L2Book:
    """Minimal L2 order book. Prices → sizes, sorted separately per side."""
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)

    def apply_snapshot(self, bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> None:
        self.bids = {p: s for p, s in bids if s > 0}
        self.asks = {p: s for p, s in asks if s > 0}

    def apply_update(self, side: str, price: float, new_size: float) -> None:
        book = self.bids if side.lower().startswith("b") else self.asks
        if new_size <= 0:
            book.pop(price, None)
        else:
            book[price] = new_size

    def top_n(self, n: int) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        top_bids = sorted(self.bids.items(), key=lambda kv: -kv[0])[:n]
        top_asks = sorted(self.asks.items(), key=lambda kv: kv[0])[:n]
        return top_bids, top_asks


class OrderBookImbalance:
    """OBI = (bid_size - ask_size) / (bid_size + ask_size) at top N levels.
    Range [-1, +1]. Positive = bid-heavy = short-term upward pressure.
    Negative = ask-heavy = downward pressure.

    Use as pre-entry delay: if about to BUY at market and OBI < -threshold,
    wait one tick — you'll get a worse fill right now.
    """

    def __init__(self, book: L2Book, levels: int = 5):
        self.book = book
        self.levels = levels

    def value(self) -> Optional[float]:
        top_bids, top_asks = self.book.top_n(self.levels)
        if not top_bids or not top_asks:
            return None
        bid_sz = sum(s for _, s in top_bids)
        ask_sz = sum(s for _, s in top_asks)
        if bid_sz + ask_sz == 0:
            return 0.0
        return (bid_sz - ask_sz) / (bid_sz + ask_sz)


# ============================================================================
# Signal 4: VPIN — Volume-synchronized Probability of Informed Trading
# (Easley, López de Prado, O'Hara 2012)
# ============================================================================


class VPINEstimator:
    """Buckets trade volume into fixed-size volume bins, computes
    |buy_vol - sell_vol| / bucket_size per bucket, and averages over a rolling
    window of buckets. Range [0, 1]. High VPIN → informed flow → pause.

    Trade side classification: use the exchange-provided side when available
    (Coinbase market_trades tags it), fall back to the tick rule.
    """

    def __init__(self, bucket_size: float = 50.0, window: int = 50):
        self.bucket_size = bucket_size
        self.window = window
        self._buy_bucket = 0.0
        self._sell_bucket = 0.0
        self._buckets: deque[float] = deque(maxlen=window)  # per-bucket VPINs
        self._last_price: Optional[float] = None

    def update(self, price: float, size: float, side: Optional[str] = None) -> None:
        """Feed one trade. `side` is 'buy' or 'sell' from the exchange; if
        None, we classify via the tick rule (up-tick = buy, down-tick = sell)."""
        if size <= 0 or price <= 0:
            return
        if side is None:
            if self._last_price is None:
                self._last_price = price
                return
            if price > self._last_price:
                side = "buy"
            elif price < self._last_price:
                side = "sell"
            else:
                side = "buy"  # zero-tick → treat as continuation
        self._last_price = price

        if side.lower().startswith("b"):
            self._buy_bucket += size
        else:
            self._sell_bucket += size

        while self._buy_bucket + self._sell_bucket >= self.bucket_size:
            total = self._buy_bucket + self._sell_bucket
            excess = total - self.bucket_size
            # Prorate excess proportionally back into the next bucket
            frac = self.bucket_size / total
            buy_in_bucket = self._buy_bucket * frac
            sell_in_bucket = self._sell_bucket * frac
            bucket_vpin = abs(buy_in_bucket - sell_in_bucket) / self.bucket_size
            self._buckets.append(bucket_vpin)
            # carry over the excess into the next bucket
            self._buy_bucket = self._buy_bucket - buy_in_bucket
            self._sell_bucket = self._sell_bucket - sell_in_bucket
            if excess <= 0:
                break

    def value(self) -> Optional[float]:
        if not self._buckets:
            return None
        return sum(self._buckets) / len(self._buckets)


# ============================================================================
# Signal 5: Kyle's Lambda (Kyle 1985)
# ============================================================================


class KylesLambda:
    """Rolling regression |dP| = λ × |signed_volume| over the recent trade
    window. λ is price impact per unit volume (bp per contract-ish). High λ =
    illiquid regime, reduce size. Low λ = liquid, full size OK.

    Approach: keep a rolling window of (signed_volume, abs_price_change) pairs
    where signed_volume is sum of buys minus sum of sells over 5s intervals,
    and abs_price_change is the mid-price move over that interval. Simple OLS
    slope through the origin.
    """

    def __init__(self, window: int = 200, interval_secs: float = 5.0):
        self.window = window
        self.interval_secs = interval_secs
        self._points: deque[tuple[float, float]] = deque(maxlen=window)
        self._bucket_signed_vol = 0.0
        self._bucket_start_price: Optional[float] = None
        self._bucket_last_price: Optional[float] = None
        self._bucket_start_ts: Optional[float] = None

    def _flush_bucket(self) -> None:
        if self._bucket_start_price is None or self._bucket_last_price is None:
            return
        dp = abs(self._bucket_last_price - self._bucket_start_price)
        v = abs(self._bucket_signed_vol)
        if v > 0:
            self._points.append((v, dp))
        self._bucket_signed_vol = 0.0
        self._bucket_start_price = None
        self._bucket_last_price = None
        self._bucket_start_ts = None

    def update(self, price: float, size: float, side: Optional[str],
               ts: Optional[float] = None) -> None:
        if price <= 0 or size <= 0:
            return
        now = ts if ts is not None else time.time()
        if self._bucket_start_ts is None:
            self._bucket_start_ts = now
            self._bucket_start_price = price
        # If bucket interval elapsed, close it. Compare directly — `or` would
        # short-circuit on ts=0.0.
        elif now - self._bucket_start_ts >= self.interval_secs:
            self._flush_bucket()
            self._bucket_start_ts = now
            self._bucket_start_price = price
        self._bucket_last_price = price
        signed = size if (side and side.lower().startswith("b")) else -size
        self._bucket_signed_vol += signed

    def value(self) -> Optional[float]:
        if len(self._points) < 3:
            return None
        # OLS through origin: λ = Σ(v*dp) / Σ(v²)
        num = sum(v * dp for v, dp in self._points)
        den = sum(v * v for v, _ in self._points)
        if den == 0:
            return None
        return num / den


# ============================================================================
# Aggregator
# ============================================================================


class MicrostructureFilter:
    """Aggregates enabled signals and exposes decision helpers used by the trader."""

    def __init__(self):
        all_on = _envb("SWING_MS_ALL")
        self.enable_spread = all_on or _envb("SWING_MS_SPREAD_BAND")
        self.enable_autocorr = all_on or _envb("SWING_MS_AUTOCORR")
        self.enable_obi = all_on or _envb("SWING_MS_OBI")
        self.enable_vpin = all_on or _envb("SWING_MS_VPIN")
        self.enable_lambda = all_on or _envb("SWING_MS_LAMBDA")

        self.spread = EffectiveSpreadEstimator(
            window_secs=_envf("SWING_MS_SPREAD_WINDOW", 60.0),
        )
        self.spread_k = _envf("SWING_MS_SPREAD_K", 2.0)

        self.autocorr = ReturnAutocorrelation(
            window=_envi("SWING_MS_AUTOCORR_WINDOW", 100),
        )
        self.autocorr_max = _envf("SWING_MS_AUTOCORR_MAX", 0.0)

        self.book = L2Book()
        self.obi = OrderBookImbalance(
            self.book, levels=_envi("SWING_MS_OBI_LEVELS", 5),
        )
        self.obi_threshold = _envf("SWING_MS_OBI_THRESHOLD", 0.5)

        self.vpin = VPINEstimator(
            bucket_size=_envf("SWING_MS_VPIN_BUCKET", 50.0),
            window=_envi("SWING_MS_VPIN_WINDOW", 50),
        )
        self.vpin_max = _envf("SWING_MS_VPIN_MAX", 0.7)

        self.kyle = KylesLambda(
            window=_envi("SWING_MS_LAMBDA_WINDOW", 200),
        )
        self.lambda_max = _envf("SWING_MS_LAMBDA_MAX", 0.001)

    # ---- data ingress ---------------------------------------------------

    def on_ticker(self, best_bid: float, best_ask: float, price: float) -> None:
        self.spread.update(best_bid, best_ask)
        self.autocorr.update(price)

    def on_l2_snapshot(self, bids: list, asks: list) -> None:
        self.book.apply_snapshot(bids, asks)

    def on_l2_update(self, side: str, price: float, new_size: float) -> None:
        self.book.apply_update(side, price, new_size)

    def on_trade(self, price: float, size: float, side: Optional[str],
                 ts: Optional[float] = None) -> None:
        self.vpin.update(price, size, side)
        self.kyle.update(price, size, side, ts)

    # ---- decisions ------------------------------------------------------

    def should_pause_arm(self, side: str) -> Optional[str]:
        """Return reason string if any enabled gate says pause, else None."""
        if self.enable_autocorr:
            v = self.autocorr.value()
            if v is not None and v > self.autocorr_max:
                return f"autocorr={v:.3f} > {self.autocorr_max} (trending regime)"
        if self.enable_vpin:
            v = self.vpin.value()
            if v is not None and v > self.vpin_max:
                return f"vpin={v:.3f} > {self.vpin_max} (toxic flow)"
        if self.enable_obi:
            v = self.obi.value()
            if v is not None:
                # BUY into ask-heavy book (OBI << 0) is bad; SELL into bid-heavy is bad
                if side.upper() == "BUY" and v < -self.obi_threshold:
                    return f"obi={v:.3f} < -{self.obi_threshold} (ask-heavy, wait)"
                if side.upper() == "SELL" and v > self.obi_threshold:
                    return f"obi={v:.3f} > {self.obi_threshold} (bid-heavy, wait)"
        return None

    def adjusted_buy_px(self, cfg_buy: float, mid: float) -> float:
        if not self.enable_spread:
            return cfg_buy
        s = self.spread.value()
        if s is None or mid <= 0:
            return cfg_buy
        return mid - self.spread_k * s

    def adjusted_sell_px(self, cfg_sell: float, mid: float) -> float:
        if not self.enable_spread:
            return cfg_sell
        s = self.spread.value()
        if s is None or mid <= 0:
            return cfg_sell
        return mid + self.spread_k * s

    def size_scale(self) -> float:
        """0..1 multiplier on quantity from Kyle λ. 1.0 = full size."""
        if not self.enable_lambda:
            return 1.0
        v = self.kyle.value()
        if v is None:
            return 1.0
        if v <= self.lambda_max:
            return 1.0
        # Linear taper down to 0.5 as λ hits 2× the max; floor at 0.5
        return max(0.5, 1.0 - 0.5 * (v - self.lambda_max) / self.lambda_max)

    # ---- observation for the dashboard ----------------------------------

    def snapshot(self) -> dict:
        """Current values of every enabled signal, for the dashboard."""
        out: dict = {}
        if self.enable_spread:
            out["spread_median"] = self.spread.value()
            out["spread_k"] = self.spread_k
        if self.enable_autocorr:
            out["autocorr_lag1"] = self.autocorr.value()
            out["autocorr_max"] = self.autocorr_max
        if self.enable_obi:
            out["obi"] = self.obi.value()
            out["obi_threshold"] = self.obi_threshold
        if self.enable_vpin:
            out["vpin"] = self.vpin.value()
            out["vpin_max"] = self.vpin_max
        if self.enable_lambda:
            out["kyle_lambda"] = self.kyle.value()
            out["lambda_max"] = self.lambda_max
            out["size_scale"] = self.size_scale()
        return out

    def any_enabled(self) -> bool:
        return any((self.enable_spread, self.enable_autocorr, self.enable_obi,
                    self.enable_vpin, self.enable_lambda))

    def needs_l2(self) -> bool:
        return self.enable_obi

    def needs_trades(self) -> bool:
        return self.enable_vpin or self.enable_lambda
