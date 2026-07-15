"""Watch what expert_spread (Avellaneda-Stoikov) is doing on live.

Adam 2026-07-15: after flipping __expert_spread_mode__ to shadow or
expert, run this to see:

  [1] All expert_spread_shadow_decision events (mode=shadow) — what AS
      would have picked vs what legacy actually picked
  [2] All expert_spread_expert_decision + _applied pairs (mode=expert)
      — what AS picked and what it replaced
  [3] Per-sleeve summary: spread narrowing/widening trend, average
      expected $/day comparison

Read-only. Usage:
    python3 diag_expert_spread_review.py                        # last 24h
    python3 diag_expert_spread_review.py 720                    # last 12h
    python3 diag_expert_spread_review.py 60 HYP-20DEC30-CDE     # 1h, one product
"""
from __future__ import annotations
import os
import sys
import time
from collections import defaultdict


def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return "?"


def _pct(new, old) -> str:
    try:
        if not old:
            return "—"
        return f"{100.0 * (float(new) - float(old)) / float(old):+.2f}%"
    except Exception:
        return "?"


def main() -> None:
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 1440.0
    product_filter = sys.argv[2] if len(sys.argv) > 2 else None
    tenant = "adam-live"

    print("=" * 128)
    print(f"EXPERT SPREAD REVIEW — last {minutes:.0f}min"
          + (f"  product={product_filter}" if product_filter else "")
          + f"  tenant={tenant}")
    print("=" * 128)

    # Current mode
    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))
    mode = (store.get_state(tenant, "__expert_spread_mode__") or {}).get("mode") or "off"
    print(f"\nCurrent __expert_spread_mode__: {mode}")

    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    except Exception as e:
        print(f"\n✗ trade log load failed: {e}")
        return

    cutoff = time.time() - minutes * 60
    decisions = []  # both shadow + expert decisions
    applied = []
    errors = []
    apply_skipped = []
    for e in log.events():
        if not isinstance(e, dict):
            continue
        ts = float(e.get("ts") or 0)
        if ts < cutoff:
            continue
        sym = str(e.get("symbol") or "")
        if product_filter and sym != product_filter:
            continue
        et = str(e.get("event_type") or "")
        if et in ("expert_spread_shadow_decision",
                  "expert_spread_expert_decision"):
            decisions.append(e)
        elif et == "expert_spread_applied":
            applied.append(e)
        elif et == "expert_spread_error":
            errors.append(e)
        elif et == "expert_spread_apply_skipped_invalid":
            apply_skipped.append(e)

    print(f"\n[1] EVENT COUNTS (last {minutes:.0f}min):")
    print(f"    decisions computed:              {len(decisions)}")
    print(f"    AS applied (overrode legacy):    {len(applied)}")
    print(f"    apply skipped (bad AS output):   {len(apply_skipped)}")
    print(f"    errors (AS threw):               {len(errors)}")

    if not decisions:
        print(f"\n    No AS decisions in the window. Either:")
        print(f"    * mode='off' (currently: {mode})")
        print(f"    * no post-sell reanchors fired in this window")
        print(f"    * bot hasn't ticked yet post-mode-flip")
        return

    print(f"\n[2] PER-SLEEVE DECISION SUMMARY:")
    per_sleeve = defaultdict(lambda: {"count": 0, "as_daily_sum": 0.0,
                                       "leg_spread_sum": 0.0,
                                       "as_spread_sum": 0.0,
                                       "narrower": 0, "wider": 0,
                                       "floor_binding": 0,
                                       "symbol": "", "sid": ""})
    for e in decisions:
        sid = str(e.get("sleeve_id") or "")
        sym = str(e.get("symbol") or "")
        s = per_sleeve[sid]
        s["symbol"] = sym
        s["sid"] = sid
        s["count"] += 1
        s["as_daily_sum"] += float(e.get("as_expected_daily_pnl") or 0)
        leg_spread = float(e.get("legacy_spread") or 0)
        as_spread = float(e.get("as_spread") or 0)
        s["leg_spread_sum"] += leg_spread
        s["as_spread_sum"] += as_spread
        if as_spread < leg_spread:
            s["narrower"] += 1
        elif as_spread > leg_spread:
            s["wider"] += 1
        if e.get("as_cost_floor_binding"):
            s["floor_binding"] += 1

    print(f"\n    {'SYMBOL':22s} {'SLEEVE':14s} {'#':>4s} {'AVG LEG SPRD':>13s} "
          f"{'AVG AS SPRD':>13s} {'DIR':>7s} {'FLOOR':>6s} {'AVG E[$/day]':>13s}")
    print(f"    {'-' * 100}")
    for sid, s in sorted(per_sleeve.items(),
                         key=lambda kv: -kv[1]["count"]):
        n = s["count"]
        avg_leg = s["leg_spread_sum"] / n
        avg_as = s["as_spread_sum"] / n
        direction = ("narrower" if s["narrower"] > s["wider"]
                     else "wider" if s["wider"] > s["narrower"]
                     else "even")
        avg_daily = s["as_daily_sum"] / n
        print(f"    {s['symbol']:22s} {s['sid']:14s} {n:>4d} "
              f"${avg_leg:>12.4f} ${avg_as:>12.4f} {direction:>7s} "
              f"{s['floor_binding']:>6d} ${avg_daily:>12.2f}")

    print(f"\n[3] MOST RECENT APPLIED (up to 10) — where AS actually overrode legacy:")
    if not applied:
        print(f"    · none in window (mode={mode}; only 'expert' triggers _applied events)")
    else:
        for e in applied[-10:]:
            print(f"    · {_fmt_ts(e.get('ts'))}  {e.get('symbol'):22s} "
                  f"sleeve={e.get('sleeve_id'):12s}")
            print(f"      legacy: buy ${e.get('replaced_legacy_buy_px'):.4f} → "
                  f"sell ${e.get('replaced_legacy_sell_px'):.4f} "
                  f"(spread ${float(e.get('replaced_legacy_sell_px') or 0) - float(e.get('replaced_legacy_buy_px') or 0):.4f})")
            print(f"      AS:     buy ${e.get('as_buy_px'):.4f} → "
                  f"sell ${e.get('as_sell_px'):.4f} "
                  f"(spread ${e.get('as_spread'):.4f}, "
                  f"E[$/day] ${e.get('as_expected_daily_pnl'):.2f})")

    if errors:
        print(f"\n[4] RECENT ERRORS (up to 5):")
        for e in errors[-5:]:
            print(f"    ✗ {_fmt_ts(e.get('ts'))}  {e.get('symbol')} "
                  f"sleeve={e.get('sleeve_id')}: {e.get('error')}")

    if apply_skipped:
        print(f"\n[5] APPLY SKIPS (AS returned nonsense, fell back to legacy):")
        for e in apply_skipped[-5:]:
            print(f"    ⚠ {_fmt_ts(e.get('ts'))}  {e.get('symbol')} "
                  f"sleeve={e.get('sleeve_id')}: {e.get('reason')}")

    print("=" * 128)


if __name__ == "__main__":
    main()
