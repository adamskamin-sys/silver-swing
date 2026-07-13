"""Live risk sentinel (crew).

The expert_guard answers one question (is the config using the expert data?).
The sentinel is broader: it watches the running system for the PATTERNS that
precede a blown account — not single events, but clusters and trends across the
trade log. It's meant to be run periodically (in the loop or by the daily audit
agent) over the recent TradeLog events plus current snapshots.

Read-only. It raises ALERTS with a severity; it never trades, halts, or edits.

Detects:
  - Halt CLUSTERS (many halts in a short window = something systemically wrong).
  - Reconcile MISMATCHES (believed vs actual position diverged).
  - Kill-switch / portfolio-halt activations.
  - Fee-gate failure clusters (preview API degraded → arms being skipped).
  - Partial-fill halts (the #3 guard firing = a real position needs reconciling).
  - Drawdown ACCELERATION (loss rate increasing).
  - Stale feed / snapshot (no fresh marks = flying blind).
Every threshold is tunable; defaults are conservative.
"""

from __future__ import annotations

from typing import Iterable, Optional


DEFAULTS = {
    "window_secs": 3600.0,          # look-back for cluster detection
    "halt_cluster_n": 3,            # >= N halts in window -> alert
    "fee_fail_cluster_n": 5,        # >= N fee-gate preview failures -> alert
    "stale_snapshot_secs": 120.0,   # snapshot older than this -> flying blind
    "drawdown_accel_ratio": 2.0,    # recent loss-rate vs prior -> alert
}


def _ev_ts(e: dict) -> float:
    try:
        return float(e.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def _recent(events, now: float, window: float):
    return [e for e in events if _ev_ts(e) >= now - window]


def scan_events(events: Iterable[dict], now: float, cfg: Optional[dict] = None) -> list[dict]:
    """Scan recent TradeLog events for risk patterns. `events` are dicts shaped
    like safety.TradeLog rows: {"ts", "event_type", ...}. Returns a list of
    alert dicts: {"severity", "kind", "detail", "count"?}."""
    c = {**DEFAULTS, **(cfg or {})}
    evs = list(events)
    win = _recent(evs, now, c["window_secs"])
    alerts: list[dict] = []

    def _count(pred):
        return sum(1 for e in win if pred(e))

    et = lambda e: str(e.get("event_type") or "")

    # 1. Halt cluster
    n_halt = _count(lambda e: et(e) in ("halt", "reconcile_halt") or et(e).endswith("_halt"))
    if n_halt >= c["halt_cluster_n"]:
        alerts.append({"severity": "critical", "kind": "halt_cluster", "count": n_halt,
                       "detail": f"{n_halt} halts in the last {int(c['window_secs']/60)}m — systemic problem, investigate before it keeps re-halting."})

    # 2. Kill switch / portfolio halt activations
    n_kill = _count(lambda e: "kill_switch" in et(e) and "pause" not in et(e))
    if n_kill:
        alerts.append({"severity": "high", "kind": "kill_switch", "count": n_kill,
                       "detail": f"kill switch activity x{n_kill} in window."})
    n_pf = _count(lambda e: et(e) == "portfolio_risk_halted")
    if n_pf:
        alerts.append({"severity": "high", "kind": "portfolio_halt", "count": n_pf,
                       "detail": f"portfolio circuit breaker tripped x{n_pf} — correlated drawdown."})

    # 3. Reconcile mismatch (position drift caught)
    n_recon = _count(lambda e: et(e) == "reconcile_halt" or (et(e) == "reconciled" and e.get("mismatches")))
    if n_recon:
        alerts.append({"severity": "critical", "kind": "reconcile_mismatch", "count": n_recon,
                       "detail": "believed vs actual position diverged — real money may not match the bot's belief."})

    # 4. Fee-gate failure cluster (preview degraded -> arms skipped)
    n_fee = _count(lambda e: et(e) == "fee_gate_preview_failed")
    if n_fee >= c["fee_fail_cluster_n"]:
        alerts.append({"severity": "medium", "kind": "fee_gate_degraded", "count": n_fee,
                       "detail": f"{n_fee} fee-preview failures — the cost guard is skipping arms; check the broker preview endpoint."})

    # 5. Partial-fill halt (the #3 guard fired -> reconcile needed)
    n_partial = _count(lambda e: et(e) == "halt" and "partial fill" in str(e.get("reason") or "").lower())
    if n_partial:
        alerts.append({"severity": "high", "kind": "partial_fill_halt", "count": n_partial,
                       "detail": "partial-fill guard fired — a partially-filled order left real contracts to reconcile."})

    # 6. Drawdown acceleration: compare realized-loss rate in the recent half of
    #    the window vs the prior half. Uses cycle_completed gross fields.
    grosses = [(float(e.get("gross") or 0), _ev_ts(e)) for e in win if et(e) == "cycle_completed"]
    if len(grosses) >= 6:
        mid = now - c["window_secs"] / 2
        recent_loss = -sum(g for g, t in grosses if t >= mid and g < 0)
        prior_loss = -sum(g for g, t in grosses if t < mid and g < 0)
        if prior_loss > 0 and recent_loss >= prior_loss * c["drawdown_accel_ratio"]:
            alerts.append({"severity": "high", "kind": "drawdown_acceleration",
                           "detail": f"losses accelerating: ${recent_loss:.2f} in the recent half vs ${prior_loss:.2f} prior."})

    return alerts


def scan_snapshots(snapshots: dict, now: float, cfg: Optional[dict] = None) -> list[dict]:
    """Detect stale/flying-blind feeds. `snapshots` = {symbol: snapshot_dict}
    where a snapshot has 'generated_at'. Alerts on any that are too old."""
    c = {**DEFAULTS, **(cfg or {})}
    alerts = []
    for sym, snap in (snapshots or {}).items():
        if str(sym).startswith("__") or not isinstance(snap, dict):
            continue
        gen = snap.get("generated_at") or snap.get("ts")
        try:
            age = now - float(gen)
        except (TypeError, ValueError):
            continue
        if age > c["stale_snapshot_secs"]:
            alerts.append({"severity": "high", "kind": "stale_snapshot", "symbol": sym,
                           "detail": f"{sym} snapshot is {int(age)}s old (> {int(c['stale_snapshot_secs'])}s) — feed may be down; the bot is flying blind."})
    return alerts


def run_sentinel(store, tenant: str, trade_log, now: float,
                 notifier=None, cfg: Optional[dict] = None) -> list[dict]:
    """Convenience: pull recent events from the trade log + snapshots from the
    store, scan, and alert on anything at 'high' or 'critical'. Returns alerts.
    Wire into the loop or call from the daily audit agent."""
    c = {**DEFAULTS, **(cfg or {})}
    try:
        events = list(trade_log.tail(2000)) if hasattr(trade_log, "tail") else list(trade_log)
    except Exception:
        events = []
    snaps = {}
    try:
        for sym in store.list_symbols(tenant):
            if str(sym).startswith("__"):
                continue
            s = store.get_snapshot(tenant, sym) if hasattr(store, "get_snapshot") else None
            if s:
                snaps[sym] = s
    except Exception:
        pass

    alerts = scan_events(events, now, c) + scan_snapshots(snaps, now, c)
    if notifier is not None:
        for a in alerts:
            if a.get("severity") in ("high", "critical"):
                try:
                    from alerting import Priority
                    prio = Priority.CRIT if a["severity"] == "critical" else Priority.HIGH
                    notifier.send(f"risk sentinel: {a['kind']}", a["detail"], prio)
                except Exception:
                    pass
    return alerts
