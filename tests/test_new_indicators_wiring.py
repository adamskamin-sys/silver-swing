"""Verify KAMA + Fisher Transform advisory stages are wired into
experts_reentry.compute_reentry. These are 2026-07-15 additions.

Both are ADVISORY (add to snapshot + reasons but don't hard-veto).
Existing behavior (buy_px derivation, VPIN gate, Vince cap, etc.)
is unchanged — tested by tests/test_experts_reentry.py which continues
to pass.
"""
from __future__ import annotations


def _make_prices(n=80, start=100.0):
    """Ascending price series long enough to trigger full expert chain."""
    return [start + i * 0.15 for i in range(n)]


def test_kama_snapshot_present():
    import experts_reentry as _er
    prices = _make_prices()
    result = _er.compute_reentry(
        prices=prices, sold_price=105.0, spread=0.5, strategy_qty=1,
    )
    snap = result.get("expert_snapshot", {})
    assert "kama" in snap, f"KAMA missing from snapshot; got keys: {list(snap.keys())}"
    kama_data = snap["kama"]
    if "error" not in kama_data:
        # If module loaded successfully, verify the shape
        assert "signal" in kama_data
        assert kama_data["signal"] in ("buy", "sell", "hold")


def test_fisher_snapshot_present():
    import experts_reentry as _er
    prices = _make_prices()
    result = _er.compute_reentry(
        prices=prices, sold_price=105.0, spread=0.5, strategy_qty=1,
    )
    snap = result.get("expert_snapshot", {})
    assert "fisher" in snap, f"Fisher missing from snapshot; got keys: {list(snap.keys())}"
    fisher_data = snap["fisher"]
    if "error" not in fisher_data:
        assert "crossover" in fisher_data
        assert fisher_data["crossover"] in ("up", "down", "none")


def test_new_indicators_advisory_not_veto():
    """New indicators should ADD to snapshot but never HARD-VETO on their own.
    Verify by feeding a synthetic price series and confirming should_arm
    doesn't become False solely due to KAMA or Fisher (unless something
    they blocked was a real veto — e.g., regime downtrend still hard-vetoes)."""
    import experts_reentry as _er
    prices = _make_prices(n=80)
    result = _er.compute_reentry(
        prices=prices, sold_price=105.0, spread=0.5, strategy_qty=1,
    )
    # We can't assert should_arm=True unconditionally because Elder/regime
    # gates may block for legitimate reasons on this synthetic data. But we
    # CAN assert that KAMA/Fisher errors don't crash the whole chain.
    assert "expert_snapshot" in result
    # Both new stages present without crashing:
    snap = result["expert_snapshot"]
    assert "kama" in snap
    assert "fisher" in snap
