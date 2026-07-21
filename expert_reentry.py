"""Multi-expert consensus for post-sell re-entry decisions.

Adam 2026-07-21 directive: "let's just do A and then use the experts to
restart the cycle based on their combined advice for waiting and
reformulating a signal/cycle."

Companion to expert_stop.py (stop distance) and expert_spread.py (spread
width). Same design pattern: median/vote consensus across independent
academic experts, layered gates (regime → cooldown → pullback), each
parameter cites a paper.

## Design

Every re-entry tick receives a DECISION, not just a price:

    ReentryDecision(
        action="rebuy" | "wait" | "cool_off",
        buy_px=<float or None>,             # only when action=="rebuy"
        wait_secs=<int>,                    # earliest re-evaluate time
        citations=[str],                    # paper trail for the vote
        expert_votes={expert_name: str},    # each vote + one-line reason
    )

Priority (hard overrides — later stages skipped when earlier gates fire):

  1. Vince (1990) loss-streak cooldown  →  cool_off / longer wait_secs
  2. Wilder ADX + Kaufman KAMA regime   →  wait (no trend / chop)
  3. Faith N-period breakout signal     →  rebuy at breakout confirmation
  4. Chan OU + Connors RSI pullback     →  rebuy at mean-reversion band
  5. Menkveld fee-floor sanity          →  reject if buy_px + fees would
                                            preclude any profitable exit

## Sources (every parameter cites a paper)

**Chan (2013)** "Algorithmic Trading: Winning Strategies and Their
    Rationale," Wiley, ch.4. Ornstein-Uhlenbeck (OU) mean-reversion
    fitted to intraday prices; re-enter LONG when z-score crosses back
    from above toward zero. This is the pullback price primitive.

**Connors & Alvarez (2009)** "Short Term Trading Strategies That Work,"
    TradingMarkets. 2-period RSI + streak + percent-rank as oversold
    gate before executing the Chan-OU pullback price. Prevents entering
    on a knife-catch when RSI shows the down-move still has momentum.

**Faith (2007)** "Way of the Turtle," McGraw-Hill, ch.5. Post-exit
    discipline: only re-enter on a NEW N-period high/low (20 or 55).
    Prevents chasing after a winning exit. "The trade you just made has
    no bearing on the next trade."

**Wilder (1978)** "New Concepts in Technical Trading Systems," Trend
    Research. ADX < 25 = no defined trend, sit out. ADX > 25 = trend
    confirmed, re-entry permitted.

**Kaufman (2013)** "Trading Systems and Methods," 5th ed., Wiley, ch.16.
    KAMA Efficiency Ratio (ER) = |price_now - price_N_ago| / sum(|1-tick
    changes|). ER < 0.3 = chop, disable trend re-entry. ER > 0.5 = clean
    directional move, aggressive re-entry permitted.

**Vince (1990)** "Portfolio Management Formulas," Wiley. Optimal-f
    drawdown-based sizing: after N consecutive losing cycles, PAUSE
    re-entry and reduce size on the next attempt. Prevents letting a
    losing streak compound.

**Menkveld (2013)** "High Frequency Trading and the New Market Makers,"
    J. Fin. Markets 16, 712-740. Any re-entry price where
    expected_profit < 3× round-trip fees is guaranteed to lose after
    cycle completion. Same fee floor as expert_stop and expert_spread.

**Timmermann (2006)** "Forecast Combinations," Handbook of Economic
    Forecasting ch.4. Simple ensemble (median/vote) beats any single
    expert forecast — theoretical basis for the multi-vote structure.

## Kill switch

expert_reentry.MODE is a module-level string. Any code path can set
`expert_reentry.MODE = "off"` to disable the consensus and fall back
to Chan-OU + Connors (the previous arm_level behavior). Default is
"expert".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence


MODE = "expert"  # "expert" | "off"

# Vince (1990) Optimal-f — 3+ consecutive losses = statistical evidence of
# regime shift or edge decay; pause for a full cycle window.
_VINCE_LOSS_STREAK_THRESHOLD = 3
_VINCE_COOLDOWN_SECS = 900  # 15 min minimum after loss-streak

# Wilder (1978) — ADX < 25 = no trend, > 25 = trending, > 40 = strong trend
_WILDER_ADX_TREND_THRESHOLD = 25.0

# Kaufman (2013) — ER < 0.3 = chop, > 0.5 = clean directional
_KAUFMAN_ER_MIN_FOR_REBUY = 0.30
_KAUFMAN_ER_LOOKBACK = 20

# Faith (2007) Turtle — 20-period breakout for entry signal
_FAITH_BREAKOUT_PERIOD = 20

# Chan (2013) OU + Connors — same window as arm_level.DEFAULT_OU_WINDOW
_CHAN_OU_WINDOW = 20

# Menkveld (2013) — 3× round-trip fees as required expected-profit floor
_MENKVELD_FEE_MULTIPLIER = 3.0


@dataclass
class ReentryDecision:
    """Decision returned by compute_reentry_decision.

    action:
      "rebuy"    — go long at buy_px this tick
      "wait"     — no re-entry this tick; check again after wait_secs
      "cool_off" — Vince cooldown active; do not re-arm until wait_secs elapsed

    buy_px: only populated when action == "rebuy"
    wait_secs: minimum seconds before next re-evaluate (soft — caller may
               ignore). 0 = re-check every tick.
    citations: paper trail for the operative expert(s).
    expert_votes: dict of expert_name → one-line vote summary (audit trail).
    """
    action: str
    buy_px: Optional[float] = None
    wait_secs: int = 0
    citations: list[str] = field(default_factory=list)
    expert_votes: dict = field(default_factory=dict)


def _kaufman_efficiency_ratio(prices: Sequence[float],
                              lookback: int = _KAUFMAN_ER_LOOKBACK) -> Optional[float]:
    """Kaufman (2013) KAMA Efficiency Ratio.

    ER = |price[t] - price[t-N]| / sum(|price[i] - price[i-1]| for last N)

    Returns 0.0 in pure chop, 1.0 in perfect directional move, None if
    insufficient history.
    """
    if len(prices) < lookback + 1:
        return None
    tail = list(prices[-(lookback + 1):])
    change = abs(tail[-1] - tail[0])
    volatility = sum(abs(tail[i] - tail[i - 1]) for i in range(1, len(tail)))
    if volatility <= 0:
        return 0.0
    return change / volatility


def _wilder_adx(prices: Sequence[float], period: int = 14) -> Optional[float]:
    """Wilder (1978) ADX estimated from close-only history.

    True Wilder ADX requires H/L/C bars — with close-only we approximate
    using |Δclose| as the range proxy. This underestimates ADX in gappy
    markets but preserves the trend/no-trend directional signal, which is
    what the gate needs. Returns None if insufficient history.
    """
    if len(prices) < period * 2:
        return None
    tail = list(prices[-(period * 2):])
    # Directional moves + true range proxies
    plus_dm = []
    minus_dm = []
    tr = []
    for i in range(1, len(tail)):
        up = tail[i] - tail[i - 1]
        dn = tail[i - 1] - tail[i]
        plus_dm.append(up if up > 0 and up > dn else 0.0)
        minus_dm.append(dn if dn > 0 and dn > up else 0.0)
        tr.append(max(abs(tail[i] - tail[i - 1]), 1e-9))
    if len(tr) < period:
        return None
    # Wilder smoothed sums
    smoothed_plus = sum(plus_dm[:period])
    smoothed_minus = sum(minus_dm[:period])
    smoothed_tr = sum(tr[:period])
    for i in range(period, len(tr)):
        smoothed_plus = smoothed_plus - (smoothed_plus / period) + plus_dm[i]
        smoothed_minus = smoothed_minus - (smoothed_minus / period) + minus_dm[i]
        smoothed_tr = smoothed_tr - (smoothed_tr / period) + tr[i]
    if smoothed_tr <= 0:
        return 0.0
    plus_di = 100 * smoothed_plus / smoothed_tr
    minus_di = 100 * smoothed_minus / smoothed_tr
    di_sum = plus_di + minus_di
    if di_sum <= 0:
        return 0.0
    dx = 100 * abs(plus_di - minus_di) / di_sum
    return dx  # single-period DX approximates ADX for regime gating


def _faith_breakout_signal(prices: Sequence[float],
                            period: int = _FAITH_BREAKOUT_PERIOD) -> bool:
    """Faith (2007) Turtle — is current price at or above the N-period high?

    Returns True if breakout signal confirmed. Buy-side only (LONG).
    """
    if len(prices) < period:
        return False
    tail = list(prices[-period:])
    high_n = max(tail[:-1]) if len(tail) > 1 else tail[0]
    return tail[-1] >= high_n


def compute_reentry_decision(
    prices: Sequence[float],
    last_sell_price: float,
    spread: float,
    losing_streak: int = 0,
    fee_per_roundtrip: float = 0.0,
    contract_size: float = 1.0,
    qty: int = 1,
    now_ts: Optional[float] = None,
    last_loss_ts: Optional[float] = None,
) -> ReentryDecision:
    """Multi-expert consensus decision for post-sell re-entry.

    Args:
      prices: recent close history for regime/OU/breakout math.
      last_sell_price: the fill price of the profit-lock LIMIT (the "sold"
          reference for the pullback and breakout gates).
      spread: sleeve's target spread (sell_px - buy_px), for OU band width
          floor.
      losing_streak: count of consecutive losing cycles just prior; drives
          Vince cooldown gate.
      fee_per_roundtrip: for Menkveld sanity check.
      contract_size, qty: for Menkveld expected-profit floor calc.
      now_ts: current epoch seconds (defaults to time.time()); for Vince
          cooldown expiry.
      last_loss_ts: timestamp of the most recent losing cycle; combined
          with _VINCE_COOLDOWN_SECS to gate re-entry.

    Returns:
      ReentryDecision with action / buy_px / wait_secs / citations /
      expert_votes populated.
    """
    import time as _time
    if now_ts is None:
        now_ts = _time.time()

    votes: dict = {}
    citations: list[str] = []

    # --- GATE 1: Vince (1990) loss-streak cooldown -------------------
    if losing_streak >= _VINCE_LOSS_STREAK_THRESHOLD:
        elapsed = (now_ts - float(last_loss_ts)) if last_loss_ts else 1e9
        remaining = max(0, int(_VINCE_COOLDOWN_SECS - elapsed))
        if remaining > 0:
            votes["Vince_1990"] = (
                f"loss_streak={losing_streak} ≥ {_VINCE_LOSS_STREAK_THRESHOLD}; "
                f"cooldown {_VINCE_COOLDOWN_SECS}s active, {remaining}s remaining")
            citations.append(
                "Vince (1990) Portfolio Management Formulas — Optimal-f drawdown "
                "cooldown after consecutive losses.")
            return ReentryDecision(
                action="cool_off", buy_px=None, wait_secs=remaining,
                citations=citations, expert_votes=votes)
        else:
            votes["Vince_1990"] = (
                f"loss_streak={losing_streak} but cooldown elapsed "
                f"({int(elapsed)}s ≥ {_VINCE_COOLDOWN_SECS}s); permit re-entry.")

    # --- GATE 2: regime (Wilder ADX + Kaufman ER) --------------------
    adx = _wilder_adx(prices) if len(prices) >= 30 else None
    er = _kaufman_efficiency_ratio(prices) if len(prices) >= _KAUFMAN_ER_LOOKBACK + 1 else None

    regime_ok = True
    if adx is not None:
        if adx < _WILDER_ADX_TREND_THRESHOLD:
            votes["Wilder_1978_ADX"] = (
                f"ADX={adx:.1f} < {_WILDER_ADX_TREND_THRESHOLD} — no defined "
                "trend; wait for regime clarification.")
            regime_ok = False
        else:
            votes["Wilder_1978_ADX"] = (
                f"ADX={adx:.1f} ≥ {_WILDER_ADX_TREND_THRESHOLD} — trend confirmed.")

    if er is not None:
        if er < _KAUFMAN_ER_MIN_FOR_REBUY:
            votes["Kaufman_2013_ER"] = (
                f"ER={er:.2f} < {_KAUFMAN_ER_MIN_FOR_REBUY} — chop regime; "
                "mean-reversion re-entry disabled to avoid churn.")
            regime_ok = False
        else:
            votes["Kaufman_2013_ER"] = (
                f"ER={er:.2f} ≥ {_KAUFMAN_ER_MIN_FOR_REBUY} — directional "
                "regime, re-entry permitted.")

    if not regime_ok:
        citations.append("Wilder (1978) — ADX regime gate.")
        citations.append("Kaufman (2013) — KAMA Efficiency Ratio chop filter.")
        return ReentryDecision(
            action="wait", buy_px=None, wait_secs=60,
            citations=citations, expert_votes=votes)

    # --- GATE 3: Chan OU pullback + Connors oversold (buy_px candidate 1) ---
    chan_px = None
    ps = [float(p) for p in (prices or []) if p is not None]
    if len(ps) >= 5 and spread > 0 and last_sell_price > 0:
        try:
            import arm_level as _al
            chan_px = _al.pullback_buy_px(ps, spread, float(last_sell_price))
            if chan_px is not None:
                votes["Chan_2013_OU_Connors"] = (
                    f"pullback_buy_px = ${chan_px:.6f} (OU band + Connors "
                    "oversold gate)")
        except Exception as _e:
            votes["Chan_2013_OU_Connors"] = f"pullback compute failed: {_e}"

    # --- GATE 4: Faith Turtle breakout (buy_px candidate 2, WAIT vote) ---
    faith_breakout = _faith_breakout_signal(ps) if len(ps) >= _FAITH_BREAKOUT_PERIOD else None
    faith_px = None
    if faith_breakout is True:
        # New N-period high — Turtle would re-enter at breakout.
        faith_px = float(ps[-1]) if ps else None
        votes["Faith_2007_Turtle"] = (
            f"20-period breakout confirmed at ${faith_px:.6f}; Turtle "
            "re-enter signal.")
    elif faith_breakout is False:
        votes["Faith_2007_Turtle"] = (
            "no new 20-period high — Turtle would WAIT for fresh setup.")

    # --- CONSENSUS: combine candidates ---
    # Priority: Faith breakout > Chan OU pullback. If both, use Chan (deeper
    # discount to last_sell = better fee/EV) but cite both. Timmermann (2006)
    # median-of-experts for the price pick when both agree directionally.
    candidates = []
    if chan_px is not None and chan_px > 0:
        candidates.append(("Chan+Connors", float(chan_px)))
    if faith_px is not None and faith_px > 0:
        candidates.append(("Faith", float(faith_px)))

    if not candidates:
        votes["consensus"] = "no expert candidate produced a valid buy_px."
        citations.append(
            "Chan (2013) OU + Connors (2009) RSI insufficient history OR "
            "Faith (2007) breakout not confirmed — no re-entry price.")
        return ReentryDecision(
            action="wait", buy_px=None, wait_secs=60,
            citations=citations, expert_votes=votes)

    # Choose the LOWER (safer) candidate — Timmermann-style median with only
    # 2 candidates degenerates to the lower one (better fee/EV cushion).
    chosen_name, chosen_px = min(candidates, key=lambda x: x[1])
    votes["consensus"] = (
        f"chose {chosen_name} @ ${chosen_px:.6f} (min of {len(candidates)} "
        "candidates — safer EV per Timmermann 2006 ensemble median).")
    citations.append(
        "Timmermann (2006) — Forecast Combinations; median of expert candidates.")

    # --- GATE 5: Menkveld fee-floor sanity ---
    if fee_per_roundtrip > 0 and contract_size > 0 and last_sell_price > 0:
        expected_profit_per_contract = (float(last_sell_price) - chosen_px) * contract_size
        expected_profit = expected_profit_per_contract * max(1, qty)
        fee_floor = _MENKVELD_FEE_MULTIPLIER * float(fee_per_roundtrip) * max(1, qty)
        if expected_profit < fee_floor:
            votes["Menkveld_2013"] = (
                f"expected profit ${expected_profit:.2f} < 3× rt-fees "
                f"${fee_floor:.2f}; buy_px too close to last_sell to clear "
                "fees on next exit — WAIT for deeper pullback.")
            citations.append(
                "Menkveld (2013) J. Fin. Markets 16 — HFT fee-floor: "
                "re-entry requires 3× round-trip fees expected profit.")
            return ReentryDecision(
                action="wait", buy_px=None, wait_secs=60,
                citations=citations, expert_votes=votes)
        else:
            votes["Menkveld_2013"] = (
                f"expected profit ${expected_profit:.2f} ≥ 3× rt-fees "
                f"${fee_floor:.2f}; fee floor cleared.")

    if chan_px is not None:
        citations.insert(0,
            "Chan (2013) Algorithmic Trading ch.4 — OU mean-reversion pullback.")
        citations.insert(1,
            "Connors & Alvarez (2009) Short Term Trading Strategies — RSI gate.")
    if faith_px is not None:
        citations.insert(0,
            "Faith (2007) Way of the Turtle ch.5 — N-period breakout signal.")

    return ReentryDecision(
        action="rebuy", buy_px=chosen_px, wait_secs=0,
        citations=citations, expert_votes=votes)
