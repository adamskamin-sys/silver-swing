"""Tests for retirement_ledger — closes the PT/HYP/SLR ghost class.

Invariants that must hold:
  - Recording a retirement puts the product in cooldown for `cooldown_hours`
  - `is_in_cooldown` returns False once the cooldown has expired
  - Multiple retirements on same product: latest expiry wins
  - `clear_product` removes only that product's entries, keeps others
  - `prune_expired` drops long-expired entries (>30d past expiry)
  - Different product_ids do NOT interfere with each other
"""
from __future__ import annotations

import time

import pytest

import retirement_ledger as rl


class _FakeStore:
    """Minimal StateStore Protocol impl for testing."""
    def __init__(self):
        self._config: dict[tuple, dict] = {}

    def get_config(self, tenant, symbol):
        return self._config.get((tenant, symbol))

    def put_config(self, tenant, symbol, config):
        self._config[(tenant, symbol)] = config


TENANT = "adam-live"
NOW = 1_000_000_000.0


def test_record_puts_product_in_cooldown():
    s = _FakeStore()
    rl.record_retirement(s, TENANT, "PT-28SEP26-CDE", "scan-abc",
                          reason="test", cooldown_hours=24, now_ts=NOW)
    in_cd, reason, remaining = rl.is_in_cooldown(s, TENANT, "PT-28SEP26-CDE",
                                                   now_ts=NOW + 60)
    assert in_cd is True
    assert reason == "test"
    assert remaining == pytest.approx(24 * 3600 - 60, abs=1)


def test_cooldown_expires():
    s = _FakeStore()
    rl.record_retirement(s, TENANT, "PT-28SEP26-CDE", "scan-abc",
                          reason="test", cooldown_hours=1, now_ts=NOW)
    # 1 hour + 1 second later
    in_cd, reason, remaining = rl.is_in_cooldown(s, TENANT, "PT-28SEP26-CDE",
                                                   now_ts=NOW + 3601)
    assert in_cd is False
    assert reason == ""
    assert remaining == 0.0


def test_different_products_isolated():
    s = _FakeStore()
    rl.record_retirement(s, TENANT, "PT-28SEP26-CDE", "scan-abc",
                          reason="test", cooldown_hours=24, now_ts=NOW)
    in_cd_pt, _, _ = rl.is_in_cooldown(s, TENANT, "PT-28SEP26-CDE",
                                        now_ts=NOW + 60)
    in_cd_chn, _, _ = rl.is_in_cooldown(s, TENANT, "CHN-19DEC30-CDE",
                                         now_ts=NOW + 60)
    assert in_cd_pt is True
    assert in_cd_chn is False


def test_multiple_retirements_latest_expiry_wins():
    s = _FakeStore()
    # Retire twice with different cooldowns; the LONGER one should win
    rl.record_retirement(s, TENANT, "PT-28SEP26-CDE", "sleeve-1",
                          reason="short block", cooldown_hours=1, now_ts=NOW)
    rl.record_retirement(s, TENANT, "PT-28SEP26-CDE", "sleeve-2",
                          reason="long block", cooldown_hours=48, now_ts=NOW)
    # 2h later — short cooldown expired but long one active
    in_cd, reason, remaining = rl.is_in_cooldown(s, TENANT, "PT-28SEP26-CDE",
                                                   now_ts=NOW + 2 * 3600)
    assert in_cd is True
    assert reason == "long block"
    assert remaining == pytest.approx(46 * 3600, abs=1)


def test_clear_product_targeted():
    s = _FakeStore()
    rl.record_retirement(s, TENANT, "PT-28SEP26-CDE", "sleeve-1",
                          reason="test", cooldown_hours=24, now_ts=NOW)
    rl.record_retirement(s, TENANT, "CHN-19DEC30-CDE", "sleeve-2",
                          reason="test", cooldown_hours=24, now_ts=NOW)
    removed = rl.clear_product(s, TENANT, "PT-28SEP26-CDE")
    assert removed == 1
    # PT no longer in cooldown, CHN still is
    in_cd_pt, _, _ = rl.is_in_cooldown(s, TENANT, "PT-28SEP26-CDE",
                                        now_ts=NOW + 60)
    in_cd_chn, _, _ = rl.is_in_cooldown(s, TENANT, "CHN-19DEC30-CDE",
                                         now_ts=NOW + 60)
    assert in_cd_pt is False
    assert in_cd_chn is True


def test_clear_missing_product_is_noop():
    s = _FakeStore()
    rl.record_retirement(s, TENANT, "PT-28SEP26-CDE", "sleeve-1",
                          reason="test", cooldown_hours=24, now_ts=NOW)
    removed = rl.clear_product(s, TENANT, "NONEXISTENT-CDE")
    assert removed == 0
    # PT still in cooldown
    in_cd, _, _ = rl.is_in_cooldown(s, TENANT, "PT-28SEP26-CDE", now_ts=NOW + 60)
    assert in_cd is True


def test_prune_expired_drops_old_entries():
    s = _FakeStore()
    # Entry that expired 31 days ago — should be pruned
    rl.record_retirement(s, TENANT, "OLD-CDE", "sleeve-old",
                          reason="ancient", cooldown_hours=24,
                          now_ts=NOW - 32 * 24 * 3600)
    # Entry that expired 1 day ago — should be kept (audit window)
    rl.record_retirement(s, TENANT, "RECENT-CDE", "sleeve-recent",
                          reason="recent", cooldown_hours=24,
                          now_ts=NOW - 2 * 24 * 3600)
    pruned = rl.prune_expired(s, TENANT, now_ts=NOW)
    assert pruned == 1
    active_all = rl._load(s, TENANT)["entries"]
    assert len(active_all) == 1
    assert active_all[0]["product_id"] == "RECENT-CDE"


def test_list_active_only_returns_unexpired():
    s = _FakeStore()
    # Expired: 2h ago with 1h cooldown
    rl.record_retirement(s, TENANT, "EXPIRED-CDE", "sleeve-1",
                          reason="expired", cooldown_hours=1,
                          now_ts=NOW - 2 * 3600)
    # Active: now with 24h cooldown
    rl.record_retirement(s, TENANT, "ACTIVE-CDE", "sleeve-2",
                          reason="active", cooldown_hours=24, now_ts=NOW)
    active = rl.list_active(s, TENANT, now_ts=NOW + 60)
    assert len(active) == 1
    assert active[0]["product_id"] == "ACTIVE-CDE"


def test_no_ledger_yet_returns_not_in_cooldown():
    s = _FakeStore()
    in_cd, reason, remaining = rl.is_in_cooldown(s, TENANT, "ANY-CDE", now_ts=NOW)
    assert in_cd is False
    assert reason == ""
    assert remaining == 0.0
