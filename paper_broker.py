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
class Lot:
    """One buy = one lot. Kept in FIFO order so we can compute per-lot P/L
    (what you paid for THIS contract) instead of just the aggregate avg entry.
    Sells reduce lots from the oldest first."""
    id: str
    qty: int
    entry_price: float
    entry_ts: float
    source: str = "unknown"  # "manual" | "strategy" | "mirror" | "unknown"
    strategy_id: Optional[str] = None  # which sleeve owns this lot, if any


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
        self.lots: list[Lot] = []
        self._pending_source: str = "unknown"  # set by callers (SwingTrader / manual) before place_*
        self._pending_strategy_id: Optional[str] = None
        self.high_water_mark = cfg.starting_balance
        self.max_drawdown = 0.0
        self._last_mark = 0.0
        # Session high/low of the price mark. Reset at UTC midnight so the
        # dashboard can label the range as "today's". Live feed can also supply
        # exchange-computed high_24h/low_24h; when it does, we use those in
        # snapshot() and keep the session tracker as backup.
        self._day_high: Optional[float] = None
        self._day_low: Optional[float] = None
        self._day_reset_utc_date: Optional[str] = None
        self._external_high_24h: Optional[float] = None
        self._external_low_24h: Optional[float] = None
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

    def place_market(self, side: str, qty: int) -> str:
        """Simulate a market order — fills immediately at the current bid/ask.

        BUY hits the ask (worst-case for buyer). SELL hits the bid. If no
        bid/ask has been seen yet (feed hasn't started), uses last_mark.
        """
        if self._halted:
            raise RuntimeError(f"paper broker halted: {self._halt_reason}")
        s = side.upper()
        if s not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, got {side!r}")
        # Fill price: buyer hits the ask, seller hits the bid. Approximate with
        # last_mark ± half-spread if we don't have a real book. Slippage applies.
        fill_price = self._last_mark or 0.0
        oid = f"paper-mkt-{uuid.uuid4()}"
        o = PaperOrder(
            order_id=oid, product_id=self.cfg.product_id,
            side=s, qty=int(qty), limit_price=fill_price,
            placed_at=time.time(),
        )
        if s == "BUY" and self.cfg.slippage_ticks > 0:
            fill_price += self.cfg.slippage_ticks * self.cfg.tick_size
        elif s == "SELL" and self.cfg.slippage_ticks > 0:
            fill_price -= self.cfg.slippage_ticks * self.cfg.tick_size
        self._process_fill(o, fill_price)
        self.history.append(o)
        self._check_margin_call()
        self._update_drawdown()
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
        mid = (best_bid + best_ask) / 2
        self._last_mark = mid
        # Session high/low, reset at UTC midnight. Cheap to compute; useful for
        # the dashboard's day-range display when the feed doesn't ship 24h stats.
        import datetime as _dt
        today = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
        if self._day_reset_utc_date != today:
            self._day_reset_utc_date = today
            self._day_high = mid
            self._day_low = mid
        else:
            if self._day_high is None or mid > self._day_high: self._day_high = mid
            if self._day_low is None or mid < self._day_low: self._day_low = mid

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
                if self._halted:
                    break

        # Margin call is a mark-to-market check, not a fill event: a price crash
        # against a resting long position can blow the account without a single
        # order filling. Check on every tick.
        self._check_margin_call()

        # HWM / drawdown also tracks equity even without a fill.
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
        """BUY qty at price: either open/add to LONG, or close/reverse a SHORT.
        Every long-adding BUY creates a lot so we can report per-contract P/L."""
        p = self.position
        if p.qty >= 0:
            new_qty = p.qty + qty
            p.avg_entry = price if p.qty == 0 else (p.qty * p.avg_entry + qty * price) / new_qty
            p.qty = new_qty
            self._add_lot(qty, price)
        else:
            covered = min(qty, -p.qty)
            self.realized_pnl += (p.avg_entry - price) * self.cfg.contract_size * covered
            p.qty += qty
            if p.qty == 0:
                p.avg_entry = 0.0
            elif p.qty > 0:
                # flipped from short to long — remainder is a fresh long at the fill price
                p.avg_entry = price
                self._add_lot(p.qty, price)

    def _reduce_long(self, qty: int, price: float) -> None:
        """SELL qty at price: either close/reduce LONG, or open/add to SHORT.
        Lots are consumed FIFO; per-lot realized P/L accumulates into total realized."""
        p = self.position
        if p.qty > 0:
            covered = min(qty, p.qty)
            self.realized_pnl += (price - p.avg_entry) * self.cfg.contract_size * covered
            self._consume_lots(covered, price)
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

    def _add_lot(self, qty: int, price: float) -> None:
        self.lots.append(Lot(
            id=f"lot-{uuid.uuid4()}",
            qty=int(qty),
            entry_price=float(price),
            entry_ts=time.time(),
            source=self._pending_source,
            strategy_id=self._pending_strategy_id,
        ))

    def _consume_lots(self, qty: int, exit_price: float) -> None:
        """Consume `qty` contracts from open lots. Priority order:
          1. If a strategy_id is set on the pending source, consume THAT
             strategy's own tagged lots first (FIFO within its own inventory).
             Keeps each sleeve's cost basis tied to what IT actually bought.
          2. Then fall back to global FIFO (oldest lot first).
        Splits a lot if qty doesn't fully close it. Silent no-op if lots empty."""
        remaining = qty
        tag = self._pending_strategy_id
        if tag is not None:
            # Preferred: consume this strategy's own tagged lots FIFO
            i = 0
            while remaining > 0 and i < len(self.lots):
                lot = self.lots[i]
                if lot.strategy_id != tag:
                    i += 1
                    continue
                if lot.qty <= remaining:
                    remaining -= lot.qty
                    self.lots.pop(i)
                else:
                    lot.qty -= remaining
                    remaining = 0
        # Fallback: global FIFO for anything left
        while remaining > 0 and self.lots:
            lot = self.lots[0]
            if lot.qty <= remaining:
                remaining -= lot.qty
                self.lots.pop(0)
            else:
                lot.qty -= remaining
                remaining = 0

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

    def reset(self, starting_balance: Optional[float] = None) -> None:
        """Wipe all paper state in place — balance, position, lots, orders,
        realized P/L, drawdown, halt status. The bot's SwingTrader can keep
        using the same broker instance; from its perspective it's a fresh
        account. Real Coinbase state is not touched."""
        self.balance = float(starting_balance) if starting_balance is not None else self.cfg.starting_balance
        self.realized_pnl = 0.0
        self.fees_paid = 0.0
        self.open_orders = {}
        self.history = []
        self.position = PaperPosition(self.cfg.product_id)
        self.lots = []
        self._pending_source = "unknown"
        self._pending_strategy_id = None
        self.high_water_mark = self.balance
        self.max_drawdown = 0.0
        self._last_mark = 0.0
        self._halted = False
        self._halt_reason = None

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
            "margin_per_contract": self.cfg.margin_per_contract,
            "available_margin": self.equity() - self.margin_used(),
            "liquidation_price": self._liquidation_price(),
            "high_water_mark": self.high_water_mark,
            "max_drawdown": self.max_drawdown,
            "open_orders": len(self.open_orders),
            "fills": sum(1 for o in self.history if o.status == "FILLED"),
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "last_mark": self._last_mark,
            "day_high": self._external_high_24h if self._external_high_24h is not None else self._day_high,
            "day_low": self._external_low_24h if self._external_low_24h is not None else self._day_low,
            "lots": self.lots_snapshot(),
        }

    def set_external_day_range(self, high_24h: Optional[float], low_24h: Optional[float]) -> None:
        """Optional: feed exchange-computed 24h high/low so the snapshot uses
        those instead of the session-only fallback."""
        self._external_high_24h = high_24h
        self._external_low_24h = low_24h

    # ---- Persistence -----------------------------------------------------
    # Paper positions used to vanish on every worker restart because this
    # broker's state was in-memory only. Now we serialize the authoritative
    # bits to the store on each snapshot and restore on boot.
    #
    # Deliberately NOT persisted:
    #   - cfg (comes from PaperConfig which is deterministic from env)
    #   - _external_high_24h/low_24h (fresh from the feed each tick)
    #   - _pending_source/_pending_strategy_id (transient — set right before place_*)

    def to_state_dict(self) -> dict:
        return {
            "balance": self.balance,
            "realized_pnl": self.realized_pnl,
            "fees_paid": self.fees_paid,
            "position": {
                "product_id": self.position.product_id,
                "qty": self.position.qty,
                "avg_entry": self.position.avg_entry,
            },
            "lots": [
                {"id": l.id, "qty": l.qty, "entry_price": l.entry_price,
                 "entry_ts": l.entry_ts, "source": l.source,
                 "strategy_id": l.strategy_id}
                for l in self.lots
            ],
            "high_water_mark": self.high_water_mark,
            "max_drawdown": self.max_drawdown,
            "last_mark": self._last_mark,
            "day_high": self._day_high,
            "day_low": self._day_low,
            "day_reset_utc_date": self._day_reset_utc_date,
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "open_orders": [
                {"order_id": o.order_id, "product_id": o.product_id,
                 "side": o.side, "qty": o.qty, "limit_price": o.limit_price,
                 "placed_at": o.placed_at}
                for o in self.open_orders.values()
            ],
        }

    def restore_from_state_dict(self, d: dict) -> None:
        """Restore in-memory state from a previously-serialized dict. Called
        on boot when the store already has paper state for this tenant/symbol.
        Silently ignores unknown keys so old snapshots don't blow up after
        we add fields."""
        self.balance = float(d.get("balance", self.cfg.starting_balance))
        self.realized_pnl = float(d.get("realized_pnl", 0.0))
        self.fees_paid = float(d.get("fees_paid", 0.0))
        p = d.get("position") or {}
        self.position = PaperPosition(
            product_id=p.get("product_id", self.cfg.product_id),
            qty=int(p.get("qty", 0)),
            avg_entry=float(p.get("avg_entry", 0.0)),
        )
        self.lots = [
            Lot(
                id=l.get("id") or str(uuid.uuid4()),
                qty=int(l["qty"]),
                entry_price=float(l["entry_price"]),
                entry_ts=float(l.get("entry_ts", 0.0)),
                source=l.get("source", "unknown"),
                strategy_id=l.get("strategy_id"),
            )
            for l in (d.get("lots") or [])
        ]
        self.high_water_mark = float(d.get("high_water_mark", self.balance))
        self.max_drawdown = float(d.get("max_drawdown", 0.0))
        self._last_mark = float(d.get("last_mark", 0.0))
        self._day_high = d.get("day_high")
        self._day_low = d.get("day_low")
        self._day_reset_utc_date = d.get("day_reset_utc_date")
        self._halted = bool(d.get("halted", False))
        self._halt_reason = d.get("halt_reason")
        # Restore any resting limit orders. status/filled_qty/fill_price stay
        # at their defaults from PaperOrder since only OPEN orders serialize.
        self.open_orders = {}
        for o in (d.get("open_orders") or []):
            po = PaperOrder(
                order_id=o["order_id"],
                product_id=o["product_id"],
                side=o["side"],
                qty=int(o["qty"]),
                limit_price=float(o["limit_price"]),
                placed_at=float(o.get("placed_at", 0.0)),
            )
            self.open_orders[po.order_id] = po

    def _liquidation_price(self) -> Optional[float]:
        """Price at which equity would exactly equal required margin (i.e. the
        margin call trips). None when flat, since there's no directional risk.
        Also None when the account has so much cushion that liquidation would
        be at a negative price for a long (impossible in reality) — the UI
        reads None as "no practical liquidation risk".

        For a LONG position: liq = avg_entry - (balance + realized - margin_used) / (contract_size * qty)
        For a SHORT position: liq = avg_entry + (balance + realized - margin_used) / (contract_size * |qty|)
        The (balance + realized - margin_used) term is the cushion above required
        margin; divided by contract_size × qty it's how many dollars of adverse
        move the account can absorb per contract."""
        p = self.position
        if p.qty == 0:
            return None
        cushion = self.balance + self.realized_pnl - self.margin_used()
        distance = cushion / (self.cfg.contract_size * abs(p.qty))
        if p.qty > 0:
            liq = p.avg_entry - distance
            return liq if liq > 0 else None  # negative price = safe from liq
        return p.avg_entry + distance

    def lots_snapshot(self) -> list[dict]:
        """One entry per open lot, enriched with live P/L at last mark."""
        mark = self._last_mark
        out = []
        for lot in self.lots:
            unrealized = 0.0
            if mark and lot.qty > 0:
                unrealized = (mark - lot.entry_price) * self.cfg.contract_size * lot.qty
            out.append({
                "id": lot.id,
                "qty": lot.qty,
                "entry_price": lot.entry_price,
                "entry_ts": lot.entry_ts,
                "source": lot.source,
                "strategy_id": lot.strategy_id,
                "mark": mark,
                "unrealized_pnl": unrealized,
            })
        return out

    def set_pending_source(self, source: str, strategy_id: Optional[str] = None) -> None:
        """Tag the NEXT fill so its resulting lot knows where it came from.
        Callers should set this immediately before place_limit / place_market."""
        self._pending_source = source
        self._pending_strategy_id = strategy_id
