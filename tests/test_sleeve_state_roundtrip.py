"""Every SleeveState field must round-trip through to_dict → from_dict.

Adam 2026-07-19: reload-on-tick (commit 83dd31b) re-hydrates SleeveState
from persisted dict every tick. If from_dict silently drops a field,
the in-memory value gets wiped every 2s. Two active bugs found this way:
  - credited_oids dropped → double-credit vector on resting-stop fills
  - sell_entry_avg dropped → realized-P/L basis corruption

This test asserts EVERY dataclass field survives a round-trip so future
field additions can't reintroduce the class silently.
"""
from __future__ import annotations

from dataclasses import fields

from sleeves import SleeveState, SleeveStateEnum


def _fully_populated_state() -> SleeveState:
    """Build a SleeveState with a non-default value in every field so
    a dropped field shows up as the default after round-trip."""
    return SleeveState(
        id="scan-test",
        state=SleeveStateEnum.ARMED_BUY,
        live_order_id="oid-abc-123",
        filled_qty=3,
        last_sell_qty=2,
        last_sell_fill_price=56.75,
        realized_pnl=42.50,
        cycles=7,
        trail_armed=True,
        trail_high_water_price=56.99,
        current_qty=5,
        hybrid_sell_triggered_ts=1720000000.5,
        sell_entry_avg=56.10,
        own_avg_entry=55.80,
        halt_reason="test halt",
        pre_halt_state="ARMED_SELL",
        stop_loss_hwm=56.99,
        consecutive_stops=2,
        cycles_losing_streak=1,
        last_cycle_realized=-3.25,
        recent_cycle_pnls=[1.0, -2.0, 3.0],
        buy_trail_armed=True,
        buy_trail_low_water=55.20,
        reentry_pending=True,
        reentry_stop_ts=1720000100.0,
        pre_stop_range=0.45,
        reentry_scale_in_stage=1,
        reentry_stage_1_price=55.75,
        blackout_until_ts=1720099999.0,
        armed_buy_since_ts=1720000200.0,
        post_trail_stage="wait_volatility",
        post_trail_exit_ts=1720000300.0,
        post_trail_pre_range=0.55,
        post_trail_stage_b_ts=1720000400.0,
        post_trail_stage_b_ref_high=57.10,
        resting_stop_oid="stop-oid-999",
        resting_stop_px=56.00,
        resting_stop_stage="profit_lock",
        credited_oids=["oid-1", "oid-2", "oid-3"],
    )


def test_all_fields_round_trip():
    """to_dict → from_dict must return an identical SleeveState.
    Any field silently dropped by from_dict causes this to fail."""
    original = _fully_populated_state()
    d = original.to_dict()
    restored = SleeveState.from_dict(d, original.id)

    diffs: list[str] = []
    for f in fields(SleeveState):
        orig_val = getattr(original, f.name)
        rest_val = getattr(restored, f.name)
        if orig_val != rest_val:
            diffs.append(f"  {f.name}: original={orig_val!r} != restored={rest_val!r}")
    assert not diffs, "SleeveState fields dropped by from_dict:\n" + "\n".join(diffs)


def test_credited_oids_survives_reload():
    """The specific bug: reload-on-tick wiped credited_oids, opening a
    double-credit vector on resting-stop fills."""
    ss = SleeveState(id="s1", credited_oids=["stop-oid-fill-A"])
    restored = SleeveState.from_dict(ss.to_dict(), "s1")
    assert restored.credited_oids == ["stop-oid-fill-A"]


def test_sell_entry_avg_survives_reload():
    """The other bug: sell-side cost basis wiped, realized_pnl computed
    against wrong basis on next fill."""
    ss = SleeveState(id="s1", sell_entry_avg=56.42)
    restored = SleeveState.from_dict(ss.to_dict(), "s1")
    assert restored.sell_entry_avg == 56.42


def test_none_defaults_still_none_after_roundtrip():
    """Fields defaulting to None must remain None (not become 0/empty)."""
    ss = SleeveState(id="s1")  # all defaults
    restored = SleeveState.from_dict(ss.to_dict(), "s1")
    assert restored.sell_entry_avg is None
    assert restored.credited_oids == []
    assert restored.own_avg_entry is None
    assert restored.resting_stop_oid is None
