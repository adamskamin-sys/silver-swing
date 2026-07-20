"""Margin / liquidation-distance sentinel (crew).

portfolio_risk watches P&L drawdown; a prop desk also watches how close a
CORRELATED move gets you to a forced liquidation. On leveraged futures the
liquidation price is the number that actually ends accounts — the exchange makes
your decision for you. This computes, across all sleeves at once:
  - margin utilization (used vs available)
  - the adverse price move (per correlated cluster) that triggers liquidation
  - the sleeve/cluster with the least headroom

Read-only. All inputs are numbers you already track (position qty, avg entry,
balance, contract/margin specs, correlation family). No new API calls.
"""

from __future__ import annotations

from typing import Optional

try:
    from correlation import _family_of as _fam  # reuse the same family map
except Exception:  # pragma: no cover - fallback if imported standalone
    def _fam(symbol):
        return (symbol or "").split("-")[0].upper() or None


def liquidation_move_pct(side: str, leverage: float, maint_margin_frac: float = 0.005) -> float:
    """Approx adverse % move to liquidation for an isolated leveraged position:
    ~ (1/leverage) - maintenance_margin_fraction. Long and short symmetric here.
    Returns a positive fraction (e.g. 0.08 = an 8% adverse move liquidates)."""
    if leverage <= 0:
        return 1.0
    return max(0.0, (1.0 / leverage) - maint_margin_frac)


def position_headroom(pos: dict, maint_margin_frac: float = 0.005) -> Optional[dict]:
    """pos = {symbol, side, qty, avg_entry, mark, contract_size, margin_per_contract[, liquidation_price]}.
    Returns headroom to liquidation for this position (None if underspecified).

    When margin_per_contract is zero (auto-seeded config), falls back to the
    Coinbase-provided liquidation_price field if present. When both are available,
    Coinbase's value is preferred as it accounts for actual FCM margin tiers."""
    try:
        qty = float(pos["qty"]); entry = float(pos["avg_entry"]); mark = float(pos.get("mark") or entry)
        cs = float(pos["contract_size"]); mpc = float(pos.get("margin_per_contract") or 0)
    except (KeyError, TypeError, ValueError):
        return None
    if qty <= 0 or entry <= 0 or cs <= 0:
        return None
    side = str(pos.get("side", "BUY")).upper()
    liq_given = float(pos.get("liquidation_price") or 0)
    if mpc <= 0:
        # No margin_per_contract in config (auto-seeded product). Fall back to
        # the Coinbase-reported liquidation_price; return None if unavailable.
        if liq_given <= 0:
            return None
        liq_price = liq_given
        leverage = 0.0
        move = 0.0
        margin = 0.0
    else:
        notional = qty * cs * entry
        margin = qty * mpc
        leverage = notional / margin if margin > 0 else 0.0
        move = liquidation_move_pct(side, leverage, maint_margin_frac)
        liq_price = entry * (1 - move) if side == "BUY" else entry * (1 + move)
        if liq_given > 0:
            liq_price = liq_given
    dist_pct = (mark - liq_price) / mark if side == "BUY" else (liq_price - mark) / mark
    return {
        "symbol": pos.get("symbol"),
        "family": _fam(pos.get("symbol")),
        "leverage": round(leverage, 2),
        "liq_move_pct": round(move * 100, 2),
        "liq_price": round(liq_price, 6),
        "distance_to_liq_pct": round(dist_pct * 100, 2),
        "margin_used": round(margin, 2),
        "notional": round(qty * cs * entry, 2),
    }


def margin_report(positions, balance: float, maint_margin_frac: float = 0.005,
                  warn_distance_pct: float = 15.0, warn_utilization: float = 0.6) -> dict:
    """positions: list of position dicts. Aggregates margin utilization and the
    correlated-cluster liquidation risk."""
    rows = []
    blind_spots: list[str] = []
    for p in positions:
        r = position_headroom(p, maint_margin_frac)
        if r:
            rows.append(r)
        elif (float(p.get("margin_per_contract") or 0) <= 0
              and float(p.get("liquidation_price") or 0) <= 0
              and float(p.get("qty") or 0) > 0):
            # Auto-seeded config with no margin_per_contract AND exchange
            # didn't supply liquidation_price — position is invisible to margin
            # math. Emit a high alert so it's not silently excluded.
            blind_spots.append(str(p.get("symbol", "?")))

    used = sum(r["margin_used"] for r in rows)
    utilization = used / balance if balance > 0 else 0.0

    # Correlated clusters share a shock: the WHOLE family's positions move together,
    # so the cluster's risk is set by its NEAREST-to-liquidation member.
    clusters: dict[str, dict] = {}
    for r in rows:
        fam = r["family"] or r["symbol"]
        c = clusters.setdefault(fam, {"family": fam, "symbols": [], "margin_used": 0.0,
                                      "nearest_liq_pct": None})
        c["symbols"].append(r["symbol"])
        c["margin_used"] += r["margin_used"]
        d = r["distance_to_liq_pct"]
        if c["nearest_liq_pct"] is None or d < c["nearest_liq_pct"]:
            c["nearest_liq_pct"] = d

    nearest = min((r["distance_to_liq_pct"] for r in rows), default=None)
    alerts = []
    for sym in blind_spots:
        alerts.append({"severity": "high",
                       "detail": f"{sym}: excluded from margin check — no margin_per_contract and no liquidation_price from exchange; verify manually"})
    if utilization >= warn_utilization:
        alerts.append({"severity": "high", "detail": f"margin utilization {utilization*100:.0f}% (>= {warn_utilization*100:.0f}%)"})
    for fam, c in clusters.items():
        if c["nearest_liq_pct"] is not None and c["nearest_liq_pct"] <= warn_distance_pct:
            alerts.append({"severity": "critical",
                           "detail": f"cluster {fam} is {c['nearest_liq_pct']:.1f}% from liquidation on a correlated move ({', '.join(c['symbols'])})"})
    return {
        "positions": rows,
        "blind_spots": blind_spots,
        "margin_used": round(used, 2),
        "balance": round(balance, 2),
        "utilization_pct": round(utilization * 100, 1),
        "nearest_distance_to_liq_pct": nearest,
        "clusters": list(clusters.values()),
        "alerts": alerts,
        "verdict": "MARGIN RISK" if alerts else "healthy headroom",
    }
