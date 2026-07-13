"""Tests for the sleeve-spawn accumulation mechanic (Adam 2026-07-13).

When a sleeve's realized_pnl exceeds the accumulation threshold, a NEW
sibling sleeve is created with fresh expert-derived parameters anchored
to the current market (instead of incrementing the parent's qty).
"""

import time

import pytest

from paper_broker import PaperBroker, PaperConfig
from safety import KillSwitch, TradeLog, reconcile as safety_reconcile
from sleeves import SleeveConfig, SleeveStateEnum
from state_store import JsonFileStateStore
from swing_leg import SwingConfig, SwingTrader


TENANT = "adam"
SYMBOL = "SLR-27AUG26-CDE"


def _make_broker(balance=100_000.0):
    return PaperBroker(PaperConfig(
        product_id=SYMBOL, contract_size=50.0, tick_size=0.005,
        fee_per_fill=2.34, margin_per_contract=275.0,
        starting_balance=balance,
    ))


def _base_config():
    return {
        "core_qty": 0,
        "swing_qty": 0,
        "sell_px": 65.0,
        "buy_px": 63.0,
        "contract_size": 50,
        "margin_per_contract": 275.0,
        "fee_per_contract_roundtrip": 4.68,
        "scale_up_buffer_mult": 1.5,
        "abort_below": 55.0,
        "abort_above": 75.0,
    }


def _seed_expert_params(store, tenant, symbol, atr=0.10):
    """Set expert_params on the __portfolio__ snap so spawn can read ATR."""
    pf = store.get_config(tenant, "__portfolio__") or {}
    pf["derivatives"] = [{
        "product_id": symbol,
        "expert_params": {
            "atr": atr,
            "multipliers": {
                "trail_x_atr": 2.0,
                "stop_x_atr": 2.0,
                "activation_offset_x_atr": 0.5,
                "ratchet_x_atr": 3.0,
                "ratchet_activation_x_atr": 0.5,
                "reanchor_x_atr": 1.0,
                "buy_trail_x_atr": 0.5,
            }
        }
    }]
    store.put_config(tenant, "__portfolio__", pf)


def _seed_snapshot(store, tenant, symbol, mark=64.5):
    store.put_snapshot(tenant, symbol, {"last_mark": mark, "position_qty": 0})


def _trader_with_sleeves(tmp_path, sleeves):
    store = JsonFileStateStore(str(tmp_path / "store"))
    cfg = _base_config()
    cfg["sleeves"] = sleeves
    store.put_config(TENANT, SYMBOL, cfg)
    _seed_expert_params(store, TENANT, SYMBOL, atr=0.10)
    _seed_snapshot(store, TENANT, SYMBOL, mark=64.5)
    b = _make_broker()
    log = TradeLog(str(tmp_path / "trades.jsonl"))
    ks = KillSwitch(store, TENANT)
    trader = SwingTrader(b, store, TENANT, SYMBOL, trade_log=log, kill_switch=ks)
    return trader, store


def test_spawn_creates_new_sibling_sleeve_with_fresh_params(tmp_path):
    """When accumulate_mode='spawn' and threshold hit, a NEW sleeve
    appears in the config (not just qty++ on parent)."""
    parent_sleeve = {
        "id": "p1", "name": "Parent", "qty": 1,
        "sell_px": 65.0, "buy_px": 63.0,
        "accumulate_enabled": True, "max_qty": 5,
        "accumulate_mode": "spawn",
        "scale_up_buffer_mult": 1.5,
    }
    trader, store = _trader_with_sleeves(tmp_path, [parent_sleeve])
    # Manually invoke the scale-up path with sufficient realized profit
    sc = trader._load_sleeves_cfg()[0]
    ss = trader._init_sleeves_state({})[sc.id]
    ss.realized_pnl = 275.0 * 1.5 + 10  # over threshold
    trader._maybe_scale_up_sleeve(sc, ss)
    cfg_after = store.get_config(TENANT, SYMBOL)
    sleeves_after = cfg_after.get("sleeves") or []
    assert len(sleeves_after) == 2  # parent + child
    child = [s for s in sleeves_after if s["id"] != "p1"][0]
    assert child["qty"] == 1
    assert child["spawned_from"] == "p1"
    assert child["spawn_generation"] == 1
    assert "Gen 1" in child["name"]


def test_spawn_child_has_fresh_market_anchored_targets(tmp_path):
    """Child sleeve's sell_px/buy_px are centered on CURRENT mark, not the
    parent's stale sell/buy."""
    parent_sleeve = {
        "id": "p1", "name": "Parent", "qty": 1,
        "sell_px": 65.0, "buy_px": 63.0,  # centered on 64
        "accumulate_enabled": True, "max_qty": 5,
        "accumulate_mode": "spawn",
    }
    trader, store = _trader_with_sleeves(tmp_path, [parent_sleeve])
    # Move the market — snapshot mark = 64.5, different from parent's 64.0 midpoint
    sc = trader._load_sleeves_cfg()[0]
    ss = trader._init_sleeves_state({})[sc.id]
    ss.realized_pnl = 500  # over threshold
    trader._maybe_scale_up_sleeve(sc, ss)
    cfg_after = store.get_config(TENANT, SYMBOL)
    child = [s for s in cfg_after["sleeves"] if s["id"] != "p1"][0]
    # Child midpoint should be near 64.5 (the current mark), not 64.0
    mid = (child["sell_px"] + child["buy_px"]) / 2
    assert abs(mid - 64.5) < 0.01


