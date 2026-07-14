"""Tests for reconciliation_monitor — cloud auditor artifact.

Covers the five failure modes it's designed to catch, especially the
2026-07-14 duplicate-orders incident (identical-qty identical-price SLVR
sells 51 min apart), and the position-mismatch class (position going down
while bot thinks it hasn't).
"""
from __future__ import annotations

import reconciliation_monitor as rm


# ---- duplicate_order — the SLVR incident ---------------------------------

def test_duplicate_orders_flagged_critical():
    """The 2026-07-14 bug: two identical-qty identical-price sells."""
    open_orders = [
        {"order_id": "aaaa1111", "symbol": "SLR-27AUG26-CDE",
         "side": "SELL", "price": 65.25, "qty": 2},
        {"order_id": "bbbb2222", "symbol": "SLR-27AUG26-CDE",
         "side": "SELL", "price": 65.25, "qty": 2},
    ]
    findings = rm.check_duplicate_orders(open_orders)
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "critical"
    assert f.kind == "duplicate_order"
    assert f.symbol == "SLR-27AUG26-CDE"
    assert "SELL" in f.detail


def test_no_duplicate_when_different_price():
    open_orders = [
        {"order_id": "a", "symbol": "SLR", "side": "SELL", "price": 65.25, "qty": 2},
        {"order_id": "b", "symbol": "SLR", "side": "SELL", "price": 65.50, "qty": 2},
    ]
    assert rm.check_duplicate_orders(open_orders) == []


def test_no_duplicate_when_different_side():
    open_orders = [
        {"order_id": "a", "symbol": "SLR", "side": "SELL", "price": 65.25, "qty": 2},
        {"order_id": "b", "symbol": "SLR", "side": "BUY", "price": 65.25, "qty": 2},
    ]
    assert rm.check_duplicate_orders(open_orders) == []


def test_price_tick_groups_near_prices():
    """Two orders within the same tick should count as duplicates."""
    open_orders = [
        {"order_id": "a", "symbol": "SLR", "side": "SELL", "price": 65.253, "qty": 2},
        {"order_id": "b", "symbol": "SLR", "side": "SELL", "price": 65.255, "qty": 2},
    ]
    findings = rm.check_duplicate_orders(open_orders, price_tick=0.01)
    assert len(findings) == 1  # rounded to 65.25 for both


# ---- orphan_order + missing_order ----------------------------------------

def test_orphan_order_flagged_warn():
    """Order open on exchange but no sleeve tracks it."""
    open_orders = [{"order_id": "x1", "symbol": "OIL", "side": "SELL",
                    "price": 74.5, "qty": 1}]
    sleeves = [{"symbol": "OIL", "state": "ARMED_SELL", "armed": True,
                "live_order_id": "different-id"}]
    findings = rm.check_orphans_and_missing(open_orders, sleeves)
    orphans = [f for f in findings if f.kind == "orphan_order"]
    assert len(orphans) == 1
    assert orphans[0].severity == "warn"


def test_missing_order_flagged_warn():
    """Sleeve says armed with a live_order_id but no such order on exchange."""
    sleeves = [{"symbol": "OIL", "state": "ARMED_SELL", "armed": True,
                "live_order_id": "vanished"}]
    findings = rm.check_orphans_and_missing([], sleeves)
    missing = [f for f in findings if f.kind == "missing_order"]
    assert len(missing) == 1
    assert missing[0].severity == "warn"


def test_no_orphan_when_id_matches():
    open_orders = [{"order_id": "same-id", "symbol": "OIL", "side": "SELL",
                    "price": 74.5, "qty": 1}]
    sleeves = [{"symbol": "OIL", "state": "ARMED_SELL", "armed": True,
                "live_order_id": "same-id"}]
    assert rm.check_orphans_and_missing(open_orders, sleeves) == []


# ---- position_mismatch — the SLR primary state drift class ---------------

