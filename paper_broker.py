"""
PaperBroker — simulated Broker with the real fee + margin model (spec §12 step 3, §10A).

Implements the same Broker Protocol as CoinbaseBroker so the strategy code is
identical between paper and live. Nothing here talks to Coinbase. Fills are
simulated from a price feed the driver supplies via `tick(best_bid, best_ask)`.

The whole point is that "does the strategy actually make money after costs?" gets
answered honestly. To that end, the cost model uses empirical values (§3A, §2A):
  - fee_per_fill: the actual client_commission from a live preview
  - contract_size / tick_size: from the live product spec
  - margin_per_contract: from the live futures_balance_summary
  - starting_balance: whatever the user "deposits" — a real constraint, not decoration.
    A margin-call simulation halts the run if equity would fall below required margin.

Not covered in this MVP (deliberate scope): funding (dated futures don't have it,
so N/A for SLR-CDE), slippage on limit fills (parameter present, default 0), and
partial fills (all-or-nothing per spec §2's "full fills only" rule).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PaperOrder:
    order_id: str
    product_id: str
    side: str
    qty: int
    limit_price: float
    status: str = "OPEN"
    filled_qty: int = 0
    fill_price: Optional[float] = None
    placed_at: float = 0.0
    filled_at: Optional[float] = None
    fee_paid: float = 0.0


@dataclass
class PaperPosition:
    product_id: str
    qty: int = 0             # signed — LONG > 0, SHORT < 0
    avg_entry: float = 0.0   # 0 when flat


@dataclass
class PaperConfig:
    product_id: str
    contract_size: float
    tick_size: float
    fee_per_fill: float           # per-contract fee (from CoinbaseBroker.preview_order or spec §2A empirical)
    margin_per_contract: float
    starting_balance: float
    slippage_ticks: float = 0.0   # additional ticks against the trader on each fill; 0 = optimistic


class PaperBroker:
    """Same Broker Protocol as CoinbaseBroker, simulated fills."""

    def __init__(self, cfg: PaperConfig):
        self.cfg = cfg
        self.balance = cfg.starting_balance
        self.realized_pnl = 0.0
        self.fees_paid = 0.0
        self.open_orders: dict[str, PaperOrder] = {}
        self.history: list[PaperOrder] = []
        self.position = PaperPosition(cfg.product_id)
        self.high_water_mark = cfg.starting_balance
        self.max_drawdown = 0.0
        self._last_mark = 0.0
        self._halted = False
        self._halt_reason: Optional[str] = None

    # ---- Broker Protocol -------------------------------------------------

    def place_limit(self, side: str, qty: int, price: float) -> str:
        if self._halted:
            raise RuntimeError(f"paper broker halted: {self._halt_reason}")
        s = side.upper()
        if s not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, got {side!r}")
        oid = f"paper-{uuid.uuid4()}"
        self.open_orders[oid] = PaperOrder(
            order_id=oid,
            product_id=self.cfg.product_id,
            side=s,
            qty=int(qty),
            limit_price=float(price),
            placed_at=time.time(),
        )
        return oid

    def order_status(self, order_id: str) -> dict:
        o = self.open_orders.get(order_id) or self._find_in_history(order_id)
        if o is None:
            return {"status": "UNKNOWN", "filled_qty": 0}
        return {
            "status": o.status,
            "filled_qty": o.filled_qty,
            "average_filled_price": o.fill_price,
        }

    def cancel(self, order_id: str) -> None:
        o = self.open_orders.pop(order_id, None)
        if o is None:
            # Idempotent — cancelling an already-cancelled/filled order is a no-op
            return
        o.status = "CANCELLED"
        self.history.append(o)

    def position_qty(self) -> int:
        return self.position.qty

    # ---- Fill simulation -------------------------------------------------

    def tick(self, best_bid: float, best_ask: float) -> list[PaperOrder]:
        """Feed one price observation. Fills any resting orders that the market has crossed.

        Returns the list of orders that filled on this tick (may be empty).
        """
        if self._halted:
            return []
        # Track the mid so unrealized P&L stays fresh even when nothing fills
        self._last_mark = (best_bid + best_ask) / 2

        filled_now: list[PaperOrder] = []
        for oid in list(self.open_orders.keys()):
            o = self.open_orders[oid]
            crossed = False
            fill_price = o.limit_price

            if o.side == "BUY" and best_ask <= o.limit_price:
                crossed = True
                fill_price = o.limit_price + self.cfg.slippage_ticks * self.cfg.tick_size
            elif o.side == "SELL" and best_bid >= o.limit_price:
                crossed = True
                fill_price = o.limit_price - self.cfg.slippage_ticks * self.cfg.tick_size

            if crossed:
                self._process_fill(o, fill_price)
                self.open_orders.pop(oid)
                self.history.append(o)
                filled_now.append(o)
                self._check_margin_call()
                if self._halted:
                    break

        # HWM / drawdown must track equity even when no fill happens — a mark
        # against the resting position moves equity via unrealized P&L.
        self._update_drawdown()
        return filled_now

    def _process_fill(self, o: PaperOrder, fill_price: float) -> None:
        o.status = "FILLED"
        o.filled_qty = o.qty
        o.fill_price = fill_price
        o.filled_at = time.time()

        fee = self.cfg.fee_per_fill * o.qty
        o.fee_paid = fee
        self.fees_paid += fee
        self.balance -= fee

        if o.side == "BUY":
            self._add_long(o.qty, fill_price)
        else:
            self._reduce_long(o.qty, fill_price)

        self._update_drawdown()

    def _add_long(self, qty: int, price: float) -> None:
        """BUY qty at price: either open/add to LONG, or close/reverse a SHORT."""
        p = self.position
        if p.qty >= 0:
            new_qty = p.qty + qty
            p.avg_entry = price if p.qty == 0 else (p.qty * p.avg_entry + qty * price) / new_qty
            p.qty = new_qty
        else:
            covered = min(qty, -p.qty)
            self.realized_pnl += (p.avg_entry - price) * self.cfg.contract_size * covered
            p.qty += qty
            if p.qty == 0:
                p.avg_entry = 0.0
            elif p.qty > 0:
                # flipped from short to long — remainder is a fresh long at the fill price
                p.avg_entry = price

    def _reduce_long(self, qty: int, price: float) -> None:
        """SELL qty at price: either close/reduce LONG, or open/add to SHORT."""
        p = self.position
        if p.qty > 0:
            covered = min(qty, p.qty)
            self.realized_pnl += (price - p.avg_entry) * self.cfg.contract_size * covered
            p.qty -= qty
            if p.qty == 0:
                p.avg_entry = 0.0
            elif p.qty < 0:
                p.avg_entry = price
        else:
            # p.qty <= 0 : opening or adding to a short
            new_qty = p.qty - qty
            p.avg_entry = price if p.qty == 0 else (abs(p.qty) * p.avg_entry + qty * price) / abs(new_qty)
            p.qty = new_qty

    def unrealized_pnl(self) -> float:
        if self.position.qty == 0 or self._last_mark == 0:
            return 0.0
        if self.position.qty > 0:
            return (self._last_mark - self.position.avg_entry) * self.cfg.contract_size * self.position.qty
        return (self.position.avg_entry - self._last_mark) * self.cfg.contract_size * abs(self.position.qty)

    def equity(self) -> float:
        return self.balance + self.realized_pnl + self.unrealized_pnl()

    def margin_used(self) -> float:
        return abs(self.position.qty) * self.cfg.margin_per_contract

    def _update_drawdown(self) -> None:
        eq = self.equity()
        if eq > self.high_water_mark:
            self.high_water_mark = eq
        drawdown = self.high_water_mark - eq
        if drawdown > self.max_drawdown:
            self.max_drawdown = drawdown

    def _check_margin_call(self) -> None:
        if self.position.qty == 0:
            return
        if self.equity() < self.margin_used():
            self._halt(
                f"margin call: equity ${self.equity():.2f} < required ${self.margin_used():.2f}"
            )

    def _halt(self, reason: str) -> None:
        self._halted = True
        self._halt_reason = reason
        # Cancel all resting orders on halt
        for oid in list(self.open_orders.keys()):
            self.cancel(oid)

    def _find_in_history(self, order_id: str) -> Optional[PaperOrder]:
        for o in self.history:
            if o.order_id == order_id:
                return o
        return None

    # ---- Snapshot for dashboard / audit ----------------------------------

    def snapshot(self) -> dict:
        return {
            "starting_balance": self.cfg.starting_balance,
            "balance": self.balance,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl(),
            "equity": self.equity(),
            "fees_paid": self.fees_paid,
            "position_qty": self.position.qty,
            "position_avg_entry": self.position.avg_entry,
            "margin_used": self.margin_used(),
            "available_margin": self.equity() - self.margin_used(),
            "high_water_mark": self.high_water_mark,
            "max_drawdown": self.max_drawdown,
            "open_orders": len(self.open_orders),
            "fills": sum(1 for o in self.history if o.status == "FILLED"),
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "last_mark": self._last_mark,
        }
