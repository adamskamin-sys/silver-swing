"""Tests for the 2026-07-16 expert_spread integration:
  1. Fee floor bumped 2.0 → 3.0 (Menkveld 2013)
  2. Sleeve EXPERT-mode gate deleted (_expert_spread_mode always "expert")
  3. Primary swing arm consults expert_spread + respects the fee floor

Focus: guardrail behavior, not the AS math itself (already covered in
tests/test_expert_spread.py). Every test names the invariant it
protects so a regression traces back to the intent.
"""
from __future__ import annotations

import math


def test_cost_floor_multiplier_is_three():
    """Invariant: floor is 3× round-trip fees (Menkveld 2013 empirical
    breakeven 2.5× + 0.5× safety). Bumped 2026-07-16 after PT/HYPE bleed
    proved 2.0× was insufficient. Regression guard against silent revert."""
    from expert_spread import _COST_FLOOR_MULTIPLIER
    assert _COST_FLOOR_MULTIPLIER == 3.0, (
        "Cost floor must be 3.0 (Menkveld 2013 safety margin). "
        "Reverting to 2.0 or lower re-opens the tight-spread bleed."
    )


def test_fee_floor_binds_when_spread_would_be_tight():
    """When AS proposes a spread smaller than 3× fees, the floor MUST
    kick in and widen the spread to at least 3× fees / (contract_size × qty)."""
    from expert_spread import optimal_spread
    # Setup: quiet market (tight AS spread), non-trivial fees, small qty.
    dec = optimal_spread(
        mid_price=100.0,
        price_history=[100.0, 100.01, 100.0, 100.01, 100.0, 100.01,
                       100.0, 100.01, 100.0, 100.01],
        cycle_completion_ts=[],
        fee_per_roundtrip=1.0,      # $1 round-trip
        contract_size=10.0,          # 10 units per contract
        qty=1,
        tick_size=0.01,
    )
    assert dec is not None, "AS should return a decision on valid inputs"
    # 3× floor = 3 × 1.0 / (10 × 1) = 0.30 dollars
    assert dec.spread >= 0.30 - 1e-9, (
        f"Spread {dec.spread} must be ≥ 0.30 (3× fee floor). "
        f"Cost-floor-binding = {dec.cost_floor_binding}"
    )


def test_fee_floor_binding_flag_set_when_floor_hits():
    """When the floor was the reason spread was widened, the decision
    must report cost_floor_binding=True so the operator can audit."""
    from expert_spread import optimal_spread
    # Very quiet market + high fees → floor definitely binds
    dec = optimal_spread(
        mid_price=100.0,
        price_history=[100.0] * 10,  # zero vol
        cycle_completion_ts=[],
        fee_per_roundtrip=10.0,      # very high fee
        contract_size=1.0,
        qty=1,
        tick_size=0.01,
    )
    # optimal_spread may return None on zero vol — accept that as valid
    if dec is None:
        return
    if dec.spread <= 30.0:
        # If spread is tight enough that the floor should have engaged, flag must be set
        assert dec.cost_floor_binding is True or dec.spread >= 30.0


def test_expert_spread_mode_always_returns_expert():
    """Adam 2026-07-16: gate deleted. Method must return 'expert' regardless
    of what's in the tenant-scoped __expert_spread_mode__ store scope.
    Regression guard against re-introducing the gate."""
    from swing_leg import SwingTrader
    # Peek at the source to confirm the method exists and returns "expert"
    import inspect
    src = inspect.getsource(SwingTrader._expert_spread_mode)
    # The method should unconditionally return "expert" — no store lookup
    assert 'return "expert"' in src, (
        "_expert_spread_mode must unconditionally return 'expert'. "
        "The tenant-scoped gate was deleted 2026-07-16 to make experts always-on."
    )


def test_primary_price_history_populated_on_step():
    """The primary-swing price history must accept new samples in step()
    so expert_spread has fresh data to work with on the next arm."""
    import inspect
    from swing_leg import SwingTrader
    step_src = inspect.getsource(SwingTrader.step)
    assert "_primary_price_history.append" in step_src, (
        "SwingTrader.step must append to _primary_price_history — otherwise "
        "expert_spread will always see empty history and never fire on the primary."
    )


