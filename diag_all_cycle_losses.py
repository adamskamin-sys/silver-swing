"""Class-wide audit — per-product cycle P&L, cancel activity, immediate-stop-fire detection.

Adam 2026-07-20: OIL screenshot showed -$16.94 net across 3 cycles (buy $82.30
→ sell $80.91 = -$14.95; buy $81.08 → sell $80.88 = -$3.05; buy $80.77 →
sell $80.98 = +$1.06). Adam: "apply fix to everything, not one contract."

Before shipping any fix, prove the CLASS scope:
  1. Enumerate every product with recent SELL activity
  2. Pair BUYs with SELLs into cycles, compute net after fees
  3. Detect immediate-stop-fire pattern (SELL within N seconds of BUY)
  4. Count cancel events per product
  5. Show whether fee-floor clamp fired
  6. Classify each SELL by triggering event_type from trade log

Read-only. Prints a table + narrative per product with rule violations.

Usage:  python3 diag_all_cycle_losses.py
"""
from __future__ import annotations
import os
import time
from collections import defaultdict, Counter
from datetime import datetime, timezone


IMMEDIATE_STOP_THRESHOLD_SECS = 300  # buy→sell within 5 min = suspect
LOOKBACK_EVENTS = 20000
LOOKBACK_FILLS_HOURS = 24


