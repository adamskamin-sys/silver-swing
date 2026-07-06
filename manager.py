"""
TraderManager + AccountMarginGovernor (spec §8, §9, §9A).

TraderManager runs N SwingTraders in one loop, one per symbol under a single
tenant. Each has its own broker (paper or live) and its own feed. State and
config are already namespaced per (tenant, symbol) in the StateStore, so
adding an instrument is config, not code.

AccountMarginGovernor sits above the manager. Each SwingTrader independently
reasons about its own margin; the governor tracks the total across all
instances and vetoes scale-ups that would push aggregate margin past a
per-tenant cap. This is the portfolio-level Jim Paul — required once >1
instrument can trade.

Multi-tenant note (§9A): the governor is per-tenant. Never aggregates across
tenants. A separate governor instance per tenant.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from alerting import Notifier, Priority
from safety import KillSwitch, TradeLog
from state_store import StateStore
from swing_leg import Broker, SwingConfig, SwingTrader


@dataclass
class InstrumentSlot:
    symbol: str
    broker: Broker
    trader: SwingTrader
    feed: Optional[object] = None  # optional feed with latest_ticker() / start() / stop()


class AccountMarginGovernor:
    """Per-tenant aggregate margin veto.

    Attach to a TraderManager. Before each SwingTrader would scale up, the
    manager asks the governor whether the new margin would exceed the cap.
    If yes, the scale-up is silently declined (the strategy just doesn't grow
    this cycle). No halt — the swing continues at its current size.
    """

    def __init__(
        self,
        tenant_id: str,
        max_aggregate_margin: float,
        notifier: Optional[Notifier] = None,
    ):
        self.tenant_id = tenant_id
        self.max_aggregate_margin = float(max_aggregate_margin)
        self.notifier = notifier

    def current_margin(self, slots: list[InstrumentSlot]) -> float:
        total = 0.0
        for slot in slots:
            snapshot = getattr(slot.broker, "snapshot", None)
            if callable(snapshot):
                try:
                    s = snapshot()
                    total += float(s.get("margin_used") or s.get("initial_margin") or 0)
                    continue
                except Exception:
                    pass
            # Fallback: qty * cfg.margin_per_contract
            try:
                qty = abs(slot.broker.position_qty())
                total += qty * slot.trader.cfg.margin_per_contract
            except Exception:
                pass
        return total

    def veto_scale_up(self, slots: list[InstrumentSlot], candidate: InstrumentSlot) -> bool:
        """Return True if the candidate's scale-up should be blocked."""
        current = self.current_margin(slots)
        additional = candidate.trader.cfg.margin_per_contract
        projected = current + additional
        if projected > self.max_aggregate_margin:
            self._notify(
                f"scale-up vetoed by governor",
                f"tenant={self.tenant_id}, symbol={candidate.symbol}\n"
                f"current aggregate margin: ${current:,.2f}, "
                f"candidate adds ${additional:,.2f}, "
                f"cap ${self.max_aggregate_margin:,.2f}",
                Priority.WARN,
            )
            return True
        return False

    def _notify(self, subject: str, body: str, priority: Priority) -> None:
        if self.notifier is None:
            return
        try:
            self.notifier.send(subject, body, priority)
        except Exception:
            pass


class TraderManager:
    """Runs N SwingTraders for one tenant. Add instruments at any time.

    Feeds are optional (only paper mode uses `broker.tick(bid, ask)` via feed);
    for live mode, order fills happen at the exchange and step() just polls.
    """

    def __init__(
        self,
        tenant_id: str,
        store: StateStore,
        governor: Optional[AccountMarginGovernor] = None,
        trade_log: Optional[TradeLog] = None,
        kill_switch: Optional[KillSwitch] = None,
        notifier: Optional[Notifier] = None,
    ):
        self.tenant_id = tenant_id
        self.store = store
        self.governor = governor
        self.trade_log = trade_log
        self.kill_switch = kill_switch
        self.notifier = notifier
        self.slots: dict[str, InstrumentSlot] = {}

    def add_instrument(
        self,
        symbol: str,
        broker: Broker,
        feed: Optional[object] = None,
    ) -> InstrumentSlot:
        trader = SwingTrader(
            broker, self.store, self.tenant_id, symbol,
            trade_log=self.trade_log,
            kill_switch=self.kill_switch,
            notifier=self.notifier,
        )
        # Governor hook: override _maybe_scale_up to consult the governor first.
        if self.governor is not None:
            orig_maybe_scale_up = trader._maybe_scale_up
            def gated_scale_up(slot=None):
                slot = self.slots.get(symbol)
                if slot is None:
                    return orig_maybe_scale_up()
                if self.governor.veto_scale_up(list(self.slots.values()), slot):
                    return  # veto — do not grow this cycle
                orig_maybe_scale_up()
            trader._maybe_scale_up = gated_scale_up
        slot = InstrumentSlot(symbol=symbol, broker=broker, trader=trader, feed=feed)
        self.slots[symbol] = slot
        return slot

    def remove_instrument(self, symbol: str) -> None:
        slot = self.slots.pop(symbol, None)
        if slot is None:
            return
        if slot.feed is not None:
            try: slot.feed.stop()
            except Exception: pass

    def reconcile_all(self) -> None:
        for slot in self.slots.values():
            slot.trader.reconcile()

    def step_all(self, prices: Optional[dict[str, float]] = None) -> None:
        """Step each trader once. `prices` optionally provides per-symbol mark
        prices; if omitted for a symbol, uses feed.latest_ticker()['price'].
        Live mode passes nothing since fills happen at the exchange."""
        prices = prices or {}
        for symbol, slot in self.slots.items():
            price = prices.get(symbol)
            if price is None and slot.feed is not None:
                t = slot.feed.latest_ticker()
                if t is not None:
                    price = t.get("price")
            if price is None:
                continue
            slot.trader.step(float(price))

    def snapshot_all(self) -> dict:
        """Aggregate snapshot for the account governor / dashboard consumption."""
        aggregate = {"tenant": self.tenant_id, "instruments": {}, "total_margin": 0.0, "total_equity": 0.0}
        for symbol, slot in self.slots.items():
            snap = getattr(slot.broker, "snapshot", lambda: {})()
            aggregate["instruments"][symbol] = snap
            aggregate["total_margin"] += float(snap.get("margin_used") or snap.get("initial_margin") or 0)
            aggregate["total_equity"] += float(snap.get("equity") or 0)
        return aggregate
