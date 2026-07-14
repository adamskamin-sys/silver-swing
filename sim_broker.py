"""
sim_broker.py — pure in-memory simulation broker for backtest and grid-search.

Written fresh for WS3 (2026-07-14) per auditor's B2 recommendation: a small
module we own end-to-end that has NO import path to the live client, no
tenant scope writes, no reachability into `_derive_live_tenant`. Enforced by
`tests/test_sim_broker_cannot_reach_live.py` — 5 tripwires that MUST stay
green forever, in CI.

Contract (used by backtest.py, expert_tuner.py, champion_challenger.py,
run_champion_challenger.py, run_go_live_check.py, scripts/run_backtest.py,
tune_reentry_thresholds.py):

    Constructor:
        SimBroker(cfg: SimConfig)

    Order lifecycle:
        place_limit(side, qty, price, post_only=False) -> order_id
        place_market(side, qty) -> order_id
        cancel(order_id) -> None
        order_status(order_id) -> dict
        tick(best_bid, best_ask) -> list[SimOrder]   # marches simulation, returns newly-filled orders

    Read-only accessors:
        position_qty() -> int
        equity() -> float
        unrealized_pnl() -> float
        margin_used() -> float
        snapshot() -> dict

    State portability (used by main.py to persist across restarts in the
    paper mode this module is REPLACING; retained for behavioral parity
    with backtest fixtures):
        to_state_dict() -> dict
        restore_from_state_dict(d) -> None

    Misc:
        reset(starting_balance: Optional[float] = None) -> None
        set_external_day_range(high_24h, low_24h) -> None
        set_pending_source(source, strategy_id=None) -> None

    Exposed properties (read directly by tests / backtest analytics):
        cfg, balance, realized_pnl, fees_paid, position (with .qty),
        lots, open_orders, history, high_water_mark, max_drawdown,
        _halted, _halt_reason, _last_mark

DELIBERATE non-imports (proven by tests/test_sim_broker_cannot_reach_live.py):
    - Never imports `broker` (CoinbaseBroker lives there — no live-order path).
    - Never imports `state_store` (no tenant scope writes possible).
    - Never imports `main` (no _derive_live_tenant reachability).

If a future caller needs one of those, they wire it OUTSIDE SimBroker.
SimBroker itself is a pure Python object that never touches the network.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


# ---- Data model ----------------------------------------------------------

@dataclass
class SimOrder:
    """One resting or filled order in the sim book."""
    order_id: str
    side: str                        # "BUY" | "SELL"
    qty: int
    limit_price: float
    status: str = "OPEN"             # OPEN | FILLED | CANCELLED
    filled_qty: int = 0
    fill_price: Optional[float] = None
    placed_at: float = 0.0
    filled_at: Optional[float] = None
    fee_paid: float = 0.0
    source: str = "unknown"          # "strategy" | "sleeve:<id>" | "manual"
    strategy_id: Optional[str] = None


@dataclass
class SimPosition:
    qty: int = 0                     # signed — LONG > 0, SHORT < 0
    avg_entry: float = 0.0           # 0 when flat


@dataclass
class SimLot:
    """One buy = one FIFO lot. Enables per-lot P&L on partial sells."""
    id: str
    qty: int
    entry_price: float
    entry_ts: float
    source: str = "unknown"
    strategy_id: Optional[str] = None


@dataclass
class SimConfig:
    """Constructor arg — mirrors PaperConfig for backtest-fixture parity.
    Field names chosen deliberately: existing backtest.py fixtures pass
    a PaperConfig; a rename-scan will substitute SimConfig cleanly."""
    product_id: str
    contract_size: float
    tick_size: float
    fee_per_fill: float              # per-contract fee (empirical from live preview or spec §2A)
    margin_per_contract: float
    starting_balance: float
    slippage_ticks: float = 0.0      # 0 = optimistic; increase for pessimistic fills


# ---- SimBroker -----------------------------------------------------------

class SimBroker:
    """Pure Python fill simulator. No I/O. No live imports.

    Fills are marched by the driver via `tick(best_bid, best_ask)` — every
    OPEN order checks: BUY fills when best_ask <= limit_price, SELL fills
    when best_bid >= limit_price. Market orders fill at the opposing top.
    """

    def __init__(self, cfg: SimConfig):
        self.cfg = cfg
        self.balance: float = float(cfg.starting_balance)
        self.realized_pnl: float = 0.0
        self.fees_paid: float = 0.0
        self.position = SimPosition()
        self.lots: list[SimLot] = []
        self.open_orders: dict[str, SimOrder] = {}
        self.history: list[SimOrder] = []
        self.high_water_mark: float = float(cfg.starting_balance)
        self.max_drawdown: float = 0.0
        self._halted: bool = False
        self._halt_reason: Optional[str] = None
        self._last_mark: float = 0.0
        self._pending_source: str = "unknown"
        self._pending_strategy_id: Optional[str] = None
        self._ext_high_24h: Optional[float] = None
        self._ext_low_24h: Optional[float] = None

    # --- Order entry -----------------------------------------------------

    def place_limit(self, side: str, qty: int, price: float,
                    post_only: bool = False) -> str:
        """Place a limit order. post_only is accepted for signature parity
        with CoinbaseBroker but a sim has no maker/taker distinction."""
        # Silently refuse orders when halted (return an empty order-id).
        # Raising here breaks the backtest engine's halt-and-break flow
        # because trader.step() runs BEFORE the loop checks broker._halted
        # (backtest.py:146-150).
        if self._halted:
            return ""
        side = side.upper()
        if side not in ("BUY", "SELL"):
            raise ValueError(f"invalid side: {side}")
        if qty <= 0:
            raise ValueError(f"qty must be > 0, got {qty}")
        oid = str(uuid.uuid4())
        order = SimOrder(
            order_id=oid, side=side, qty=int(qty),
            limit_price=float(price), status="OPEN",
            placed_at=time.time(),
            source=self._pending_source,
            strategy_id=self._pending_strategy_id,
        )
        self.open_orders[oid] = order
        return oid

    def place_market(self, side: str, qty: int) -> str:
        """Market order — fills next tick at best-opposing-top plus slippage.
        Represented internally as a limit with a very aggressive price so
        the tick loop matches it immediately."""
        # Silently refuse orders when halted (return an empty order-id).
        # Raising here breaks the backtest engine's halt-and-break flow
        # because trader.step() runs BEFORE the loop checks broker._halted
        # (backtest.py:146-150).
        if self._halted:
            return ""
        side = side.upper()
        if side not in ("BUY", "SELL"):
            raise ValueError(f"invalid side: {side}")
        # Aggressive price so tick() matches it on first eligible mark.
        px = float("inf") if side == "BUY" else 0.0
        oid = str(uuid.uuid4())
        order = SimOrder(
            order_id=oid, side=side, qty=int(qty),
            limit_price=px, status="OPEN",
            placed_at=time.time(),
            source=self._pending_source,
            strategy_id=self._pending_strategy_id,
        )
        self.open_orders[oid] = order
        return oid

    def cancel(self, order_id: str) -> None:
        order = self.open_orders.pop(order_id, None)
        if order is None:
            return
        order.status = "CANCELLED"
        self.history.append(order)

    def order_status(self, order_id: str) -> dict:
        # Check open first, then history.
        for o in list(self.open_orders.values()) + list(self.history):
            if o.order_id == order_id:
                return {
                    "status": o.status,
                    "filled_qty": o.filled_qty,
                    "average_filled_price": o.fill_price,
                    "fee_paid": o.fee_paid,
                }
        return {"status": "UNKNOWN"}

    # --- Simulation tick -------------------------------------------------

    def tick(self, best_bid: float, best_ask: float) -> list[SimOrder]:
        """Advance the sim. For every OPEN order, check if the mark
        crosses the limit; if so, mark FILLED. Returns the list of orders
        that filled this tick."""
        if self._halted:
            return []
        mark = (float(best_bid) + float(best_ask)) / 2.0
        self._last_mark = mark
        newly_filled: list[SimOrder] = []
        to_close: list[str] = []
        tick = float(self.cfg.tick_size or 0.0001)
        slip = float(self.cfg.slippage_ticks or 0.0) * tick

        for oid, o in self.open_orders.items():
            if o.status != "OPEN":
                continue
            hit = False
            fill_px = 0.0
            if o.side == "BUY":
                # BUY fills at best_ask or better; slippage pushes it up (worse).
                if float(best_ask) <= o.limit_price:
                    fill_px = float(best_ask) + slip
                    hit = True
            else:  # SELL
                if float(best_bid) >= o.limit_price:
                    fill_px = float(best_bid) - slip
                    hit = True
            if not hit:
                continue
            o.status = "FILLED"
            o.filled_qty = o.qty
            o.fill_price = round(fill_px, 8)
            o.filled_at = time.time()
            o.fee_paid = float(self.cfg.fee_per_fill) * o.qty
            self.fees_paid += o.fee_paid
            self._apply_fill(o)
            newly_filled.append(o)
            to_close.append(oid)

        for oid in to_close:
            self.history.append(self.open_orders.pop(oid))

        # Margin-call auto-halt — MTM check, not a fill event. A price
        # crash against a resting long position can blow the account
        # without a single order filling. Behavioral parity with
        # paper_broker._check_margin_call() (paper_broker.py:369).
        self._check_margin_call()

        # Update HWM + drawdown on the marked-to-market equity curve.
        eq = self.equity()
        if eq > self.high_water_mark:
            self.high_water_mark = eq
        dd = self.high_water_mark - eq
        if dd > self.max_drawdown:
            self.max_drawdown = dd
        return newly_filled

    def _apply_fill(self, order: SimOrder) -> None:
        """Update balance, position, lots on a filled order.
        FIFO lot bookkeeping: BUY opens a lot; SELL consumes lots oldest-first."""
        cs = float(self.cfg.contract_size)
        if order.side == "BUY":
            self.balance -= order.fill_price * order.qty * cs
            self.balance -= order.fee_paid
            lot = SimLot(
                id=order.order_id, qty=order.qty,
                entry_price=order.fill_price, entry_ts=order.filled_at or time.time(),
                source=order.source, strategy_id=order.strategy_id,
            )
            self.lots.append(lot)
            self._recompute_position()
        else:  # SELL
            self.balance += order.fill_price * order.qty * cs
            self.balance -= order.fee_paid
            remaining = order.qty
            realized = 0.0
            while remaining > 0 and self.lots:
                lot = self.lots[0]
                take = min(remaining, lot.qty)
                realized += (order.fill_price - lot.entry_price) * take * cs
                lot.qty -= take
                remaining -= take
                if lot.qty <= 0:
                    self.lots.pop(0)
            self.realized_pnl += realized
            self._recompute_position()

    def _recompute_position(self) -> None:
        qty = sum(l.qty for l in self.lots)
        if qty == 0:
            self.position = SimPosition(qty=0, avg_entry=0.0)
            return
        total_cost = sum(l.qty * l.entry_price for l in self.lots)
        self.position = SimPosition(qty=qty, avg_entry=total_cost / qty)

    # --- Accessors -------------------------------------------------------

    def position_qty(self) -> int:
        return int(self.position.qty)

    def equity(self) -> float:
        """Balance + mark-to-market on open lots."""
        cs = float(self.cfg.contract_size)
        mtm = 0.0
        if self.lots and self._last_mark > 0:
            for l in self.lots:
                mtm += (self._last_mark - l.entry_price) * l.qty * cs
        return self.balance + mtm

    def unrealized_pnl(self) -> float:
        cs = float(self.cfg.contract_size)
        if not self.lots or self._last_mark <= 0:
            return 0.0
        return sum((self._last_mark - l.entry_price) * l.qty * cs for l in self.lots)

    def margin_used(self) -> float:
        return float(self.cfg.margin_per_contract) * abs(self.position.qty)

    def snapshot(self) -> dict:
        return {
            "balance": round(self.balance, 4),
            "equity": round(self.equity(), 4),
            "position_qty": self.position.qty,
            "position_avg": round(self.position.avg_entry, 6),
            "realized_pnl": round(self.realized_pnl, 4),
            "unrealized_pnl": round(self.unrealized_pnl(), 4),
            "fees_paid": round(self.fees_paid, 4),
            "high_water_mark": round(self.high_water_mark, 4),
            "max_drawdown": round(self.max_drawdown, 4),
            "open_orders": len(self.open_orders),
            "filled_orders": len(self.history),
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "last_mark": self._last_mark,
        }

    # --- Portability -----------------------------------------------------

    def to_state_dict(self) -> dict:
        """Serialize for cross-restart persistence in backtest fixtures."""
        return {
            "balance": self.balance,
            "realized_pnl": self.realized_pnl,
            "fees_paid": self.fees_paid,
            "position": {"qty": self.position.qty, "avg_entry": self.position.avg_entry},
            "lots": [
                {"id": l.id, "qty": l.qty, "entry_price": l.entry_price,
                 "entry_ts": l.entry_ts, "source": l.source,
                 "strategy_id": l.strategy_id}
                for l in self.lots
            ],
            "high_water_mark": self.high_water_mark,
            "max_drawdown": self.max_drawdown,
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "last_mark": self._last_mark,
        }

    def restore_from_state_dict(self, d: dict) -> None:
        self.balance = float(d.get("balance", self.cfg.starting_balance))
        self.realized_pnl = float(d.get("realized_pnl", 0.0))
        self.fees_paid = float(d.get("fees_paid", 0.0))
        pos = d.get("position") or {}
        self.position = SimPosition(qty=int(pos.get("qty", 0)),
                                     avg_entry=float(pos.get("avg_entry", 0.0)))
        self.lots = [
            SimLot(id=x["id"], qty=int(x["qty"]),
                   entry_price=float(x["entry_price"]),
                   entry_ts=float(x.get("entry_ts", 0.0)),
                   source=x.get("source", "unknown"),
                   strategy_id=x.get("strategy_id"))
            for x in (d.get("lots") or [])
        ]
        self.high_water_mark = float(d.get("high_water_mark", self.cfg.starting_balance))
        self.max_drawdown = float(d.get("max_drawdown", 0.0))
        self._halted = bool(d.get("halted", False))
        self._halt_reason = d.get("halt_reason")
        self._last_mark = float(d.get("last_mark", 0.0))

    # --- Housekeeping ---------------------------------------------------

    def reset(self, starting_balance: Optional[float] = None) -> None:
        sb = float(starting_balance) if starting_balance is not None else float(self.cfg.starting_balance)
        self.balance = sb
        self.realized_pnl = 0.0
        self.fees_paid = 0.0
        self.position = SimPosition()
        self.lots = []
        self.open_orders = {}
        self.history = []
        self.high_water_mark = sb
        self.max_drawdown = 0.0
        self._halted = False
        self._halt_reason = None
        self._last_mark = 0.0

    def set_external_day_range(self, high_24h, low_24h) -> None:
        try:
            self._ext_high_24h = float(high_24h) if high_24h is not None else None
        except (TypeError, ValueError):
            self._ext_high_24h = None
        try:
            self._ext_low_24h = float(low_24h) if low_24h is not None else None
        except (TypeError, ValueError):
            self._ext_low_24h = None

    def set_pending_source(self, source: str, strategy_id: Optional[str] = None) -> None:
        self._pending_source = source or "unknown"
        self._pending_strategy_id = strategy_id

    # --- Guards ---------------------------------------------------------

    def _require_running(self) -> None:
        if self._halted:
            raise RuntimeError(f"SimBroker halted: {self._halt_reason}")

    def halt(self, reason: str) -> None:
        """Deliberate halt — e.g. margin call in the sim."""
        self._halted = True
        self._halt_reason = reason

    def _check_margin_call(self) -> None:
        """Auto-halt when equity falls below required margin. Cancels every
        resting order on halt (matches paper_broker semantics — no new fills
        can occur while halted anyway)."""
        if self.position.qty == 0:
            return
        if self.equity() < self.margin_used():
            self._halted = True
            self._halt_reason = (
                f"margin call: equity ${self.equity():.2f} < "
                f"required ${self.margin_used():.2f}"
            )
            for oid in list(self.open_orders.keys()):
                self.cancel(oid)
