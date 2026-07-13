"""Cross-asset correlation gate.

Adam's ask: 'don't arm silver longs when copper just dropped 3% in the
last hour. Metals correlate; the crash usually spreads.'

Why: correlated assets crash together. If oil dumps 5%, natgas will
probably follow within 30-60 min. If BTC breaks a key level, ETH/SOL
usually confirm. Adding a new long into a correlated crash means
buying right before the follow-through hits your product.

How it works:
    - Assets are grouped into CORRELATION_FAMILIES (metals, energy,
      crypto, indices). Within a family, if any peer has dropped more
      than crash_threshold_pct in the last window_secs, block new
      long arms for the whole family until the peer recovers or the
      window rolls off.
    - Peer price history comes from the store's snapshot data (which
      each SwingTrader writes on its snapshot interval). No new API
      calls — we reuse what the bot is already fetching.
    - Gate checked from _sleeve_arm; skip event logged so post-mortem
      can see 'this sleeve DECLINED to arm because copper was down 3.2%'.
"""

from __future__ import annotations

import time
from typing import Optional


# Product-symbol → family mapping. Extend as new products are added.
# Family names are shared with dashboard/public/app.js's assetClassOf.
CORRELATION_FAMILIES: dict[str, str] = {
    # Metals — silver, copper, platinum move together on industrial-metals
    # sentiment. Gold is a partial correlate (safe-haven pulls one way,
    # industrial pulls another) but grouped here for now.
    "SLR": "metals", "SLVR": "metals",
    "COPR": "metals", "CU": "metals",
    "PLAT": "metals", "PT": "metals",
    "GOLD": "metals", "GLD": "metals",
    # Energy — WTI oil, natgas, brent all crash together on OPEC/inventory
    # news. Nano oil (NOL) tracks WTI 1:1.
    "OIL": "energy", "NOL": "energy",
    "NGS": "energy", "NAT_GAS": "energy",
    # Crypto majors — BTC drives the whole complex. When BTC dumps, ETH
    # and SOL follow within minutes; ZEC/HYPE/etc. usually amplify.
    "BTC": "crypto_major", "BIT": "crypto_major",
    "ETH": "crypto_major",
    "SOL": "crypto_major",
    # Crypto perps — smaller caps that follow BTC but with more beta.
    "ZEC": "crypto_perp",
    "HYP": "crypto_perp", "HYPE": "crypto_perp",
    "XLM": "crypto_perp", "XLP": "crypto_perp",
    "NER": "crypto_perp", "NEAR": "crypto_perp",
    "SUI": "crypto_perp",
    "AVE": "crypto_perp",
    "ENA": "crypto_perp",
    "PEP": "crypto_perp",
}


def _family_of(symbol: str) -> Optional[str]:
    """Extract the family for a symbol like 'SLVR-27AUG26-CDE' or 'BIT-31JUL26-CDE'."""
    if not symbol:
        return None
    head = symbol.split("-")[0].upper()
    return CORRELATION_FAMILIES.get(head)


def _peer_pct_change(store, tenant: str, family: str, exclude_symbol: str,
                     window_secs: float) -> tuple[float, str | None]:
    """Return (worst_pct_change, worst_symbol) across all peers in the same
    family EXCLUDING the symbol we're about to arm. Negative = crash.
    Reads snapshot data the bot is already writing — no new API calls.
    Uses (last_mark - price_at_window_start) as the change. If no history
    is available, returns (0, None) — permissive default."""
    now = time.time()
    worst_pct = 0.0
    worst_sym = None
    for sym in store.list_symbols(tenant):
        if sym.startswith("__") or sym == exclude_symbol:
            continue
        if _family_of(sym) != family:
            continue
        try:
            snap = store.get_snapshot(tenant, sym) if hasattr(store, "get_snapshot") else None
        except Exception:
            snap = None
        if not snap:
            continue
        mark = float(snap.get("last_mark") or 0)
        # Rolling window: snap may include a `price_history` list of
        # (ts, price) tuples the SwingTrader can populate. If absent, fall
        # back to comparing mark against high_24h / low_24h as a rough proxy
        # for intra-day extent.
        history = snap.get("price_history") or []
        window_start_price = None
        for ts, px in history:
            try:
                ts_f = float(ts)
                if ts_f >= now - window_secs:
                    window_start_price = float(px)
                    break
            except (TypeError, ValueError):
                continue
        if window_start_price is None or window_start_price <= 0:
            continue
        pct = (mark - window_start_price) / window_start_price * 100.0
        if pct < worst_pct:  # more negative = worse crash
            worst_pct = pct
            worst_sym = sym
    return worst_pct, worst_sym


