"""P&L attribution (crew).

"Which sleeve / signal / gate is actually making money?" is the question that
lets you PRUNE — cut the machinery that adds surface area without contributing
P&L. This attributes realized P&L across sources from the trade log, and tallies
how often each gate blocked an arm (opportunity-cost signal). Read-only.

Attribution is by SOURCE = sleeve_name / sleeve_id, or "primary" for the base
strategy. Any event carrying a numeric `gross` (cycle_completed and the sleeve
cycle events) is counted, so it works regardless of the exact sleeve event name.
"""

from __future__ import annotations

from statistics import mean
from typing import Optional


def _source_of(e: dict) -> str:
    return str(e.get("sleeve_name") or e.get("sleeve_id") or "primary")


def attribute_pnl(events) -> dict:
    """Group realized P&L by source. Returns {source: stats} plus a ranking."""
    by: dict[str, dict] = {}
    for e in events:
        g = e.get("gross")
        if not isinstance(g, (int, float)):
            continue
        src = _source_of(e)
        b = by.setdefault(src, {"realized": 0.0, "trades": 0, "wins": 0,
                                "losses": 0, "win_sum": 0.0, "loss_sum": 0.0})
        b["realized"] += float(g)
        b["trades"] += 1
        if g >= 0:
            b["wins"] += 1
            b["win_sum"] += float(g)
        else:
            b["losses"] += 1
            b["loss_sum"] += float(g)

    for src, b in by.items():
        t = b["trades"] or 1
        b["realized"] = round(b["realized"], 2)
        b["win_rate"] = round(b["wins"] / t, 3)
        b["avg_win"] = round(b["win_sum"] / b["wins"], 2) if b["wins"] else 0.0
        b["avg_loss"] = round(b["loss_sum"] / b["losses"], 2) if b["losses"] else 0.0
        b["expectancy"] = round(b["realized"] / t, 3)
        del b["win_sum"], b["loss_sum"]

    ranked = sorted(by.items(), key=lambda kv: kv[1]["realized"], reverse=True)
    losers = [s for s, b in by.items() if b["realized"] < 0]
    total = round(sum(b["realized"] for b in by.values()), 2)
    return {
        "by_source": by,
        "ranked": [s for s, _ in ranked],
        "total_realized": total,
        "net_losers": losers,   # candidates to PRUNE
        "top_contributor": ranked[0][0] if ranked else None,
        "advice": (f"{', '.join(losers)} are net-negative — measure whether they earn "
                   f"their complexity, and cut if not." if losers
                   else "every source is net-positive."),
    }


_GATE_EVENTS = (
    "sleeve_arm_skipped_portfolio_halt", "sleeve_arm_skipped_news_blackout",
    "sleeve_arm_skipped_book_imbalance", "sleeve_arm_skipped_trade_ofi",
    "sleeve_arm_skipped_peer_crash", "fee_gate_halt", "fee_gate_preview_failed",
)


def gate_activity(events) -> dict:
    """How often each gate blocked an arm. High counts = a gate doing a lot of
    work (verify it's HELPING, not just suppressing good trades)."""
    counts: dict[str, int] = {}
    for e in events:
        et = str(e.get("event_type") or "")
        if et in _GATE_EVENTS or et.startswith("sleeve_arm_skipped_"):
            counts[et] = counts.get(et, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return {"by_gate": dict(ranked), "busiest_gate": ranked[0][0] if ranked else None,
            "total_blocks": sum(counts.values())}


def report(trade_log, tail: int = 5000) -> dict:
    """Convenience: attribution + gate activity from the trade log."""
    try:
        events = list(trade_log.tail(tail)) if hasattr(trade_log, "tail") else list(trade_log)
    except Exception:
        events = []
    return {"pnl": attribute_pnl(events), "gates": gate_activity(events)}
