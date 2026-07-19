"""The primary stop-loss + sleeve stop-loss must NOT market-sell in the
~1s window between a fresh BUY fill and _maintain_resting_stop() placing
the ratchet stop.

Adam 2026-07-19 SLR $56.03 incident:
  15:19:04 Limit BUY $55.915 filled
  15:19:05 Market SELL $55.900   ← the bug
Bot's own stop-loss backstop fired within 1 tick of the fresh buy
because ss.resting_stop_oid was None (sleeve hadn't placed yet) and
the pre-fix guard only fired on placed oids.

Extended feedback_ratchet_stop_never_gap invariant: defer to the
resting stop whenever resting_stop_enabled, regardless of whether the
oid has been placed yet.
"""
from __future__ import annotations

import pytest

from swing_leg import SwingTrader, SleeveState, SleeveStateEnum
from sleeves import SleeveConfig


class _MinStore:
    def __init__(self):
        self._s: dict = {}
        self._c: dict = {}

    def get_state(self, t, s): return self._s.get((t, s))
    def put_state(self, t, s, v): self._s[(t, s)] = v
    def get_config(self, t, s): return self._c.get((t, s))
    def put_config(self, t, s, v): self._c[(t, s)] = v
    def get_snapshot(self, t, s): return None
    def put_snapshot(self, t, s, v): pass


class _MinBroker:
    """Records every market SELL that gets fired. Fresh-buy scenarios
    should record NONE."""
    def __init__(self):
        self.market_sells: list[tuple[str, int]] = []
        self._pos = 1  # fresh buy just filled

    def position_qty(self): return self._pos
    def contract_spec(self): return {"session_open": True}
    def place_market(self, side, qty):
        self.market_sells.append((side, qty))
        return f"oid-{len(self.market_sells)}"


class _MinCfg:
    core_qty = 0
    stop_loss_qty_mode = "all"


def _make_trader():
    t = SwingTrader.__new__(SwingTrader)
    from swing_leg import SwingState
    t.store = _MinStore()
    t.tenant_id = "adam-live"
    t.symbol = "SLR-27AUG26-CDE"
    t.s = SwingState()
    t.s.sleeves = {}
    t.b = _MinBroker()
    t.cfg = _MinCfg()
    t.notifier = None
    return t


def test_sleeve_stop_defers_to_pending_resting_stop():
    """Sleeve has resting_stop_enabled=True but oid=None (fresh position,
    hasn't placed yet). Stop-loss backstop must skip — deferring to the
    resting stop that's about to place — NOT market-sell.
    """
    trader = _make_trader()
    sc = SleeveConfig(
        id="s1", name="test", qty=1,
        stop_loss_enabled=True, stop_loss_px=55.913,
        resting_stop_enabled=True,
    )
    ss = SleeveState(
        id="s1", state=SleeveStateEnum.ARMED_SELL,
        own_avg_entry=55.915,
        resting_stop_oid=None,  # not placed yet — this is the race window
    )
    trader.s.sleeves["s1"] = ss
    # Mark below stop_loss_px — trigger condition met
    fired = trader._maybe_trigger_sleeve_stop_loss(sc, ss, last_price=55.900)
    assert not trader.b.market_sells, (
        f"Bot fired market SELL in the fresh-buy race window: "
        f"{trader.b.market_sells}"
    )


def test_sleeve_stop_would_fire_when_resting_disabled():
    """Regression: when resting_stop_enabled=False, code path should
    proceed PAST the deferral guard. Verifies deferral only triggers on
    resting_stop_enabled=True. Uses lightweight sc bag; enough to prove
    the deferral check doesn't over-fire."""
    trader = _make_trader()
    trader.log = None  # attribute checked by downstream _record

    # sc-like namespace — enough attributes for the guard check + qty compute
    class _SC:
        id = "s1"
        stop_loss_enabled = True
        stop_loss_px = 55.913
        resting_stop_enabled = False
        stop_loss_qty_mode = "all"
        stop_loss_qty_custom = 0
        stop_loss_max_consecutive = 0
        stop_loss_reanchor_on_trigger = False
        stop_loss_protect_realized_enabled = False
        stop_loss_ratchet_enabled = False
        qty = 1
        name = "test"
        buy_px = 55.90
        sell_px = 56.10

    ss = SleeveState(
        id="s1", state=SleeveStateEnum.ARMED_SELL,
        own_avg_entry=55.915,
    )
    trader.s.sleeves["s1"] = ss

    # Stub methods the fire-path calls so we can observe place_market
    trader._compute_sleeve_stop_loss_qty = lambda sc, pos: 1
    trader._refresh_portfolio_after_fill = lambda: None
    trader._sleeve_reanchor = lambda *a, **kw: None
    trader._record = lambda *a, **kw: None

    # We only care that place_market was CALLED, not that the full post-sell
    # flow succeeds. Downstream reanchor/reentry paths need more setup than
    # this smoke test wants to build — swallow any exception after the sell.
    try:
        trader._maybe_trigger_sleeve_stop_loss(_SC(), ss, last_price=55.900)
    except Exception:
        pass
    assert trader.b.market_sells, (
        "Backstop MUST fire when resting_stop_enabled=False — deferral guard "
        "over-triggered"
    )
