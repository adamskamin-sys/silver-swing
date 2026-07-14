"""
bollinger_squeeze.py — volatility-REGIME timing signal, built the way its author
prescribes. EXPERIMENT: unproven until backtested; do not take live before it
shows an expectancy lift.

Source: John Bollinger, "Bollinger on Bollinger Bands" (2001) — BandWidth, %b,
and The Squeeze. TWO of Bollinger's own rules are baked in on purpose:
  1. The Squeeze gives NO direction — a big move is coming, not which way. So this
     module NEVER emits a direction from the squeeze alone. Beware "head fakes."
  2. Confirm with an INDEPENDENT, non-price indicator (Bollinger pairs bands with
     volume, never two price-derived indicators). Direction here comes from the
     existing trend gate + a volume/expansion confirm the caller passes in.

Intended use in this system: the squeeze is a TIMING filter. Only in a confirmed
uptrend (your Stage-1 trend gate) does a squeeze-release-up become a high-quality
long entry. It composes with the trend gate; it does not replace it.
"""
from statistics import fmean, pstdev


def bollinger_bands(closes, n=20, k=2.0):
    """Bollinger defaults: 20-period SMA, ±2 standard deviations."""
    if len(closes) < n:
        return None
    window = closes[-n:]
    mid = fmean(window)
    sd = pstdev(window)
    return mid - k * sd, mid, mid + k * sd


def bandwidth(closes, n=20, k=2.0):
    b = bollinger_bands(closes, n, k)
    if not b or b[1] == 0:
        return None
    lo, mid, hi = b
    return (hi - lo) / mid            # normalized band width


def percent_b(closes, n=20, k=2.0):
    b = bollinger_bands(closes, n, k)
    if not b:
        return None
    lo, mid, hi = b
    if hi == lo:
        return None
    return (closes[-1] - lo) / (hi - lo)


def is_squeeze(closes, n=20, k=2.0, lookback=126, margin=1.02):
    """Bollinger's Squeeze: BandWidth at/near its lowest in `lookback` bars.
    lookback=126 ~ 6 months of daily bars; SCALE to your timeframe."""
    if len(closes) < n + lookback:
        return False
    widths = []
    for i in range(lookback):
        w = bandwidth(closes[:len(closes) - i], n, k)
        if w is not None:
            widths.append(w)
    if not widths:
        return False
    cur = widths[0]
    return cur <= min(widths) * margin      # within `margin` of the lookback low


def squeeze_long_signal(closes, *, trend_is_up, volume_confirms, n=20, k=2.0, lookback=126):
    """Expert-correct long trigger. Returns (fire: bool, why: str).

    Fires ONLY when ALL hold:
      - we were in a squeeze (coiled),
      - bands are now EXPANDING and price broke the upper band (%b > 1) => the move
        has chosen 'up' (direction from the breakout, NOT from the squeeze),
      - trend_is_up: the Stage-1 trend gate agrees (direction confirm #1),
      - volume_confirms: an INDEPENDENT non-price confirm (Bollinger's rule).
    Any missing leg -> no fire. The squeeze alone NEVER fires.
    """
    if len(closes) < n + lookback:
        return False, "insufficient history"
    coiled_recently = is_squeeze(closes[:-1], n, k, lookback)   # was squeezing
    pb = percent_b(closes, n, k)
    now_w = bandwidth(closes, n, k)
    prev_w = bandwidth(closes[:-1], n, k)
    if now_w is None or prev_w is None or pb is None:
        return False, "bands unavailable"
    expanding = now_w > prev_w
    broke_up = pb > 1.0                                          # closed above upper band
    if not coiled_recently:
        return False, "no prior squeeze (nothing coiled)"
    if not (expanding and broke_up):
        return False, "squeeze not yet released upward (no direction — beware head-fake)"
    if not trend_is_up:
        return False, "upper-band break but trend gate disagrees — refuse (could be head-fake)"
    if not volume_confirms:
        return False, "no independent volume confirm (Bollinger's rule)"
    return True, "squeeze released up: expansion + upper-band break, trend + volume confirm"
