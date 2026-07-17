"""Tests for C-1 (WS-vs-REST mark drift logic) and C-2 (margin sentinel wiring).

The live_runner main loop is not unit-testable directly, so these tests exercise
the underlying modules invoked by the new loop code.
"""

from __future__ import annotations

import pytest

import margin_sentinel
import risk_sentinel


# ---- margin_sentinel ---------------------------------------------------------

def _pos(symbol="SLR", side="BUY", qty=2, avg_entry=30.0, mark=30.0,
          contract_size=50.0, margin_per_contract=275.0):
    return dict(symbol=symbol, side=side, qty=qty, avg_entry=avg_entry,
                mark=mark, contract_size=contract_size,
                margin_per_contract=margin_per_contract)


def test_margin_sentinel_healthy_no_alerts():
    """Well-capitalised position well away from liquidation: no alerts.
    mpc=400 → leverage=3.75x → liq_move≈26.2% > 20% warn threshold."""
    pos = _pos(qty=1, avg_entry=30.0, mark=30.0,
               contract_size=50.0, margin_per_contract=400.0)
    report = margin_sentinel.margin_report([pos], balance=10_000.0,
                                           warn_distance_pct=20.0)
    assert report["verdict"] == "healthy headroom"
    assert report["alerts"] == []


def test_margin_sentinel_fires_crit_when_within_20pct():
    """Position 15% from liquidation should fire a CRITICAL cluster alert
    (< 20% warn_distance_pct threshold used in the live loop)."""
    # High leverage: 10 contracts, $50 contract_size, $30 entry → notional $15k
    # margin 10 × $275 = $2750 → leverage ≈ 5.45×, liq_move ≈ 18.4%
    pos = _pos(qty=10, avg_entry=30.0, mark=30.0,
               contract_size=50.0, margin_per_contract=275.0)
    report = margin_sentinel.margin_report([pos], balance=5_000.0,
                                           warn_distance_pct=20.0)
    assert report["alerts"], "expected CRITICAL alert near liquidation"
    severities = {a["severity"] for a in report["alerts"]}
    assert "critical" in severities


def test_margin_sentinel_utilization_alert():
    """High margin utilization (>= 60%) fires a HIGH alert."""
    pos = _pos(qty=5, avg_entry=30.0, mark=30.0,
               contract_size=50.0, margin_per_contract=275.0)
    # balance just above total margin so utilization ≈ 91%
    report = margin_sentinel.margin_report([pos], balance=1_600.0,
                                           warn_distance_pct=20.0)
    kinds = {a.get("severity") for a in report["alerts"]}
    assert "high" in kinds or "critical" in kinds


def test_margin_sentinel_empty_positions_no_alerts():
    report = margin_sentinel.margin_report([], balance=5_000.0,
                                           warn_distance_pct=20.0)
    assert report["alerts"] == []
    assert report["margin_used"] == 0.0


def test_margin_sentinel_position_headroom_returns_none_on_underspecified():
    """Zero margin_per_contract AND no liquidation_price → None (truly underspecified)."""
    pos = _pos(margin_per_contract=0.0)
    result = margin_sentinel.position_headroom(pos)
    assert result is None


def test_margin_sentinel_uses_coinbase_liq_price_when_mpc_zero():
    """Auto-seeded product (mpc=0) with a Coinbase liquidation_price is included."""
    pos = _pos(margin_per_contract=0.0, avg_entry=30.0, mark=30.0)
    pos["liquidation_price"] = 25.0  # 16.7% away
    result = margin_sentinel.position_headroom(pos)
    assert result is not None
    assert result["liq_price"] == 25.0
    assert result["distance_to_liq_pct"] == pytest.approx((30.0 - 25.0) / 30.0 * 100, abs=0.01)


def test_margin_sentinel_prefers_coinbase_liq_price_over_computed():
    """When mpc > 0 and Coinbase liq_price is also present, Coinbase wins."""
    # Computed liq for qty=1, entry=30, cs=50, mpc=275 → leverage≈5.45×, liq≈24.5
    pos = _pos(qty=1, avg_entry=30.0, mark=30.0,
               contract_size=50.0, margin_per_contract=275.0)
    pos["liquidation_price"] = 22.0  # Coinbase says lower than computed
    result = margin_sentinel.position_headroom(pos)
    assert result is not None
    assert result["liq_price"] == 22.0


# ---- risk_sentinel stale threshold -------------------------------------------

def test_risk_sentinel_stale_threshold_is_30s():
    """After H-1 fix: stale_snapshot_secs default must be 30.0, not 120."""
    assert risk_sentinel.DEFAULTS["stale_snapshot_secs"] == 30.0


def test_risk_sentinel_snapshot_fires_at_31s():
    """A snapshot 31 seconds old should trigger a stale_snapshot alert."""
    now = 1000.0
    snaps = {"SLR": {"generated_at": now - 31}}
    alerts = risk_sentinel.scan_snapshots(snaps, now)
    assert any(a["kind"] == "stale_snapshot" for a in alerts)


def test_risk_sentinel_snapshot_ok_at_29s():
    """A snapshot 29 seconds old must NOT trigger an alert."""
    now = 1000.0
    snaps = {"SLR": {"generated_at": now - 29}}
    alerts = risk_sentinel.scan_snapshots(snaps, now)
    stale = [a for a in alerts if a["kind"] == "stale_snapshot"]
    assert stale == []


# ---- drift detection helper (pure logic, no live_runner import) --------------

def _compute_drift(ws_price: float, rest_mark: float) -> float:
    return abs(ws_price - rest_mark) / rest_mark


def test_drift_above_1pct_detected():
    assert _compute_drift(101.5, 100.0) > 0.01


def test_drift_below_1pct_not_flagged():
    assert _compute_drift(100.5, 100.0) <= 0.01


def test_drift_exactly_1pct_not_flagged():
    """Threshold is strictly > 1%."""
    assert _compute_drift(101.0, 100.0) <= 0.01