def rolling_correlation(store, tenant: str, symbol_a: str, symbol_b: str,
                        window_secs: float = 30 * 24 * 3600.0) -> Optional[float]:
    """Pearson correlation of price_history between two symbols over the
    last window_secs. Reads snapshot.price_history — no new API calls.

    Returns None if insufficient overlapping samples. Used to DISCOVER
    correlations at runtime, beyond the hardcoded CORRELATION_FAMILIES.
    Cross-asset macro shocks (BTC ↔ gold during a rate scare) create
    temporary correlations that the static family map misses.
    """
    import math
    def _history(sym):
        try:
            snap = store.get_snapshot(tenant, sym) if hasattr(store, "get_snapshot") else None
        except Exception:
            return []
        if not snap:
            return []
        return snap.get("price_history") or []
    ha = _history(symbol_a)
    hb = _history(symbol_b)
    if not ha or not hb:
        return None
    now = time.time()
    cutoff = now - window_secs
    def _samples(h):
        out = []
        for entry in h:
            try:
                ts = float(entry[0])
                px = float(entry[1])
            except (TypeError, ValueError, IndexError):
                continue
            if ts >= cutoff and px > 0:
                out.append((ts, px))
        return out
    sa = _samples(ha)
    sb = _samples(hb)
    if len(sa) < 10 or len(sb) < 10:
        return None
    # Align via nearest-timestamp lookup. Cheap N×log(M) — both lists are
    # small (bounded by snapshot history size).
    sb_sorted = sorted(sb, key=lambda p: p[0])
    sb_ts = [p[0] for p in sb_sorted]
    sb_px = [p[1] for p in sb_sorted]
    import bisect
    pairs = []
    for ts_a, px_a in sa:
        idx = bisect.bisect_left(sb_ts, ts_a)
        # nearest neighbor within 60s
        best = None
        for cand in (idx - 1, idx):
            if 0 <= cand < len(sb_ts):
                dt = abs(sb_ts[cand] - ts_a)
                if dt <= 60.0 and (best is None or dt < best[0]):
                    best = (dt, sb_px[cand])
        if best:
            pairs.append((px_a, best[1]))
    if len(pairs) < 10:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    n = len(pairs)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = math.sqrt(sum((xs[i] - mx) ** 2 for i in range(n)))
    dy = math.sqrt(sum((ys[i] - my) ** 2 for i in range(n)))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def discover_correlated_peers(store, tenant: str, symbol: str,
                              threshold: float = 0.6,
                              window_secs: float = 30 * 24 * 3600.0) -> list[tuple[str, float]]:
    """Return list of (peer_symbol, correlation) where |correlation| > threshold.
    Complements CORRELATION_FAMILIES — catches dynamic correlations across
    families that the hardcoded map wouldn't have.

    Cost: O(N) correlation computes over N products in the tenant. Cheap
    at our scale (< 30 products) but the caller should cache.
    """
    peers: list[tuple[str, float]] = []
    for sym in store.list_symbols(tenant):
        if sym.startswith("__") or sym == symbol:
            continue
        c = rolling_correlation(store, tenant, symbol, sym, window_secs=window_secs)
        if c is not None and abs(c) >= threshold:
            peers.append((sym, round(c, 3)))
    peers.sort(key=lambda p: -abs(p[1]))
    return peers


def peer_crash_check(store, tenant: str, symbol: str, side: str,
                     window_secs: float = 3600.0,
                     crash_threshold_pct: float = 3.0,
                     use_dynamic_correlation: bool = False,
                     correlation_threshold: float = 0.6) -> Optional[dict]:
    """Check if a peer has crashed more than crash_threshold_pct in the
    last window_secs. Returns a dict describing the peer crash if the
    arm should be BLOCKED, else None.

    Only gates BUY arms (fresh long entries). SELL arms are always allowed
    — if we already hold contracts, we may want to EXIT into a peer crash
    (that's what the trail is for), not be blocked from exiting.

    use_dynamic_correlation=True (opt-in): also check peers discovered via
    rolling correlation ≥ correlation_threshold, not just the hardcoded
    family map. Catches macro-shock cross-family correlations (e.g., BTC
    tanking after an FOMC surprise correlates with everything else that
    day, even though the static families don't group crypto with metals).
    """
    if side != "BUY":
        return None
    family = _family_of(symbol)
    # Static-family check (existing behavior).
    if family:
        worst_pct, worst_sym = _peer_pct_change(store, tenant, family,
                                                 exclude_symbol=symbol,
                                                 window_secs=window_secs)
        if worst_pct <= -abs(crash_threshold_pct):
            return {
                "family": family,
                "worst_peer": worst_sym,
                "worst_pct": round(worst_pct, 3),
                "window_secs": window_secs,
                "threshold_pct": crash_threshold_pct,
                "source": "static_family",
            }
    # Dynamic-correlation check (opt-in additional coverage).
    if use_dynamic_correlation:
        try:
            peers = discover_correlated_peers(
                store, tenant, symbol,
                threshold=correlation_threshold,
                window_secs=max(window_secs, 7 * 24 * 3600.0),  # need ≥ 7d for stable corr
            )
        except Exception:
            peers = []
        # For each strongly-correlated peer, check its recent pct change.
        # If the correlation is POSITIVE (co-moves) and the peer crashed,
        # our long is likely to follow — block.
        for peer_sym, corr in peers:
            if corr <= 0:
                continue  # only care about positive co-movement
            try:
                snap = store.get_snapshot(tenant, peer_sym)
            except Exception:
                snap = None
            if not snap:
                continue
            mark = float(snap.get("last_mark") or 0)
            history = snap.get("price_history") or []
            window_start_price = None
            now = time.time()
            for ts, px in history:
                try:
                    ts_f = float(ts)
                    if ts_f >= now - window_secs:
                        window_start_price = float(px)
                        break
                except (TypeError, ValueError):
                    continue
            if not window_start_price or window_start_price <= 0:
                continue
            pct = (mark - window_start_price) / window_start_price * 100.0
            if pct <= -abs(crash_threshold_pct):
                return {
                    "family": family or "dynamic",
                    "worst_peer": peer_sym,
                    "worst_pct": round(pct, 3),
                    "window_secs": window_secs,
                    "threshold_pct": crash_threshold_pct,
                    "correlation": corr,
                    "source": "dynamic_correlation",
                }
    return None
