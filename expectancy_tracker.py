"""
expectancy_tracker.py — MEASURE what to optimize.

You can't improve what you don't track, and P&L alone tells you nothing actionable.
This computes the numbers that actually drive long-run outcome (Van Tharp framing):
  * Expectancy = average R-multiple per trade (P&L in units of initial risk)
  * Win rate, avg win R, avg loss R, profit factor
  * Max drawdown on the equity curve (the "don't blow up" number)
  * Per sleeve/symbol AND overall, so you can see WHICH edge works.

Feed it closed trades, or pair a raw fill stream into trades with pair_fills_to_trades().
Read-only: it computes, it never trades.
"""
from dataclasses import dataclass
from collections import defaultdict, deque


@dataclass
class Trade:
    group: str                 # e.g. "adam-live|NOL-20JUL26-CDE|smri9vd4f"
    entry_ts: float
    exit_ts: float
    entry_px: float
    exit_px: float
    qty: float
    side: str = "long"         # "long" | "short"
    risk_per_unit: float = None  # entry - stop, per unit; enables R-multiples. None -> R skipped
    fees: float = 0.0

    @property
    def pnl(self):
        d = (self.exit_px - self.entry_px) if self.side == "long" else (self.entry_px - self.exit_px)
        return d * self.qty - self.fees

    @property
    def r_multiple(self):
        if not self.risk_per_unit or self.risk_per_unit <= 0:
            return None
        return self.pnl / (self.risk_per_unit * self.qty)

    @property
    def hold_s(self):
        return max(0.0, self.exit_ts - self.entry_ts)


def pair_fills_to_trades(fills, long_side="BUY"):
    """FIFO-match a fill stream into closed round-trips per group.
    fills: list of dicts {group, ts, side, price, qty, fees?, risk_per_unit?}.
    Opens on long_side, closes on the opposite. Handles partial fills."""
    books = defaultdict(deque)        # group -> deque of open lots
    trades = []
    for f in sorted(fills, key=lambda x: x["ts"]):
        g = f["group"]; qty = f["qty"]
        if f["side"] == long_side:
            books[g].append(dict(f))  # open a lot
            continue
        # closing fill: consume open lots FIFO
        remaining = qty
        while remaining > 1e-12 and books[g]:
            lot = books[g][0]
            take = min(remaining, lot["qty"])
            trades.append(Trade(
                group=g, entry_ts=lot["ts"], exit_ts=f["ts"],
                entry_px=lot["price"], exit_px=f["price"], qty=take, side="long",
                risk_per_unit=lot.get("risk_per_unit"),
                fees=lot.get("fees", 0.0) * (take / lot["qty"]) + f.get("fees", 0.0) * (take / qty)))
            lot["qty"] -= take
            remaining -= take
            if lot["qty"] <= 1e-12:
                books[g].popleft()
    return trades


def _max_drawdown(trades):
    """Peak-to-trough on cumulative P&L, trades ordered by exit time."""
    cum = peak = 0.0
    mdd = 0.0
    for t in sorted(trades, key=lambda x: x.exit_ts):
        cum += t.pnl
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return mdd


def compute_metrics(trades):
    if not trades:
        return {"trades": 0}
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    rs = [t.r_multiple for t in trades if t.r_multiple is not None]
    win_r = [r for r in rs if r > 0]
    loss_r = [r for r in rs if r <= 0]
    n = len(trades)
    m = {
        "trades": n,
        "win_rate": len(wins) / n,
        "total_pnl": round(sum(pnls), 2),
        "avg_pnl": round(sum(pnls) / n, 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "max_drawdown": round(_max_drawdown(trades), 2),
        "avg_hold_min": round(sum(t.hold_s for t in trades) / n / 60.0, 1),
    }
    if rs:                     # R-multiple metrics (need risk_per_unit on trades)
        m["expectancy_R"] = round(sum(rs) / len(rs), 3)         # THE number to optimize
        m["avg_win_R"] = round(sum(win_r) / len(win_r), 3) if win_r else 0.0
        m["avg_loss_R"] = round(sum(loss_r) / len(loss_r), 3) if loss_r else 0.0
        m["r_coverage"] = f"{len(rs)}/{n} trades have risk data"
    else:
        m["expectancy_R"] = None
        m["note"] = "No risk_per_unit on trades -> R-expectancy unavailable. Attach entry-minus-stop to enable."
    return m


def report(trades):
    """Per-group + overall, printable. Best expectancy first."""
    by_group = defaultdict(list)
    for t in trades:
        by_group[t.group].append(t)
    lines = ["=== Expectancy / drawdown ==="]
    rows = [(g, compute_metrics(ts)) for g, ts in by_group.items()]
    rows.sort(key=lambda r: (r[1].get("expectancy_R") is None, -(r[1].get("expectancy_R") or -9e9)))
    for g, m in rows:
        exp = m.get("expectancy_R")
        exp_s = f"exp={exp:+.2f}R" if exp is not None else "exp=n/a"
        lines.append(f"{g:<40} n={m['trades']:>3}  win={m['win_rate']*100:4.0f}%  "
                     f"{exp_s}  PF={m['profit_factor']}  maxDD=${m['max_drawdown']}  pnl=${m['total_pnl']}")
    lines.append("-" * 40)
    o = compute_metrics(trades)
    oexp = o.get("expectancy_R")
    lines.append(f"{'OVERALL':<40} n={o['trades']:>3}  win={o['win_rate']*100:4.0f}%  "
                 f"exp={('%+.2fR' % oexp) if oexp is not None else 'n/a'}  "
                 f"PF={o['profit_factor']}  maxDD=${o['max_drawdown']}  pnl=${o['total_pnl']}")
    return "\n".join(lines)