def _fmt_ts(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%H:%M:%S")
    except Exception:
        return "?"


def main() -> None:
    print("=" * 88)
    print("CLASS-WIDE CYCLE-LOSS AUDIT (all products, last 24h)")
    print("=" * 88)

    from safety import make_trade_log
    log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    all_events = log.tail(LOOKBACK_EVENTS)
    cutoff = time.time() - (LOOKBACK_FILLS_HOURS * 3600)
    events = [e for e in all_events if float(e.get("ts") or 0) >= cutoff]
    print(f"\n  loaded {len(all_events)} events, {len(events)} within {LOOKBACK_FILLS_HOURS}h")

    # -- Coinbase truth for fills --------------------------------------------
    from broker import BrokerConfig, CoinbaseBroker
    b = CoinbaseBroker(BrokerConfig(product_id="BTC-USD"))  # any pid, we use client only

    # Pull recent fills across all products via list_orders (FILLED status)
    print("\n  fetching recent Coinbase fills...")
    try:
        resp = b.client.list_orders(order_status="FILLED", limit=250)
        orders = getattr(resp, "orders", None) or (resp.get("orders") if isinstance(resp, dict) else []) or []
    except Exception as e:
        print(f"  ✗ list_orders failed: {e}")
        return

    def _get(o, k):
        if hasattr(o, k):
            return getattr(o, k)
        if isinstance(o, dict):
            return o.get(k)
        return None

    # Group fills by product
    fills_by_pid = defaultdict(list)
    for o in orders:
        pid = _get(o, "product_id") or ""
        side = str(_get(o, "side") or "").upper()
        filled_at = _get(o, "last_fill_time") or _get(o, "created_time")
        try:
            ts = datetime.fromisoformat(str(filled_at).replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if ts < cutoff:
            continue
        avg = _get(o, "average_filled_price")
        try:
            px = float(avg) if avg else 0.0
        except Exception:
            px = 0.0
        qty = _get(o, "filled_size")
        try:
            q = float(qty) if qty else 0.0
        except Exception:
            q = 0.0
        # Coinbase reports fee under total_fees typically
        fee_raw = _get(o, "total_fees") or 0
        try:
            fee = float(fee_raw)
        except Exception:
            fee = 0.0
        order_type = _get(o, "order_type") or "?"
        oid = _get(o, "order_id") or ""
        fills_by_pid[pid].append({
            "ts": ts, "side": side, "px": px, "qty": q,
            "fee": fee, "type": order_type, "oid": oid,
        })

    # -- Per-product analysis ------------------------------------------------
    total_net = 0.0
    total_loss_cycles = 0
    total_immediate_fires = 0

    for pid in sorted(fills_by_pid.keys()):
        fills = sorted(fills_by_pid[pid], key=lambda x: x["ts"])
        if len(fills) < 2:
            continue

        # contract size (for perp/futures) — use 1.0 for spot
        cs = 1.0
        try:
            spec = b.client.get_product(pid)
            cs_raw = _get(spec, "contract_size") or 1.0
            cs = float(cs_raw) if cs_raw else 1.0
        except Exception:
            pass

        buys = [f for f in fills if f["side"] == "BUY"]
        sells = [f for f in fills if f["side"] == "SELL"]

        # Pair each SELL with its most recent prior BUY
        cycles = []
        buy_queue = list(buys)
        for s in sells:
            prior = [b for b in buy_queue if b["ts"] < s["ts"]]
            if not prior:
                continue
            match = prior[-1]  # most recent BUY before this SELL
            gap_secs = int(s["ts"] - match["ts"])
            gross_per_unit = s["px"] - match["px"]
            qty = min(s["qty"], match["qty"])
            gross_dollars = gross_per_unit * qty * cs
            fees = s["fee"] + match["fee"]
            net = gross_dollars - fees
            cycles.append({
                "buy_ts": match["ts"], "buy_px": match["px"],
                "sell_ts": s["ts"], "sell_px": s["px"],
                "gap_secs": gap_secs, "gross": gross_dollars,
                "fees": fees, "net": net,
            })

        if not cycles:
            continue

        # Product-level cancel activity
        pid_events = [e for e in events if (e.get("symbol") or "") == pid]
        cancel_events = [e for e in pid_events
                         if "cancel" in (e.get("event_type") or "").lower()]
        place_events = [e for e in pid_events
                        if "place" in (e.get("event_type") or "").lower()]
        stop_fires = [e for e in pid_events
                      if "stop_loss" in (e.get("event_type") or "").lower()]
        take_profits = [e for e in pid_events
                        if "take_profit" in (e.get("event_type") or "").lower()]
        fee_floor_clamps = [e for e in pid_events
                            if "fee_floor" in (e.get("event_type") or "").lower()]
        reblends = [e for e in pid_events
                    if "reblend" in (e.get("event_type") or "").lower()]
        reanchors = [e for e in pid_events
                     if "reanchor" in (e.get("event_type") or "").lower()]

        product_net = sum(c["net"] for c in cycles)
        loss_cycles = [c for c in cycles if c["net"] < 0]
        immediate_fires = [c for c in cycles if c["gap_secs"] <= IMMEDIATE_STOP_THRESHOLD_SECS]

        total_net += product_net
        total_loss_cycles += len(loss_cycles)
        total_immediate_fires += len(immediate_fires)

        # Skip products with nothing interesting
        if not loss_cycles and not immediate_fires and abs(product_net) < 0.10:
            continue

        print("\n" + "-" * 88)
        print(f"  {pid}  (contract_size={cs})")
        print(f"  {len(cycles)} cycles, {len(loss_cycles)} losses, {len(immediate_fires)} <5min buy→sell")
        print(f"  net P&L: ${product_net:+.2f}")
        print(f"  cancel events: {len(cancel_events)}  place: {len(place_events)}  "
              f"stop_fires: {len(stop_fires)}  take_profits: {len(take_profits)}  "
              f"fee_floor_clamps: {len(fee_floor_clamps)}  reblends: {len(reblends)}  "
              f"reanchors: {len(reanchors)}")
        print(f"\n  cycles (most-recent-last):")
        print(f"    {'buy_ts':>10} {'buy_px':>10} → {'sell_ts':>10} {'sell_px':>10} "
              f"{'gap':>7} {'gross':>10} {'fees':>7} {'NET':>10}")
        for c in cycles[-8:]:
            flag = ""
            if c["net"] < 0:
                flag = " ✗ RULE #1"
            if c["gap_secs"] <= IMMEDIATE_STOP_THRESHOLD_SECS:
                flag += " ⚡IMMED"
            print(f"    {_fmt_ts(c['buy_ts']):>10} {c['buy_px']:>10.4f} → "
                  f"{_fmt_ts(c['sell_ts']):>10} {c['sell_px']:>10.4f} "
                  f"{c['gap_secs']:>6}s ${c['gross']:>+9.2f} ${c['fees']:>6.2f} "
                  f"${c['net']:>+9.2f}{flag}")

        # For each SELL, find the trade-log event closest in time to classify
        print(f"\n  SELL classification (from trade log within 30s):")
        for c in cycles[-8:]:
            near = [e for e in pid_events
                    if abs(float(e.get("ts") or 0) - c["sell_ts"]) <= 30
                    and "sell" in (e.get("event_type") or "").lower()
                    or "stop_loss" in (e.get("event_type") or "").lower()
                    or "trail" in (e.get("event_type") or "").lower()
                    or "take_profit" in (e.get("event_type") or "").lower()]
            types = Counter(e.get("event_type") for e in near[:5])
            summary = ", ".join(f"{n}×{t}" for t, n in types.most_common(3)) or "no matching log event"
            print(f"    sell {_fmt_ts(c['sell_ts'])} @ ${c['sell_px']:.4f} → {summary}")

    # -- Summary -------------------------------------------------------------
    print("\n" + "=" * 88)
    print(f"  TOTAL across all products (last {LOOKBACK_FILLS_HOURS}h):")
    print(f"    net P&L:            ${total_net:+.2f}")
    print(f"    loss cycles:        {total_loss_cycles}")
    print(f"    immediate-fire (<{IMMEDIATE_STOP_THRESHOLD_SECS}s buy→sell): {total_immediate_fires}")
    print("=" * 88)
    if total_loss_cycles > 0:
        print("\n  → Loss cycles violate feedback_no_net_loss_cycles.md")
        print("    Only stop_loss exits are allowed to close red.")
        print("    If SELL classification above shows 'sleeve_take_profit' or")
        print("    'ratcheted_trail_fire' on a losing cycle, fee-floor clamp was bypassed.")
    if total_immediate_fires > 0:
        print("\n  → Immediate buy→sell (<5min) pattern = re-entry into falling knife.")
        print("    Need post-stop-loss cooldown or discount gate on ARMED_BUY re-arm.")


if __name__ == "__main__":
    main()
