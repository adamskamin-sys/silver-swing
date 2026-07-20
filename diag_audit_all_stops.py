"""Audit every held position's actual exchange stop vs bot state vs mark.

Adam 2026-07-20: dashboard shows several products with TRIGGER above MARK
(MC $2698.50 vs $2687.50, NER $1.9518 vs $1.9104, etc). Either the
display is stale (showing a config value while a different actual stop
sits on Coinbase) or those stops SHOULD have fired and didn't.

This script pulls ground truth for every held product:
  - Coinbase position qty
  - Coinbase live mark
  - Every OPEN stop-limit order on that product (side=SELL) — real trigger
  - Bot state: ss.resting_stop_px, ss.resting_stop_stage
  - Bot's own_avg_entry

Flags:
  🔴 UNPROTECTED — position > 0 but no exchange stop found
  🔴 STOP >= MARK — trigger already crossed, should have fired
  ⚠  DRIFT — exchange trigger differs from bot state by > 2%
  ⚠  STALE — bot state has resting_stop_oid but no matching open order

Read-only. Zero side effects.

Usage:  python3 diag_audit_all_stops.py
"""
from __future__ import annotations
import os


def _dump(x):
    """Same as broker._dump — normalize SDK response to dict."""
    if x is None:
        return {}
    if hasattr(x, "to_dict"):
        return x.to_dict()
    if isinstance(x, dict):
        return x
    try:
        return dict(x)
    except Exception:
        return {}


def main() -> None:
    print("=" * 100)
    print("EXCHANGE STOP AUDIT — every held position, actual Coinbase state vs bot state")
    print("=" * 100)

    from broker import BrokerConfig, CoinbaseBroker
    import state_store

    # Bootstrap one broker to hit shared APIs (positions, list_orders).
    b0 = CoinbaseBroker(BrokerConfig(product_id="SLR-27AUG26-CDE"))
    client = b0.client

    # 1. All futures positions
    try:
        positions_raw = _dump(client.list_futures_positions()).get("positions") or []
    except Exception as e:
        print(f"✗ list_futures_positions failed: {e}")
        return
    held = []
    for p in positions_raw:
        try:
            n = int(float(p.get("number_of_contracts") or 0))
        except (TypeError, ValueError):
            n = 0
        side = str(p.get("side") or "").upper()
        signed = n if side == "LONG" else -n
        if signed != 0:
            held.append((p.get("product_id"), signed))
    print(f"\nHeld positions: {len(held)}\n")

    # 2. All open orders across the account
    try:
        orders_raw = _dump(client.list_orders(order_status=["OPEN"])).get("orders") or []
    except Exception as e:
        print(f"✗ list_orders failed: {e}")
        return
    # Bucket by product_id, filter for SELL stop-limit shapes
    stops_by_pid: dict[str, list[dict]] = {}
    for o in orders_raw:
        pid = o.get("product_id")
        side = str(o.get("side") or "").upper()
        if side != "SELL" or not pid:
            continue
        cfg = o.get("order_configuration") or {}
        shape = cfg.get("stop_limit_stop_limit_gtc") or cfg.get("stop_limit_stop_limit_gtd")
        if not shape:
            continue
        try:
            stop_px = float(shape.get("stop_price") or 0)
            limit_px = float(shape.get("limit_price") or 0)
            qty = int(float(shape.get("base_size") or 0))
        except (TypeError, ValueError):
            continue
        stops_by_pid.setdefault(pid, []).append({
            "oid": o.get("order_id"),
            "stop": stop_px,
            "limit": limit_px,
            "qty": qty,
            "direction": shape.get("stop_direction"),
        })

    # 3. Bot state
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))
    raw = store._load() or {}
    def bot_stops_for(pid: str) -> list[dict]:
        out = []
        for tenant, tenant_data in raw.items():
            if not isinstance(tenant_data, dict):
                continue
            entry = tenant_data.get(pid)
            if not isinstance(entry, dict):
                continue
            sleeves = (entry.get("state") or {}).get("sleeves") or {}
            for sid, ss in sleeves.items():
                if ss.get("resting_stop_oid") or ss.get("own_avg_entry"):
                    out.append({
                        "tenant": tenant, "sleeve_id": sid,
                        "state": ss.get("state"),
                        "own_avg": ss.get("own_avg_entry"),
                        "resting_stop_oid": ss.get("resting_stop_oid"),
                        "resting_stop_px": ss.get("resting_stop_px"),
                        "resting_stop_stage": ss.get("resting_stop_stage"),
                    })
        return out

    # 4. Per-product report
    flags_total = 0
    for pid, qty in sorted(held, key=lambda x: x[0]):
        # Live mark
        try:
            bp = CoinbaseBroker(BrokerConfig(product_id=pid))
            mark = float(bp.mark_price()) if hasattr(bp, "mark_price") else 0.0
        except Exception:
            mark = 0.0
        exch_stops = stops_by_pid.get(pid, [])
        bot_stops = bot_stops_for(pid)
        line = f"── {pid}  qty={qty}  mark=${mark or '—'}"
        print(line)
        print("─" * len(line))
        # Exchange stops
        if not exch_stops:
            print(f"  🔴 EXCHANGE: NO OPEN STOP-LIMIT SELL — position UNPROTECTED")
            flags_total += 1
        else:
            for s in exch_stops:
                fired_flag = ""
                if mark > 0 and s["stop"] >= mark:
                    fired_flag = " 🔴 SHOULD-HAVE-FIRED (stop ≥ mark)"
                    flags_total += 1
                print(f"  ✓ EXCHANGE stop=${s['stop']:.5f} limit=${s['limit']:.5f} qty={s['qty']} oid={s['oid'][:8]}…{fired_flag}")
        # Bot state
        if not bot_stops:
            print(f"  ⚠  BOT: no sleeve claims this product (position may be un-tracked)")
        else:
            for ss in bot_stops:
                bot_px = ss.get("resting_stop_px") or 0
                bot_oid = ss.get("resting_stop_oid")
                drift_flag = ""
                # Check drift vs any exchange stop
                if exch_stops and bot_px and bot_px > 0:
                    closest = min(exch_stops, key=lambda s: abs(s["stop"] - float(bot_px)))
                    pct = abs(closest["stop"] - float(bot_px)) / max(1e-9, float(bot_px)) * 100
                    if pct > 2.0:
                        drift_flag = f" ⚠ DRIFT: bot=${bot_px} vs exch=${closest['stop']:.5f} ({pct:.1f}%)"
                        flags_total += 1
                # Check for stale oid
                stale_flag = ""
                if bot_oid:
                    matching = [s for s in exch_stops if s["oid"] == bot_oid]
                    if not matching:
                        stale_flag = f" ⚠ STALE oid: {bot_oid[:8]}… not found in open orders"
                        flags_total += 1
                print(f"  BOT   tenant={ss['tenant']} sleeve={ss['sleeve_id']} "
                      f"state={ss['state']} own_avg=${ss.get('own_avg')} "
                      f"resting_stop_px=${bot_px} stage={ss.get('resting_stop_stage')}"
                      f"{drift_flag}{stale_flag}")
        print()

    print("=" * 100)
    if flags_total == 0:
        print("✓ ALL POSITIONS PROTECTED, EXCHANGE ↔ BOT IN SYNC")
    else:
        print(f"🔴 {flags_total} flag(s) raised — review the 🔴 / ⚠ lines above")
    print("=" * 100)


if __name__ == "__main__":
    main()