def test_spawn_child_has_atr_derived_trail_distance(tmp_path):
    """Child's trail_distance = ATR × trail_x_atr (from expert_params)."""
    parent_sleeve = {
        "id": "p1", "name": "Parent", "qty": 1,
        "sell_px": 65.0, "buy_px": 63.0,
        "accumulate_enabled": True, "max_qty": 5,
        "accumulate_mode": "spawn",
    }
    trader, store = _trader_with_sleeves(tmp_path, [parent_sleeve])
    sc = trader._load_sleeves_cfg()[0]
    ss = trader._init_sleeves_state({})[sc.id]
    ss.realized_pnl = 500
    trader._maybe_scale_up_sleeve(sc, ss)
    cfg_after = store.get_config(TENANT, SYMBOL)
    child = [s for s in cfg_after["sleeves"] if s["id"] != "p1"][0]
    # ATR=0.10, trail_x_atr=2.0 → trail_distance=0.20
    assert abs(child["trail_distance"] - 0.20) < 0.001


def test_spawn_inherits_parent_feature_toggles(tmp_path):
    """Child copies feature toggles from parent (falling knife, Kelly, etc.)"""
    parent_sleeve = {
        "id": "p1", "name": "Parent", "qty": 1,
        "sell_px": 65.0, "buy_px": 63.0,
        "accumulate_enabled": True, "max_qty": 5,
        "accumulate_mode": "spawn",
        "buy_trail_enabled": True,
        "kelly_enabled": True,
        "adaptive_spread_enabled": True,
        "book_imbalance_gate_enabled": True,
    }
    trader, store = _trader_with_sleeves(tmp_path, [parent_sleeve])
    sc = trader._load_sleeves_cfg()[0]
    ss = trader._init_sleeves_state({})[sc.id]
    ss.realized_pnl = 500
    trader._maybe_scale_up_sleeve(sc, ss)
    cfg_after = store.get_config(TENANT, SYMBOL)
    child = [s for s in cfg_after["sleeves"] if s["id"] != "p1"][0]
    assert child["buy_trail_enabled"] is True
    assert child["kelly_enabled"] is True
    assert child["adaptive_spread_enabled"] is True
    assert child["book_imbalance_gate_enabled"] is True


def test_spawn_falls_back_to_qty_when_no_expert_atr(tmp_path):
    """If expert_params isn't loaded for this product, spawn returns None
    and we fall back to qty++ so profit isn't wasted."""
    store = JsonFileStateStore(str(tmp_path / "store"))
    cfg = _base_config()
    cfg["sleeves"] = [{
        "id": "p1", "name": "Parent", "qty": 1,
        "sell_px": 65.0, "buy_px": 63.0,
        "accumulate_enabled": True, "max_qty": 5,
        "accumulate_mode": "spawn",
    }]
    store.put_config(TENANT, SYMBOL, cfg)
    # DON'T seed expert params — this triggers the fallback
    store.put_snapshot(TENANT, SYMBOL, {"last_mark": 64.5, "position_qty": 0})
    b = _make_broker()
    log = TradeLog(str(tmp_path / "trades.jsonl"))
    ks = KillSwitch(store, TENANT)
    trader = SwingTrader(b, store, TENANT, SYMBOL, trade_log=log, kill_switch=ks)
    sc = trader._load_sleeves_cfg()[0]
    ss = trader._init_sleeves_state({})[sc.id]
    ss.realized_pnl = 500
    trader._maybe_scale_up_sleeve(sc, ss)
    cfg_after = store.get_config(TENANT, SYMBOL)
    sleeves_after = cfg_after["sleeves"]
    # Fallback: still ONE sleeve, qty=2 (parent qty++)
    assert len(sleeves_after) == 1
    assert sleeves_after[0]["qty"] == 2


def test_spawn_respects_family_max_qty_cap(tmp_path):
    """max_qty caps the FAMILY size, not just the parent's qty."""
    parent_sleeve = {
        "id": "p1", "name": "Parent", "qty": 1,
        "sell_px": 65.0, "buy_px": 63.0,
        "accumulate_enabled": True,
        "max_qty": 2,  # only room for one more
        "accumulate_mode": "spawn",
    }
    trader, store = _trader_with_sleeves(tmp_path, [parent_sleeve])
    sc = trader._load_sleeves_cfg()[0]
    ss = trader._init_sleeves_state({})[sc.id]
    ss.realized_pnl = 500
    # First spawn should succeed
    trader._maybe_scale_up_sleeve(sc, ss)
    cfg_after = store.get_config(TENANT, SYMBOL)
    assert len(cfg_after["sleeves"]) == 2
    # Reload + retry — should NOT spawn again (family at max)
    sc = trader._load_sleeves_cfg()[0]
    ss = trader._init_sleeves_state({})[sc.id]
    ss.realized_pnl = 500
    trader._maybe_scale_up_sleeve(sc, ss)
    cfg_after2 = store.get_config(TENANT, SYMBOL)
    assert len(cfg_after2["sleeves"]) == 2  # unchanged
