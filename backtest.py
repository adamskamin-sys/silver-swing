"""
Backtest engine — one engine, two viewpoints (spec §9C, §12 step 5).

The rule from the spec: the backtest must run THE SAME strategy code through
the same PaperBroker and fee model, just against historical candles instead
of a live feed. That reuse is what makes a good backtest trustworthy — this
IS your strategy, not a lookalike. Fork it and it starts lying to you.

Two doorways use this same engine:
  - Paper section — validate/tune a strategy at high speed against history.
  - Real-money section — "preview before you arm" button per instrument.

Candle → tick model:
  For each candle, walk the price in the order it plausibly moved:
    green (close >= open):  open → low → high → close
    red  (close < open):    open → high → low → close
  Each of those prices is fed to PaperBroker.tick(bid=p, ask=p) — treating
  the price as both sides is a spread-blind simplification. Callers who care
  about spread realism can pre-inflate the spread by widening bid/ask around
  the mid before feeding. Then trader.step(close) runs the state machine
  once per candle at the close price (matches how a 1m-bar strategy would
  react).

Metrics that come out mirror the spec §9C list: equity curve, realized/
unrealized/fees broken out, max drawdown, cycle count, fill count.

Deliberately out of scope for MVP:
- Multi-strategy compare-all leaderboard (spec §9C). Layer that on top by
  running the engine once per strategy over identical data.
- Multi-regime slicing. Layer on top by running the engine over three
  different windows.
- Slippage modeling — PaperConfig.slippage_ticks handles it; caller sets it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

# WS3 (2026-07-14): switched from paper_broker to sim_broker (fresh module
# with no live-client / no state-store imports; proven by
# tests/test_sim_broker_cannot_reach_live.py). Aliased to old names so this
# file's downstream references don't need touching. paper_broker.py will be
# deleted at end of Phase 6 once all consumers + tests have migrated.
from sim_broker import SimBroker as PaperBroker, SimConfig as PaperConfig


@dataclass
class Candle:
    ts: float                # unix time of the candle's start
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class EquityPoint:
    ts: float
    close: float
    equity: float
    realized_pnl: float
    unrealized_pnl: float
    fees_paid: float
    position_qty: int
    cycles: int
    halted: bool


@dataclass
class BacktestResult:
    starting_balance: float
    final_equity: float
    total_return: float
    total_return_pct: float
    realized_pnl: float
    unrealized_pnl: float
    fees_paid: float
    max_drawdown: float
    max_drawdown_pct: float
    cycles: int
    fills: int
    halted: bool
    halt_reason: Optional[str]
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    price_start: Optional[float] = None
    price_end: Optional[float] = None
    candle_count: int = 0
    equity_curve: list[EquityPoint] = field(default_factory=list)
    # [crew:#5] Populated when the run used unrealistic (optimistic) assumptions
    # — zero slippage and/or zero fees. A caller/agent should surface these so a
    # frictionless backtest isn't mistaken for a real edge.
    realism_warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        base = (
            f"start ${self.starting_balance:,.2f} → end ${self.final_equity:,.2f} "
            f"(${self.total_return:+,.2f} / {self.total_return_pct:+.2f}%) | "
            f"realized ${self.realized_pnl:+,.2f} | fees ${self.fees_paid:,.2f} | "
            f"max_dd ${self.max_drawdown:,.2f} ({self.max_drawdown_pct:.2f}%) | "
            f"cycles {self.cycles} | fills {self.fills} | "
            f"{'HALTED: ' + (self.halt_reason or '?') if self.halted else 'ran clean'}"
        )
        if self.realism_warnings:
            base += "  ⚠ " + " ".join(self.realism_warnings)
        return base


def _walk_candle(c: Candle) -> list[float]:
    """Sequence of prices touched during the candle, ordered plausibly by direction."""
    if c.close >= c.open:
        return [c.open, c.low, c.high, c.close]
    return [c.open, c.high, c.low, c.close]


def run_backtest(
    trader_factory: Callable[[PaperBroker], Any],
    paper_config: PaperConfig,
    candles: list[Candle],
) -> BacktestResult:
    """Run a strategy through historical candles.

    `trader_factory` receives a fresh PaperBroker and must return an object
    with .reconcile() and .step(price). The factory is where you inject the
    StateStore, TradeLog, and per-run SwingConfig — the engine doesn't touch
    those directly.
    """
    broker = PaperBroker(paper_config)
    trader = trader_factory(broker)
    trader.reconcile()

    curve: list[EquityPoint] = []

    for candle in candles:
        # [crew:#5] Run the state machine at EACH intrabar price, not only the
        # close. Previously stops were evaluated once per candle at the close,
        # so a wick that pierced your stop-loss and recovered by close was
        # scored as "never stopped out" — systematically inflating results for
        # any stop-based strategy. Stepping per walk price lets the stop fire on
        # the wick, which is closer to how the live loop (stepping ~1x/sec)
        # actually behaves. Fills already happen intrabar via broker.tick.
        for price in _walk_candle(candle):
            broker.tick(price, price)
            trader.step(price)
            if broker._halted:
                break

        curve.append(EquityPoint(
            ts=candle.ts,
            close=candle.close,
            equity=broker.equity(),
            realized_pnl=broker.realized_pnl,
            unrealized_pnl=broker.unrealized_pnl(),
            fees_paid=broker.fees_paid,
            position_qty=broker.position.qty,
            cycles=getattr(trader.s, "cycles", 0),
            halted=getattr(getattr(trader, "s", None), "state", None) is not None
                    and str(trader.s.state.value if hasattr(trader.s.state, "value") else trader.s.state) == "HALTED",
        ))

        if broker._halted:
            break

    start = paper_config.starting_balance
    final = broker.equity()
    price_min = min((c.low for c in candles), default=None)
    price_max = max((c.high for c in candles), default=None)
    # [crew:#5] Flag optimistic assumptions so a frictionless run isn't trusted.
    realism_warnings: list[str] = []
    if paper_config.slippage_ticks <= 0:
        realism_warnings.append(
            "slippage_ticks=0: fills are frictionless — set a realistic slippage before trusting the edge.")
    if paper_config.fee_per_fill <= 0:
        realism_warnings.append(
            "fee_per_fill=0: no trading costs modeled — results overstate profitability.")
    return BacktestResult(
        starting_balance=start,
        final_equity=final,
        total_return=final - start,
        total_return_pct=(final - start) / start * 100 if start else 0.0,
        realized_pnl=broker.realized_pnl,
        unrealized_pnl=broker.unrealized_pnl(),
        fees_paid=broker.fees_paid,
        max_drawdown=broker.max_drawdown,
        max_drawdown_pct=broker.max_drawdown / start * 100 if start else 0.0,
        cycles=getattr(trader.s, "cycles", 0),
        fills=sum(1 for o in broker.history if o.status == "FILLED"),
        halted=broker._halted,
        halt_reason=broker._halt_reason,
        price_min=price_min,
        price_max=price_max,
        price_start=candles[0].open if candles else None,
        price_end=candles[-1].close if candles else None,
        candle_count=len(candles),
        equity_curve=curve,
        realism_warnings=realism_warnings,
    )


# ============================================================================
# Historical candle fetch (Coinbase Advanced Trade)
# ============================================================================

# Coinbase granularity values, in seconds. The API's `granularity` param takes
# these string names; the seconds map lets us pick the right window size.
_GRANULARITY_SECONDS = {
    "ONE_MINUTE": 60,
    "FIVE_MINUTE": 300,
    "FIFTEEN_MINUTE": 900,
    "THIRTY_MINUTE": 1800,
    "ONE_HOUR": 3600,
    "TWO_HOUR": 7200,
    "SIX_HOUR": 21600,
    "ONE_DAY": 86400,
}


def fetch_candles(
    coinbase_client,
    product_id: str,
    start: datetime,
    end: datetime,
    granularity: str = "FIVE_MINUTE",
) -> list[Candle]:
    """Pull historical candles from Coinbase and convert to the engine's shape.

    The Advanced Trade candles endpoint caps at 350 candles per call. This
    helper pages transparently so a caller can request "last 30 days at
    five-minute granularity" (~8,600 candles) without thinking about it.
    """
    if granularity not in _GRANULARITY_SECONDS:
        raise ValueError(f"granularity must be one of {list(_GRANULARITY_SECONDS)}")
    per = _GRANULARITY_SECONDS[granularity]
    page_seconds = per * 300  # stay comfortably under 350
    all_candles: list[Candle] = []

    cursor = start
    while cursor < end:
        page_end = min(cursor + _seconds_delta(page_seconds), end)
        resp = coinbase_client.get_candles(
            product_id=product_id,
            start=str(int(cursor.timestamp())),
            end=str(int(page_end.timestamp())),
            granularity=granularity,
        )
        resp_d = resp.to_dict() if hasattr(resp, "to_dict") else resp
        for raw in resp_d.get("candles", []):
            all_candles.append(Candle(
                ts=float(raw.get("start", 0)),
                open=float(raw.get("open", 0)),
                high=float(raw.get("high", 0)),
                low=float(raw.get("low", 0)),
                close=float(raw.get("close", 0)),
                volume=float(raw.get("volume", 0)),
            ))
        cursor = page_end
        # Rate-limit courtesy pause
        time.sleep(0.05)

    # De-duplicate & sort ascending — Coinbase returns descending, and page
    # boundaries can overlap when the end aligns exactly with a candle start.
    seen: set[float] = set()
    unique: list[Candle] = []
    for c in sorted(all_candles, key=lambda c: c.ts):
        if c.ts in seen:
            continue
        seen.add(c.ts)
        unique.append(c)
    return unique


def _seconds_delta(seconds: int):
    from datetime import timedelta
    return timedelta(seconds=seconds)
