"""Execution / transaction-cost analysis (crew).

The gap between a backtest and live P&L is mostly EXECUTION. This scores every
fill in the trade log for what it actually cost you vs what the model assumed:
  - slippage: fill price vs the price you placed the limit at (adverse = paid up)
  - adverse selection: did the mark move AGAINST you right after you filled?
    (bought the local top / sold the local bottom = you were the liquidity of
    last resort — the tape ran you over)
  - maker/taker mix: are your post-only orders actually capturing spread?
  - implementation shortfall: modeled fill vs realized fill, in dollars.

Read-only. Pairs order_placed <-> order_filled events by order_id; a caller can
supply a mark-lookup for adverse-selection scoring. No new API calls.
"""

from __future__ import annotations

from statistics import mean, median, pstdev
from typing import Callable, Optional


def fills_from_events(events) -> list[dict]:
    """Pair order_placed with the matching order_filled by order_id."""
    placed = {}
    fills = []
    for e in events:
        et = str(e.get("event_type") or "")
        oid = e.get("order_id")
        if et == "order_placed" and oid:
            placed[oid] = e
        elif et == "order_filled" and oid and oid in placed:
            p = placed.pop(oid)
            fills.append({
                "order_id": oid,
                "side": str(p.get("side") or "").upper(),
                "placed_price": float(p.get("price") or 0),
                "filled_price": float(e.get("average_filled_price") or 0),
                "qty": float(e.get("filled_qty") or p.get("qty") or 0),
                "ts": float(e.get("ts") or 0),
                "maker": bool(p.get("post_only")),
            })
    return fills


def score_fill(side: str, placed_price: float, filled_price: float,
               mark_after: Optional[float] = None) -> dict:
    """Per-fill execution quality. Positive slippage/adverse = BAD (cost you)."""
    s = 1.0 if str(side).upper() == "BUY" else -1.0
    slippage = s * (filled_price - placed_price) if placed_price else 0.0
    adverse = None
    if mark_after is not None and filled_price:
        # after a BUY, price FALLING is adverse; after a SELL, price RISING is.
        adverse = -s * (mark_after - filled_price)
    return {"slippage": slippage, "adverse_selection": adverse}


def analyze(fills, mark_lookup: Optional[Callable] = None,
            contract_size: float = 1.0) -> dict:
    """Aggregate TCA over a list of fills (from fills_from_events). Optional
    mark_lookup(ts) -> mark price a few seconds later for adverse selection."""
    if not fills:
        return {"n_fills": 0, "note": "no fills in window"}
    slips, advs, dollar_cost = [], [], 0.0
    maker = 0
    for f in fills:
        mark_after = None
        if mark_lookup is not None:
            try:
                mark_after = mark_lookup(f["ts"])
            except Exception:
                mark_after = None
        sc = score_fill(f["side"], f["placed_price"], f["filled_price"], mark_after)
        slips.append(sc["slippage"])
        if sc["adverse_selection"] is not None:
            advs.append(sc["adverse_selection"])
        dollar_cost += sc["slippage"] * f.get("qty", 0) * contract_size
        if f.get("maker"):
            maker += 1
    out = {
        "n_fills": len(fills),
        "maker_ratio": round(maker / len(fills), 3),
        "slippage_mean": round(mean(slips), 6),
        "slippage_median": round(median(slips), 6),
        "slippage_worst": round(max(slips), 6),
        "implementation_shortfall_dollars": round(dollar_cost, 2),
        "adverse_selection_mean": round(mean(advs), 6) if advs else None,
        "adverse_selection_rate": round(sum(1 for a in advs if a > 0) / len(advs), 3) if advs else None,
    }
    flags = []
    if out["slippage_mean"] > 0:
        flags.append("paying up on average — orders crossing more than modeled")
    if out["adverse_selection_rate"] is not None and out["adverse_selection_rate"] > 0.6:
        flags.append("adversely selected on >60% of fills — the tape is running you over; widen entries or add an OFI gate")
    if out["maker_ratio"] < 0.5:
        flags.append("mostly taker fills — you're paying the spread; check post_only")
    out["flags"] = flags
    out["verdict"] = "execution is eating edge" if flags else "execution clean"
    return out


def run_tca(trade_log, contract_size: float = 1.0, tail: int = 2000,
            mark_lookup: Optional[Callable] = None) -> dict:
    """Convenience: pull recent events from the trade log and analyze."""
    try:
        events = list(trade_log.tail(tail)) if hasattr(trade_log, "tail") else list(trade_log)
    except Exception:
        events = []
    return analyze(fills_from_events(events), mark_lookup=mark_lookup, contract_size=contract_size)