def test_position_mismatch_flagged_critical():
    """Exchange has 1 OIL, bot expected 6."""
    exch_positions = {"OIL": 1}
    sleeves = [{"symbol": "OIL", "expected_position": 5},
               {"symbol": "OIL", "expected_position": 1}]
    findings = rm.check_position_mismatch(exch_positions, sleeves)
    assert len(findings) == 1
    assert findings[0].severity == "critical"
    assert findings[0].kind == "position_mismatch"


def test_no_mismatch_when_totals_agree():
    exch_positions = {"OIL": 6}
    sleeves = [{"symbol": "OIL", "expected_position": 5},
               {"symbol": "OIL", "expected_position": 1}]
    assert rm.check_position_mismatch(exch_positions, sleeves) == []


def test_tolerance_absorbs_small_diff():
    """tol=0.5 means diffs up to 0.5 don't flag."""
    findings = rm.check_position_mismatch(
        {"OIL": 5.3}, [{"symbol": "OIL", "expected_position": 5.0}], tol=0.5)
    assert findings == []


# ---- stale_entry (armed buy waiting too long) ----------------------------

def test_stale_entry_by_time():
    now = 1000000
    sleeves = [{"symbol": "OIL", "side": "BUY", "armed": True,
                "armed_at": now - 4000}]
    findings = rm.check_stale_entries(sleeves, now, stale_after_s=3600)
    assert len(findings) == 1
    assert findings[0].severity == "warn"
    assert findings[0].kind == "stale_entry"


def test_stale_entry_by_price_drift():
    """CU/copper case: mark trended above last sale despite time still fresh."""
    now = 1000000
    sleeves = [{"symbol": "CU", "side": "BUY", "armed": True,
                "armed_at": now - 100, "last_sale_px": 6.30, "atr": 0.05}]
    findings = rm.check_stale_entries(
        sleeves, now, stale_after_s=3600,
        price_lookup=lambda s: 6.45)   # 3 ATRs above last sale
    assert len(findings) == 1
    assert "trended above last sale" in findings[0].detail


def test_no_stale_when_fresh_and_no_drift():
    now = 1000000
    sleeves = [{"symbol": "OIL", "side": "BUY", "armed": True,
                "armed_at": now - 100, "last_sale_px": 74.5, "atr": 0.2}]
    findings = rm.check_stale_entries(
        sleeves, now, stale_after_s=3600, price_lookup=lambda s: 74.4)
    assert findings == []


# ---- safety_halt (Tier 2 (b) — excludes reentry_reeval expire halts) ----

def test_safety_halt_flagged_for_generic_halt():
    """Any sleeve in HALTED state (non-expire reason) surfaces as warn."""
    sleeves = [{"symbol": "OIL", "state": "HALTED",
                "halt_reason": "drawdown breach"}]
    findings = rm.check_safety_halts(sleeves)
    assert len(findings) == 1
    assert findings[0].severity == "warn"
    assert findings[0].kind == "safety_halt"


def test_safety_halt_EXCLUDES_reentry_reeval_expire():
    """AUDITOR 2026-07-14 Tier 2 (b): a HALTED sleeve whose reason starts
    with the reentry_reeval expire prefix MUST NOT count as a safety halt —
    those are deliberate near-expiry exits, not fixable safety halts."""
    from reentry_reeval import EXPIRE_HALT_PREFIX
    sleeves = [
        {"symbol": "OIL", "state": "HALTED",
         "halt_reason": f"{EXPIRE_HALT_PREFIX} extended, no pullback room"},
        {"symbol": "CU",  "state": "HALTED",
         "halt_reason": "portfolio circuit breaker"},
    ]
    findings = rm.check_safety_halts(sleeves)
    # Only the non-expire halt should surface
    assert len(findings) == 1
    assert findings[0].symbol == "CU"


def test_safety_halt_no_finding_when_not_halted():
    sleeves = [{"symbol": "OIL", "state": "ARMED_SELL", "halt_reason": None}]
    assert rm.check_safety_halts(sleeves) == []