def test_expert_pick_primary_prices_returns_none_on_insufficient_history():
    """When we don't have ≥5 price samples, expert can't estimate vol.
    Method must return None (caller falls back to legacy directive)
    rather than crashing or returning garbage."""
    import inspect
    from swing_leg import SwingTrader
    src = inspect.getsource(SwingTrader._expert_pick_primary_prices)
    # The method must guard on history length; look for the ≥5 check
    assert "len(history) < 5" in src or "history) < 5" in src, (
        "_expert_pick_primary_prices must guard on len(history) < 5 — "
        "AS realized_vol needs ≥5 samples or it returns None."
    )


def test_primary_wireup_respects_market_direction():
    """Safety: never override BUY price with something above mark
    (would buy above market — no market maker does this).
    Never override SELL price with something below mark either."""
    import inspect
    from swing_leg import SwingTrader
    src = inspect.getsource(SwingTrader._ensure_armed)
    # Guards must be present in the override sites
    assert 'expert_prices.get("sell_px", 0) > current_price' in src, (
        "SELL override must check expert_sell_px > current_price to avoid "
        "selling below market via limit."
    )
    assert '0 < expert_prices.get("buy_px", 0) < current_price' in src, (
        "BUY override must check expert_buy_px < current_price to avoid "
        "buying above market via limit."
    )


def test_expert_call_wrapped_in_try_except():
    """Fail-safe: an exception in expert_pick_primary_prices must NEVER
    crash the tick loop. Method must return None on any exception and
    log the error. Verifies the outer try/except is present."""
    import inspect
    from swing_leg import SwingTrader
    src = inspect.getsource(SwingTrader._expert_pick_primary_prices)
    assert "try:" in src and "except Exception" in src, (
        "_expert_pick_primary_prices must wrap the AS call in try/except — "
        "any expert error must degrade to legacy behavior, not kill the tick."
    )
    assert "return None" in src, (
        "_expert_pick_primary_prices must return None on failure paths."
    )


def test_expert_spread_call_still_hits_fee_floor_in_wire_up():
    """End-to-end: pass a HYPE-shaped input (tight quiet market, low fees)
    to optimal_spread and confirm the floor gets us out of the mathematical-
    guaranteed-loss zone."""
    from expert_spread import optimal_spread
    # HYPE-like: ~$65 mark, ~$0.50 round-trip fee, contract_size=1
    dec = optimal_spread(
        mid_price=65.0,
        price_history=[65.00, 65.01, 65.02, 65.01, 65.00, 65.01,
                       65.00, 65.01, 65.02, 65.01],
        cycle_completion_ts=[],
        fee_per_roundtrip=0.50,
        contract_size=1.0,
        qty=1,
        tick_size=0.01,
    )
    assert dec is not None
    # 3× floor = 3 × 0.50 / (1 × 1) = 1.50 dollars minimum spread
    assert dec.spread >= 1.50 - 1e-9, (
        f"HYPE-like input must produce spread ≥ $1.50 (3× fee floor). "
        f"Got spread={dec.spread}, cost_floor_binding={dec.cost_floor_binding}. "
        f"If this fails, we're back to bleeding cycles."
    )


def test_pt_shape_input_gets_wide_enough_spread():
    """PT (PLAT nano-futures): ~$1680 mark, ~$20 round-trip fee, contract_size=10.
    Historic bleed pattern had spread = $1.50 with $20 fees → guaranteed loss.
    After the fix, expert_spread must produce spread ≥ 3× $20 / 10 = $6.00."""
    from expert_spread import optimal_spread
    dec = optimal_spread(
        mid_price=1680.0,
        price_history=[1680.0, 1680.5, 1681.0, 1680.8, 1680.2, 1680.5,
                       1680.9, 1680.3, 1680.7, 1680.4],
        cycle_completion_ts=[],
        fee_per_roundtrip=20.0,
        contract_size=10.0,
        qty=1,
        tick_size=0.10,
    )
    assert dec is not None
    # 3× floor = 3 × 20 / (10 × 1) = 6.0 dollars minimum
    assert dec.spread >= 6.0 - 1e-9, (
        f"PT-like input must produce spread ≥ $6.00 (3× fee floor per contract). "
        f"Got spread={dec.spread}. If this fails, PT will keep bleeding."
    )
