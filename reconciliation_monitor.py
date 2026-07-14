"""
reconciliation_monitor.py — READ-ONLY defense.

Diffs the EXCHANGE's real orders + positions against the bot's internal sleeve
state and flags the failure modes that actually cost money today:
  * duplicate working orders (the SLVR double-sell)
  * orphan orders (open on the exchange, tracked by no sleeve)
  * missing orders (sleeve armed, but its order isn't open on the exchange)
  * position mismatch (exchange qty != what the bot thinks it holds)
  * stale entries (armed buy waiting too long / price trended above last sale)

Pure diff logic — the caller supplies data via a Coinbase adapter + Redis sleeve
read. NEVER cancels or places anything. Emits findings; you notify on them.
"""
from dataclasses import dataclass
from collections import defaultdict


@dataclass
class Finding:
    severity: str    # "critical" | "warn" | "info"
    kind: str
    symbol: str
    detail: str


def _tick(px, tick):
    return round(px / tick) * tick if tick else px


def check_duplicate_orders(open_orders, price_tick=None):
    groups = defaultdict(list)
    for o in open_orders:
        groups[(o["symbol"], o["side"], _tick(o["price"], price_tick))].append(o)
    out = []
    for (sym, side, px), os in groups.items():
        if len(os) > 1:
            ids = ", ".join(str(o["order_id"])[:8] for o in os)
            out.append(Finding("critical", "duplicate_order", sym,
                       f"{len(os)} open {side} @ ~{px} ({ids}) — all could fill; you'd oversize"))
    return out


def check_orphans_and_missing(open_orders, sleeves):
    live_ids = {s.get("live_order_id") for s in sleeves if s.get("live_order_id")}
    open_ids = {o["order_id"] for o in open_orders}
    out = []
    for o in open_orders:
        if o["order_id"] not in live_ids:
            out.append(Finding("warn", "orphan_order", o["symbol"],
                       f"open {o['side']} {str(o['order_id'])[:8]} tracked by NO sleeve"))
    for s in sleeves:
        lid = s.get("live_order_id")
        if s.get("armed") and lid and lid not in open_ids:
            out.append(Finding("warn", "missing_order", s["symbol"],
                       f"sleeve armed ({s.get('state')}) but order {str(lid)[:8]} not open on exchange"))
    return out


def check_position_mismatch(exch_positions, sleeves, tol=0.0):
    expected = defaultdict(float)
    for s in sleeves:
        expected[s["symbol"]] += s.get("expected_position", 0)
    out = []
    for sym in set(exch_positions) | set(expected):
        a = exch_positions.get(sym, 0); e = expected.get(sym, 0)
        if abs(a - e) > tol:
            out.append(Finding("critical", "position_mismatch", sym,
                       f"exchange={a} vs bot-expected={e} (Δ{a - e:+g})"))
    return out


def check_stale_entries(sleeves, now_ts, stale_after_s=3600, price_lookup=None, drift_x_atr=2.0):
    out = []
    for s in sleeves:
        if not s.get("armed") or s.get("side") != "BUY":
            continue
        age = now_ts - s.get("armed_at", now_ts)
        drifted = False
        if price_lookup and s.get("last_sale_px") and s.get("atr"):
            px = price_lookup(s["symbol"])
            if px and px > s["last_sale_px"] + drift_x_atr * s["atr"]:
                drifted = True
        if age >= stale_after_s or drifted:
            why = f"waiting {int(age)}s" + (" and price trended above last sale" if drifted else "")
            out.append(Finding("warn", "stale_entry", s["symbol"],
                       f"armed buy {why} — re-eval candidate"))
    return out


def check_safety_halts(sleeves):
    """Count HALTED sleeves and surface as warnings — but EXCLUDE
    reentry_reeval expire halts, which are deliberate near-expiry exits
    (auditor 2026-07-14 Tier 2 (b)). A safety-halt count that included
    them would false-alarm every audit cycle whenever expert-reentry
    is enabled on a dated futures near its expiry."""
    try:
        from reentry_reeval import is_expire_halt
    except Exception:
        def is_expire_halt(_r):  # pragma: no cover — defensive fallback
            return False
    out = []
    for s in sleeves:
        if str(s.get("state") or "") != "HALTED":
            continue
        reason = s.get("halt_reason") or ""
        if is_expire_halt(reason):
            continue  # deliberate expire — not a safety concern
        out.append(Finding("warn", "safety_halt", s.get("symbol") or "?",
                   f"sleeve HALTED (reason: {reason or 'unspecified'}) — review + resume"))
    return out


def check_state_config_drift(state_config_pairs):
    """Auditor 2026-07-14 SLR-incident agenda item — flag when runtime
    state.swing_qty disagrees with persisted config.swing_qty.

    The SLR ghost class: config said swing_qty=0 (primary disabled) but
    state had swing_qty=2 from a prior config value that never cleared.
    Bot re-armed sell 2 @ $65.25 every tick after user cancellation.

    Args:
        state_config_pairs: list of dicts with keys:
            symbol, state_swing_qty, config_swing_qty
    Returns:
        list of Finding — critical when state disagrees with config.
    """
    out = []
    for p in state_config_pairs or []:
        try:
            s = int(p.get("state_swing_qty") or 0)
            c = int(p.get("config_swing_qty") or 0)
        except (TypeError, ValueError):
            continue
        if s != c:
            out.append(Finding(
                "critical", "state_config_drift", p.get("symbol", "?"),
                f"state.swing_qty={s} but config.swing_qty={c} — bot's "
                f"in-memory qty is stale and will re-arm until state is "
                f"corrected (SUSPEND service + fix Redis + RESUME)."
            ))
    return out


def reconcile(*, open_orders, exch_positions, sleeves, now_ts,
              price_tick=None, stale_after_s=3600, price_lookup=None,
              state_config_pairs=None):
    """Run all checks; return findings, critical first.

    New parameter (2026-07-14):
        state_config_pairs — optional list of {symbol, state_swing_qty,
            config_swing_qty} dicts. When provided, runs the
            check_state_config_drift check (SLR-incident class).
    """
    findings = []
    findings += check_duplicate_orders(open_orders, price_tick)
    findings += check_orphans_and_missing(open_orders, sleeves)
    findings += check_position_mismatch(exch_positions, sleeves)
    findings += check_stale_entries(sleeves, now_ts, stale_after_s, price_lookup)
    findings += check_safety_halts(sleeves)
    findings += check_state_config_drift(state_config_pairs)
    rank = {"critical": 0, "warn": 1, "info": 2}
    findings.sort(key=lambda f: rank.get(f.severity, 3))
    return findings


def format_alert(findings):
    """One notify-ready string, or '' if clean (send nothing when clean)."""
    if not findings:
        return ""
    crit = [f for f in findings if f.severity == "critical"]
    head = f"{len(crit)} critical / {len(findings)} total reconciliation issue(s)"
    lines = [head] + [f"[{f.severity}] {f.kind} {f.symbol}: {f.detail}" for f in findings]
    return "\n".join(lines)