# ---- reconcile() — full pipeline + severity ordering ---------------------

def test_reconcile_returns_critical_first():
    """Critical findings must sort before warns."""
    open_orders = [
        {"order_id": "a", "symbol": "SLR", "side": "SELL", "price": 65.25, "qty": 2},
        {"order_id": "b", "symbol": "SLR", "side": "SELL", "price": 65.25, "qty": 2},  # dup
    ]
    sleeves = [{"symbol": "SLR", "side": "BUY", "armed": True,
                "armed_at": 900, "expected_position": 2}]
    exch_positions = {"SLR": 5}  # mismatch: exchange says 5, expected 2
    findings = rm.reconcile(open_orders=open_orders, exch_positions=exch_positions,
                             sleeves=sleeves, now_ts=10000, stale_after_s=3600)
    # Critical: duplicate_order + position_mismatch (2 criticals)
    # Warn: stale_entry (armed_at 900 vs now 10000 = 9100s stale)
    severities = [f.severity for f in findings]
    assert severities[0] == "critical"
    assert severities[-1] == "warn"


def test_reconcile_clean_returns_empty():
    findings = rm.reconcile(open_orders=[], exch_positions={},
                             sleeves=[], now_ts=10000)
    assert findings == []


# ---- format_alert --------------------------------------------------------

def test_format_alert_empty_returns_empty_string():
    """Nothing to alert about = send nothing."""
    assert rm.format_alert([]) == ""


def test_format_alert_names_critical_count():
    findings = [
        rm.Finding("critical", "duplicate_order", "SLR", "2 orders"),
        rm.Finding("warn", "orphan_order", "OIL", "one orphan"),
    ]
    msg = rm.format_alert(findings)
    assert "1 critical" in msg
    assert "2 total" in msg
    assert "duplicate_order" in msg
    assert "orphan_order" in msg


# ---- state_config_drift — SLR-incident class (auditor 2026-07-14) --------

def test_state_config_drift_flags_slr_ghost():
    """SLR bug: config.swing_qty=0 but state.swing_qty=2. Bot re-arms
    from stale in-memory qty."""
    findings = rm.check_state_config_drift([
        {"symbol": "SLR-27AUG26-CDE",
         "state_swing_qty": 2, "config_swing_qty": 0},
        {"symbol": "OIL-20JUL26-CDE",
         "state_swing_qty": 0, "config_swing_qty": 0},
    ])
    assert len(findings) == 1
    assert findings[0].severity == "critical"
    assert findings[0].kind == "state_config_drift"
    assert findings[0].symbol == "SLR-27AUG26-CDE"


def test_state_config_drift_no_flag_when_agree():
    findings = rm.check_state_config_drift([
        {"symbol": "OIL", "state_swing_qty": 1, "config_swing_qty": 1},
        {"symbol": "SLR", "state_swing_qty": 0, "config_swing_qty": 0},
    ])
    assert findings == []


def test_state_config_drift_handles_none_and_missing():
    """Robust to missing/None values — no NameError, no false-positive."""
    findings = rm.check_state_config_drift([
        {"symbol": "X", "state_swing_qty": None, "config_swing_qty": None},
        {"symbol": "Y"},  # missing keys → default 0/0 → agree → no finding
    ])
    assert findings == []


def test_reconcile_includes_state_config_drift_when_pairs_provided():
    findings = rm.reconcile(
        open_orders=[], exch_positions={}, sleeves=[], now_ts=10000,
        state_config_pairs=[
            {"symbol": "SLR", "state_swing_qty": 2, "config_swing_qty": 0}])
    kinds = [f.kind for f in findings]
    assert "state_config_drift" in kinds


def test_reconcile_skips_drift_when_pairs_none():
    """Backwards-compat: reconcile without state_config_pairs = old behavior."""
    findings = rm.reconcile(
        open_orders=[], exch_positions={}, sleeves=[], now_ts=10000)
    assert findings == []
