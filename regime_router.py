"""Regime router — pick strategy adjustments per product per session.

Adam 2026-07-15 priority list #9: "regime router — pick strategy variant
per product per session."

Doesn't replace the existing tools (classify_regime lives in regime.py,
adaptive spread lives in expert_spread.py). What this module does is
MAP regime → parameter adjustments so the same trading engine behaves
differently in trend / mean-revert / chop regimes.

Academic grounding:
  * Kaufman AMA (Efficiency Ratio) — regime discrimination via
    directional-motion / total-motion ratio. Already in regime.py.
  * Kaminski-Lo (2011) "Which trend is your friend?" — trend systems
    lose money in mean-revert regimes; mean-revert systems lose
    money in trends. Explicit regime routing IS the alpha.
  * Menkveld (2013) — HFT market makers profit most in mean-revert
    regimes (many round-trips at tight spread); tighten spread there.
  * Chan "Algorithmic Trading" (2013) ch.5 — pause during chop
    (no regime edge) rather than force a trade.

Public API:
  regime_adjustments(regime_classification) -> dict
    Takes the output of regime.classify_regime and returns:
      * gamma_multiplier  — for Avellaneda-Stoikov (higher γ = tighter
                            spread, more cycles — favored in mean_revert)
      * qty_multiplier    — 0.0-1.0 scaling on qty (chop → downscale)
      * should_arm        — hard 'skip arm this tick' when unclear
      * reason            — human-readable explanation for the log

Fail-safe: unknown regime → returns neutral (1.0 / 1.0 / True / "").
Never over-restricts; caller can always fall through to legacy logic.
"""

from __future__ import annotations

from typing import Optional


# Named constants — every value has a rationale, no arbitrary magic.

# In a trend regime, wider spread reduces adverse selection cost
# (Cartea-Jaimungal 2015 §8.4). Lower AS γ = wider spread → gamma_mult<1.
_GAMMA_MULT_TREND = 0.5

# In mean_revert, tighter spread = more cycles (Menkveld 2013). Higher AS
# γ = tighter spread → gamma_mult>1.
_GAMMA_MULT_MEAN_REVERT = 1.5

# In chop, neither trend nor mean-revert edge is present. Chan 2013 ch.5:
# stand aside. Downscale qty aggressively OR skip arm entirely.
_QTY_MULT_CHOP = 0.25
_SHOULD_ARM_CHOP = False  # skip arms in confirmed chop

# Vol state amplifies the base adjustment (Bollerslev GARCH). In stressed
# vol, everyone widens; in calm vol, tighten further.
_VOL_STRESSED_QTY_MULT = 0.6
_VOL_CALM_QTY_BOOST = 1.15


def regime_adjustments(regime_result: Optional[dict]) -> dict:
    """Map regime.classify_regime output to strategy parameter adjustments.

    Returns a dict with keys:
      gamma_multiplier: float in [0.3, 2.0]
        Multiplier for Avellaneda-Stoikov γ. Higher = tighter spread.
      qty_multiplier: float in [0.0, 1.5]
        Scaling on sleeve qty. Applied before Kelly + correlation drag.
      should_arm: bool
        False = skip the arm entirely (chop / unclear regime).
      reason: str
        One-line explanation for the trade log.
      inputs: dict
        The regime + vol_state + er/hurst that drove the decision.
    """
    if regime_result is None:
        return {
            "gamma_multiplier": 1.0, "qty_multiplier": 1.0,
            "should_arm": True,
            "reason": "no regime data — neutral defaults",
            "inputs": {},
        }
    regime = str(regime_result.get("regime") or "unknown")
    vol_state = str(regime_result.get("vol_state") or "normal")
    er = regime_result.get("efficiency_ratio")

    # Base adjustments from regime
    if regime == "trend":
        gamma_mult = _GAMMA_MULT_TREND
        qty_mult = 1.0
        should_arm = True
        reason = "trend regime — wider spread (Cartea-Jaimungal adverse-selection)"
    elif regime == "mean_revert":
        gamma_mult = _GAMMA_MULT_MEAN_REVERT
        qty_mult = 1.0
        should_arm = True
        reason = "mean_revert regime — tighter spread, max cycles (Menkveld 2013)"
    elif regime == "chop":
        gamma_mult = 1.0
        qty_mult = _QTY_MULT_CHOP
        should_arm = _SHOULD_ARM_CHOP
        reason = "chop regime — no directional edge; skip arm (Chan 2013 ch.5)"
    else:
        gamma_mult = 1.0
        qty_mult = 1.0
        should_arm = True
        reason = f"regime={regime} — neutral defaults"

    # Vol-state amplifier
    if vol_state == "stressed":
        qty_mult *= _VOL_STRESSED_QTY_MULT
        reason += "; stressed vol → smaller qty"
    elif vol_state == "calm":
        qty_mult = min(1.5, qty_mult * _VOL_CALM_QTY_BOOST)
        reason += "; calm vol → slightly larger qty"

    # Bounds
    gamma_mult = max(0.3, min(2.0, gamma_mult))
    qty_mult = max(0.0, min(1.5, qty_mult))

    return {
        "gamma_multiplier": round(gamma_mult, 3),
        "qty_multiplier": round(qty_mult, 3),
        "should_arm": bool(should_arm),
        "reason": reason,
        "inputs": {
            "regime": regime,
            "vol_state": vol_state,
            "efficiency_ratio": er,
            "hurst": regime_result.get("hurst"),
            "autocorr_lag1": regime_result.get("autocorr_lag1"),
        },
    }
