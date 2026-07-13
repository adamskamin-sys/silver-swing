"""Cross-exchange fair-value reference (Binance public API).

Reads best bid/ask from Binance for the same underlying (BTC/ETH/SOL) so
our sleeves can gate against Coinbase-specific price dislocations. Public
market-data endpoints don't require an API key.

Use case: refuse to arm when Coinbase is >X% away from Binance mid on the
same underlying. Prevents fills during regional feed hiccups, exchange
outages, or single-venue liquidity dislocations. NOT an arbitrage
executor — we do not trade on Binance; we only READ their price.

Only applicable to crypto perps / futures whose underlying trades on
Binance. Skipped for silver / oil / other non-crypto products.
"""

from __future__ import annotations

import time
from typing import Optional

import requests


# Coinbase product prefix → Binance spot ticker. Used for fair-value ref.
# Add entries as new products become tradeable.
_BINANCE_SYMBOL: dict[str, str] = {
    "BTC": "BTCUSDT",
    "BIT": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "ZEC": "ZECUSDT",
    "SUI": "SUIUSDT",
    "NER": "NEARUSDT",
    "NEAR": "NEARUSDT",
    "AVE": "AAVEUSDT",
    "ENA": "ENAUSDT",
    "XLP": "XLMUSDT",
    "XLM": "XLMUSDT",
    "HYP": "HYPEUSDT",
    "HYPE": "HYPEUSDT",
    "PEP": "PEPEUSDT",
}


_CACHE: dict[str, tuple[float, float]] = {}  # binance_symbol → (ts, mid_price)
_CACHE_TTL_SECS = 30.0


def _coinbase_prefix(coinbase_symbol: str) -> Optional[str]:
    """SLR-27AUG26-CDE → SLR; BTC-PERP-INTX → BTC. None for unusable input."""
    if not coinbase_symbol:
        return None
    return coinbase_symbol.split("-")[0].upper()


def binance_symbol_for(coinbase_symbol: str) -> Optional[str]:
    prefix = _coinbase_prefix(coinbase_symbol)
    if not prefix:
        return None
    return _BINANCE_SYMBOL.get(prefix)


def binance_mid_price(coinbase_symbol: str, timeout: float = 3.0) -> Optional[float]:
    """Return the current Binance mid (best_bid + best_ask) / 2 for the
    Coinbase symbol's underlying. Cached 30s to stay well under Binance's
    public-API rate limit (1200 req/min). Returns None on any failure —
    the caller must treat 'no data' as permissive (don't gate)."""
    bsym = binance_symbol_for(coinbase_symbol)
    if not bsym:
        return None
    now = time.time()
    cached = _CACHE.get(bsym)
    if cached and now - cached[0] < _CACHE_TTL_SECS:
        return cached[1]
    try:
        url = f"https://api.binance.com/api/v3/ticker/bookTicker?symbol={bsym}"
        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": "silver-swing/1.0"})
        if r.status_code != 200:
            return None
        data = r.json()
        bid = float(data.get("bidPrice") or 0)
        ask = float(data.get("askPrice") or 0)
        if bid <= 0 or ask <= 0 or ask < bid:
            return None
        mid = (bid + ask) / 2.0
        _CACHE[bsym] = (now, mid)
        return mid
    except Exception:
        return None


def crossex_gate_ok(
    coinbase_symbol: str,
    coinbase_mark: float,
    max_divergence_pct: float,
) -> tuple[bool, Optional[float]]:
    """Return (ok, divergence_pct). ok=False means the arm should be blocked
    because Coinbase price diverges too far from Binance fair value.

    Permissive-default: if Binance data unavailable OR symbol isn't mapped,
    returns (True, None) — the gate simply doesn't apply.
    """
    if coinbase_mark <= 0:
        return (True, None)
    ref = binance_mid_price(coinbase_symbol)
    if ref is None or ref <= 0:
        return (True, None)
    div_pct = abs(coinbase_mark - ref) / ref * 100.0
    return (div_pct <= max_divergence_pct, div_pct)
