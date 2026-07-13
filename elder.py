"""Alexander Elder Triple Screen — multi-timeframe re-entry direction gate.

References
----------
Elder, Alexander. *Trading for a Living: Psychology, Trading Tactics, Money
Management*. Wiley, 1993. Ch. 9 "The Triple Screen Trading System" —
original three-screen framework (weekly MACD-histogram + daily oscillator +
intraday trigger).

Elder, Alexander. *Come Into My Trading Room: A Complete Guide to Trading*.
Wiley, 2002. Ch. 8 "The New Triple Screen" — refined with Force Index and
Impulse System.

Purpose
-------
Answer "should this re-entry look for a BUY at all, given the trend?" A
mean-reversion re-entry algorithm that ignores the higher-TF trend is what
loses money in structural downtrends (Van Tharp's canonical failure mode).
Triple Screen filters BUY signals so we only fire when the higher-TF trend
is not decisively down AND the intermediate-TF oscillator is oversold AND
the lower-TF confirms a turn.

Adapted for the bot's tick-rate data:
- Screen 1 "long-tide"  — 240-bar MACD-histogram slope (Elder's weekly).
- Screen 2 "medium-wave" — 60-bar stochastic %K (Elder's daily oscillator).
- Screen 3 "short-ripple" — last 15 bars, break of prior swing low (trigger).

Each screen returns pass/fail; overall verdict is `buy_ok` iff Screens 1+2
pass (Screen 3 is TIMING; not a hard block for the arm event but recorded).
"""
from __future__ import annotations

from typing import Optional, Sequence


# -- Screen 1: higher-TF trend via MACD histogram slope --------------------

def _ema(vals: Sequence[float], period: int) -> list[float]:
    if not vals:
        return []
    k = 2.0 / (period + 1)
    out = [float(vals[0])]
    for v in vals[1:]:
        out.append(float(v) * k + out[-1] * (1 - k))
    return out


def macd_histogram(prices: Sequence[float],
                   fast: int = 12, slow: int = 26, signal: int = 9) -> list[float]:
    """Standard MACD histogram = (EMA_fast − EMA_slow) − EMA_signal(diff).
    Elder's Screen 1 direction gate reads the histogram's slope."""
    if len(prices) < slow + signal:
        return []
    ema_f = _ema(prices, fast)
    ema_s = _ema(prices, slow)
    macd_line = [f - s for f, s in zip(ema_f, ema_s)]
    signal_line = _ema(macd_line, signal)
    return [m - s for m, s in zip(macd_line, signal_line)]


def screen1_long_tide(prices: Sequence[float], window: int = 240) -> dict:
    """Higher-TF trend gate — the 'long tide' in Elder's ocean metaphor.
    Returns direction (up/down/flat) via MACD histogram slope over the last
    2 bars of the 240-bar window."""
    if len(prices) < 60:
        return {"pass_buy": False, "direction": "unknown",
                "reason": "insufficient history"}
    tail = prices[-window:] if len(prices) >= window else list(prices)
    hist = macd_histogram(tail)
    if len(hist) < 3:
        return {"pass_buy": False, "direction": "unknown",
                "reason": "MACD histogram not resolved"}
    # Slope over last 2 samples. Elder (1993): buy only when the histogram
    # is rising (slope > 0), even if the histogram itself is negative — the
    # slope catches the turn before the sign flip.
    slope = hist[-1] - hist[-2]
    if slope > 0:
        return {"pass_buy": True, "direction": "up",
                "reason": f"MACD hist slope +{slope:.5f}"}
    return {"pass_buy": False, "direction": "down",
            "reason": f"MACD hist slope {slope:.5f} — long tide against buy"}


# -- Screen 2: intermediate-TF oscillator (stochastic %K) ------------------

def stochastic_k(prices: Sequence[float], period: int = 14) -> Optional[float]:
    """Standard %K over lookback: (close − lowest_low) / (highest_high − lowest_low)."""
    if len(prices) < period:
        return None
    window = prices[-period:]
    hi = max(window)
    lo = min(window)
    if hi == lo:
        return 50.0
    return (float(prices[-1]) - lo) / (hi - lo) * 100.0


def screen2_medium_wave(prices: Sequence[float], window: int = 60,
                        oversold: float = 30.0) -> dict:
    """Intermediate-TF oscillator gate — the 'medium wave'.
    Elder (1993) — buy when the daily oscillator is oversold AGAINST the
    long-tide up-slope. We use %K with default oversold = 30 (Elder's
    canonical band)."""
    if len(prices) < window:
        return {"pass_buy": False, "reason": "insufficient history"}
    k = stochastic_k(prices[-window:], period=14)
    if k is None:
        return {"pass_buy": False, "reason": "stochastic not resolved"}
    if k <= oversold:
        return {"pass_buy": True, "stochastic_k": round(k, 2),
                "reason": f"%K {k:.1f} <= oversold {oversold}"}
    return {"pass_buy": False, "stochastic_k": round(k, 2),
            "reason": f"%K {k:.1f} > oversold {oversold} — not yet"}


# -- Screen 3: lower-TF trigger --------------------------------------------

def screen3_short_ripple(prices: Sequence[float], window: int = 15) -> dict:
    """Lower-TF trigger — the 'short ripple' (Elder). Fires when the
    current bar takes out the prior swing low (marks acceptance of a
    turn). We treat this as TIMING info only — it doesn't gate arming
    but tells the sleeve WHEN a buy is timeliest."""
    if len(prices) < window:
        return {"trigger": False, "reason": "insufficient history"}
    tail = list(prices[-window:])
    prior_low = min(tail[:-1])
    last = float(tail[-1])
    # 'Ripple' turn: last bar bounces (>prior low) after having tested it.
    tested_low = min(tail[-3:]) <= prior_low if len(tail) >= 3 else False
    bounced = last > prior_low
    fired = tested_low and bounced
    return {"trigger": fired, "prior_low": prior_low, "last": last,
            "reason": ("bounce off prior low" if fired
                       else "no clean ripple trigger yet")}


# -- Combined verdict ------------------------------------------------------

def triple_screen(prices: Sequence[float]) -> dict:
    """Full Elder Triple Screen. buy_ok requires Screens 1 & 2 to pass.
    Screen 3 is reported for entry-timing but is not a hard block — the
    bot's own tick loop already provides fine-grained timing."""
    s1 = screen1_long_tide(prices)
    s2 = screen2_medium_wave(prices)
    s3 = screen3_short_ripple(prices)
    buy_ok = bool(s1.get("pass_buy") and s2.get("pass_buy"))
    reasons: list[str] = []
    if not s1.get("pass_buy"):
        reasons.append(f"Screen 1: {s1.get('reason')}")
    if not s2.get("pass_buy"):
        reasons.append(f"Screen 2: {s2.get('reason')}")
    return {
        "buy_ok": buy_ok,
        "screen1": s1,
        "screen2": s2,
        "screen3": s3,
        "blocked_by": reasons,
        "citation": "Elder 1993 Trading for a Living Ch. 9; 2002 CIMTR Ch. 8",
    }
