"""Tests for roll detection (spec §9B)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from roll import RollDetection, _contract_family, check_roll, resolve_front_month


class FakeResp:
    def __init__(self, data): self._data = data
    def to_dict(self): return self._data


def make_broker(products):
    b = MagicMock()
    b.client.get_products.return_value = FakeResp({"products": products})
    return b


def _p(pid, expiry_iso):
    return {
        "product_id": pid,
        "future_product_details": {"contract_expiry": expiry_iso},
    }


# ---- family extraction ---------------------------------------------------


def test_family_slr():
    assert _contract_family("SLR-27AUG26-CDE") == "SLR"


def test_family_gold():
    assert _contract_family("GC-27AUG26-CDE") == "GC"


def test_family_perp():
    assert _contract_family("SILVER-PERP-INTX") == "SILVER-PERP"


def test_family_empty():
    assert _contract_family("") == ""


# ---- check_roll ----------------------------------------------------------


def test_no_roll_when_far_from_expiry():
    now = datetime(2026, 7, 6, tzinfo=timezone.utc)
    b = make_broker([_p("SLR-27AUG26-CDE", "2026-08-27T17:25:00Z")])
    d = check_roll(b, "SLR-27AUG26-CDE", roll_days_before=5, now=now)
    assert d.should_roll is False
    assert 50 < d.days_to_expiry < 55
    assert d.active_symbol == "SLR-27AUG26-CDE"


def test_roll_when_within_window():
    now = datetime(2026, 8, 25, tzinfo=timezone.utc)  # 2 days before expiry
    b = make_broker([
        _p("SLR-27AUG26-CDE", "2026-08-27T17:25:00Z"),
        _p("SLR-25NOV26-CDE", "2026-11-25T17:25:00Z"),
    ])
    d = check_roll(b, "SLR-27AUG26-CDE", roll_days_before=5, now=now)
    assert d.should_roll is True
    assert d.next_symbol == "SLR-25NOV26-CDE"


def test_picks_nearest_next_contract():
    now = datetime(2026, 8, 25, tzinfo=timezone.utc)
    b = make_broker([
        _p("SLR-27AUG26-CDE", "2026-08-27T17:25:00Z"),
        _p("SLR-24FEB27-CDE", "2027-02-24T17:25:00Z"),
        _p("SLR-25NOV26-CDE", "2026-11-25T17:25:00Z"),  # nearer next
    ])
    d = check_roll(b, "SLR-27AUG26-CDE", roll_days_before=5, now=now)
    assert d.next_symbol == "SLR-25NOV26-CDE"


def test_no_next_contract_available():
    now = datetime(2026, 8, 25, tzinfo=timezone.utc)
    b = make_broker([_p("SLR-27AUG26-CDE", "2026-08-27T17:25:00Z")])
    d = check_roll(b, "SLR-27AUG26-CDE", roll_days_before=5, now=now)
    assert d.should_roll is True
    assert d.next_symbol is None


def test_unknown_active_symbol_safe():
    b = make_broker([])
    d = check_roll(b, "MYSTERY-CONTRACT", now=datetime.now(timezone.utc))
    assert d.should_roll is False


def test_missing_expiry_safe():
    now = datetime(2026, 7, 6, tzinfo=timezone.utc)
    b = make_broker([{"product_id": "SLR-27AUG26-CDE", "future_product_details": {}}])
    d = check_roll(b, "SLR-27AUG26-CDE", now=now)
    assert d.should_roll is False


def test_summary_readable():
    now = datetime(2026, 8, 25, tzinfo=timezone.utc)
    b = make_broker([
        _p("SLR-27AUG26-CDE", "2026-08-27T17:25:00Z"),
        _p("SLR-25NOV26-CDE", "2026-11-25T17:25:00Z"),
    ])
    d = check_roll(b, "SLR-27AUG26-CDE", roll_days_before=5, now=now)
    s = d.summary()
    assert "ROLL" in s
    assert "SLR-27AUG26-CDE" in s
    assert "SLR-25NOV26-CDE" in s


# ---- resolve_front_month -------------------------------------------------


def test_resolve_front_month_picks_earliest_live_expiry():
    """When multiple contracts exist in the same family, pick the one closest
    to expiry (but not expired)."""
    now = datetime(2026, 7, 6, tzinfo=timezone.utc)
    b = make_broker([
        _p("SLR-27AUG26-CDE", "2026-08-27T17:25:00Z"),   # ← front-month
        _p("SLR-25NOV26-CDE", "2026-11-25T17:25:00Z"),
        _p("SLR-24FEB27-CDE", "2027-02-24T17:25:00Z"),
        _p("GC-30OCT26-CDE", "2026-10-30T17:25:00Z"),    # different family, ignore
    ])
    assert resolve_front_month(b, "SLR", now=now) == "SLR-27AUG26-CDE"


def test_resolve_front_month_skips_expired_contracts():
    """A contract whose expiry is in the past isn't a valid roll target."""
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    b = make_broker([
        _p("SLR-27AUG26-CDE", "2026-08-27T17:25:00Z"),   # expired 5 days ago
        _p("SLR-25NOV26-CDE", "2026-11-25T17:25:00Z"),   # ← the new front-month
    ])
    assert resolve_front_month(b, "SLR", now=now) == "SLR-25NOV26-CDE"


def test_resolve_front_month_falls_back_when_family_missing():
    """No contracts in the family (typo, delisting) → return the fallback."""
    now = datetime(2026, 7, 6, tzinfo=timezone.utc)
    b = make_broker([_p("SLR-27AUG26-CDE", "2026-08-27T17:25:00Z")])
    assert resolve_front_month(b, "NONEXISTENT", now=now, fallback="SLR-27AUG26-CDE") == "SLR-27AUG26-CDE"


def test_resolve_front_month_case_insensitive_family():
    """User might type 'slr' or 'SLR' — both should work."""
    now = datetime(2026, 7, 6, tzinfo=timezone.utc)
    b = make_broker([_p("SLR-27AUG26-CDE", "2026-08-27T17:25:00Z")])
    assert resolve_front_month(b, "slr", now=now) == "SLR-27AUG26-CDE"


def test_resolve_front_month_generic_family():
    """Works for any family — not just SLR. AVE, ETH, BTC all follow the same
    pattern."""
    now = datetime(2026, 7, 6, tzinfo=timezone.utc)
    b = make_broker([
        _p("AVE-20DEC30-CDE", "2030-12-20T17:25:00Z"),
        _p("AVE-25JUN27-CDE", "2027-06-25T17:25:00Z"),   # ← front-month
    ])
    assert resolve_front_month(b, "AVE", now=now) == "AVE-25JUN27-CDE"


def test_resolve_front_month_returns_fallback_on_api_error():
    """API blows up → return fallback instead of crashing the caller."""
    b = MagicMock()
    b.client.get_products.side_effect = RuntimeError("boom")
    assert resolve_front_month(b, "SLR", fallback="SLR-27AUG26-CDE") == "SLR-27AUG26-CDE"
