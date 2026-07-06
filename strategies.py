"""
Exit strategies (spec §5 + §6).

The SwingTrader delegates the "where do I sell / where do I rebuy" decision
to an ExitStrategy. That keeps the state-machine + safety + persistence code
(swing_leg.py) stable while we swap strategies underneath.

Currently implemented:
  FixedLimitExit    — classic swing. Sell resting at cfg.sell_px, buy resting
                      at cfg.buy_px. What swing_leg.py did before this refactor.
  TrailingStopExit  — arms at cfg.trail_trigger but does not sell until price
                      falls back through a stop trailing under the high-water
                      mark. High-water is state-persisted so a restart mid-trail
                      resumes correctly (spec §5's "MUST persist").
                      Rebuy re-anchor built in (spec §6).

Both return a `SellDirective` / `BuyDirective` that the SwingTrader executes,
or None if the strategy wants to wait one more tick.

Deliberately out of scope for this MVP:
  - Native FCM trailing stop (spec §5 [OPEN]). We synthesize the trail in
    software. Loss-of-protection-on-crash is real but caught by the heartbeat
    watcher + sanity ceiling. Upgrade path: once we confirm Coinbase supports
    a native trailing stop, replace TrailingStopExit's fire logic to place a
    stop-limit that Coinbase manages server-side.
  - ATR-based trail distance (spec §5B [OPEN]). trail_distance is a fixed cent
    amount here; ATR-based version wraps this with an ATR calculator later.
  - Structure/pivot-anchored trail (Carter, §5B). Same story — layer on top.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass
class SellDirective:
    qty: int
    limit_price: float


@dataclass
class BuyDirective:
    qty: int
    limit_price: float


class ExitStrategy(Protocol):
    """The three moments a strategy participates in.

    All state mutations happen via the passed-in `state` object; the strategy
    doesn't touch the store or the broker directly. That keeps side effects
    concentrated in swing_leg.
    """

    def sell_action(self, state, cfg, current_price: float) -> Optional[SellDirective]:
        ...

    def buy_action(
        self,
        state,
        cfg,
        current_price: float,
        last_sell_fill_price: Optional[float] = None,
    ) -> Optional[BuyDirective]:
        ...

    def on_sell_filled(self, state, cfg, fill_price: float) -> None:
        """Called after a sell fills — a chance to reset trail state, etc."""
        ...

    def on_buy_filled(self, state, cfg, fill_price: float) -> None:
        """Called after a buy fills — cycle complete, reset for next round."""
        ...


# ============================================================================
# FixedLimitExit — classic swing
# ============================================================================


class FixedLimitExit:
    """Classic range scalp: SELL cfg.swing_qty @ cfg.sell_px, BUY @ cfg.buy_px.
    Ignores current_price entirely on both sides — the levels are the whole logic.
    Matches what swing_leg.py did before the refactor."""

    name = "fixed_limit"

    def sell_action(self, state, cfg, current_price):
        return SellDirective(qty=state.swing_qty, limit_price=cfg.sell_px)

    def buy_action(self, state, cfg, current_price, last_sell_fill_price=None):
        return BuyDirective(qty=state.swing_qty, limit_price=cfg.buy_px)

    def on_sell_filled(self, state, cfg, fill_price):
        pass  # nothing to reset

    def on_buy_filled(self, state, cfg, fill_price):
        pass


# ============================================================================
# TrailingStopExit — ride the breakout, exit on the roll-over
# ============================================================================


class TrailingStopExit:
    """Arms at cfg.trail_trigger. Once armed, tracks a stop that trails under
    the high-water mark at cfg.trail_distance below. When price falls back
    through the stop, fires a limit sell one tick under the current price
    (aggressive but not market) — simple, fills fast in normal conditions.

    Persisted state:
      trail_high_water_price — updated every tick above trigger. On restart,
                               reload this so the trail continues from where
                               it was, not from scratch.
      trail_armed            — True once trigger crossed; distinguishes "still
                               waiting for the trigger" from "already in the trail."

    On buy side, applies re-anchor logic (§6): if the trailing exit filled far
    above cfg.sell_px, the old range is dead. Rebuy at floor(fill_price) − 1
    as a starting whole-number reference. If it filled near cfg.sell_px, the
    range is intact and we rebuy at cfg.buy_px.
    """

    name = "trailing_stop"

    def sell_action(self, state, cfg, current_price):
        # Not yet at the trigger — wait, don't place anything on the book
        if not getattr(state, "trail_armed", False):
            if current_price < cfg.trail_trigger:
                return None
            state.trail_armed = True
            state.trail_high_water_price = current_price

        # In the trail — ratchet the high-water mark up on new highs
        hwm = getattr(state, "trail_high_water_price", current_price) or current_price
        if current_price > hwm:
            state.trail_high_water_price = current_price
            hwm = current_price

        stop = hwm - cfg.trail_distance
        # §5A minimum lock-in: the trail may not fire below the price at which
        # this cycle's fees-per-contract have been recovered above the buy_px
        # anchor. Sell too low and the round-trip loses to costs.
        if cfg.contract_size > 0:
            min_sell = cfg.buy_px + cfg.fee_per_contract_roundtrip / cfg.contract_size
            stop = max(stop, min_sell)

        if current_price <= stop:
            # Fire an aggressive limit — one tick under current price will
            # cross the bid immediately in normal conditions.
            price_ticks = round(current_price / cfg.tick_size) - 1
            return SellDirective(qty=state.swing_qty, limit_price=price_ticks * cfg.tick_size)
        return None

    def buy_action(self, state, cfg, current_price, last_sell_fill_price=None):
        buy_px = cfg.buy_px
        # Re-anchor: if the trailing exit filled well above the old sell trigger,
        # the range is dead — pick a new whole-number anchor around the fill.
        # (Spec §6: "re-anchoring may only ever chase a confirmed level." For the
        # MVP we accept the fill price as the confirmation; a fuller Kleinman-style
        # gate lives in the strategy selector, not here.)
        if last_sell_fill_price is not None and cfg.sell_px:
            gap = last_sell_fill_price - cfg.sell_px
            if gap >= cfg.reanchor_threshold:
                buy_px = math.floor(last_sell_fill_price) - 1.0
        return BuyDirective(qty=state.swing_qty, limit_price=buy_px)

    def on_sell_filled(self, state, cfg, fill_price):
        # Reset trail state — next cycle re-arms from scratch
        state.trail_armed = False
        state.trail_high_water_price = 0.0

    def on_buy_filled(self, state, cfg, fill_price):
        pass


# ============================================================================
# Strategy selector — pick an ExitStrategy by name (spec §7)
# ============================================================================


def strategy_by_name(name: str) -> ExitStrategy:
    """Resolve a strategy name from config into a strategy instance."""
    if name == "fixed_limit":
        return FixedLimitExit()
    if name == "trailing_stop":
        return TrailingStopExit()
    raise ValueError(f"unknown exit_mode: {name!r}. Use 'fixed_limit' or 'trailing_stop'.")
