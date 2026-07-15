"""Why did a resting ratchet-stop disappear from Coinbase?

Adam 2026-07-15: HYPE sleeve state showed a live ratchet at $68.20
but Coinbase had NO active HYPE stop — last one was cancelled at
06:06 and never replaced. Six hours of unprotected position.

Walks the resting_stop_* event history for a product and diagnoses:
  * cancel_failed events (cancel didn't succeed → old stop still on book)
  * place_failed events (cancel succeeded but new place errored → GAP)
  * External CANCELLED status (user cancelled via Coinbase UI)
  * Broker rate-limit patterns
  * Whether _maintain_resting_stop is still firing at all

Read-only. Usage:
    python3 diag_ratchet_gap.py                        # HYP-20DEC30-CDE
    python3 diag_ratchet_gap.py COPR-27AUG26-CDE       # different product
    python3 diag_ratchet_gap.py HYP-20DEC30-CDE 720    # last 12h
"""
from __future__ import annotations
import os
import sys
import time


def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return "?"


def main() -> None:
    product_id = sys.argv[1] if len(sys.argv) > 1 else "HYP-20DEC30-CDE"
    minutes_back = float(sys.argv[2]) if len(sys.argv) > 2 else 720.0  # 12h default
    tenant = "adam-live"

    print("=" * 78)
    print(f"RATCHET GAP DIAG — {tenant}/{product_id}  (last {minutes_back:.0f}min)")
    print("=" * 78)

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))

    # Sleeve state — what does the bot think the resting stop is?
    state = store.get_state(tenant, product_id) or {}
    sleeves_state = state.get("sleeves") or {}
    print(f"\n[1] BOT'S VIEW — sleeve state for {product_id}:")
    if not sleeves_state:
        print(f"    ✗ no sleeves in state (no active tracking?)")
    for sid, ss in sleeves_state.items():
        oid = ss.get("resting_stop_oid")
        px = ss.get("resting_stop_px")
        stage = ss.get("resting_stop_stage")
        state_str = ss.get("state")
        own_avg = ss.get("own_avg_entry")
        print(f"    · sleeve={sid} state={state_str}")
        print(f"      own_avg_entry={own_avg}")
        print(f"      resting_stop_oid={oid}")
        print(f"      resting_stop_px={px}  stage={stage}")

    # Coinbase's view — what's actually on the book?
    print(f"\n[2] COINBASE VIEW — list_open_orders for {product_id}:")
    try:
        from broker import BrokerConfig, CoinbaseBroker
        b = CoinbaseBroker(BrokerConfig(product_id=product_id))
        opens = b.list_open_orders() or []
        matches = [o for o in opens if o.get("symbol") == product_id
                   or o.get("product_id") == product_id]
        if not matches:
            print(f"    ✗ NO open orders for {product_id} on Coinbase")
            print(f"      → if bot state says there's a resting stop, GAP CONFIRMED")
        for o in matches:
            print(f"    · oid={str(o.get('order_id'))[:16]}  side={o.get('side')} "
                  f"type={o.get('type')}  price={o.get('price')} "
                  f"stop={o.get('stop_price')}  qty={o.get('qty')}  status={o.get('status')}")

        # Cross-check the specific oid the bot thinks is live
        for sid, ss in sleeves_state.items():
            oid = ss.get("resting_stop_oid")
            if not oid:
                continue
            try:
                stat = b.order_status(oid)
                print(f"\n    Live status of bot's resting_stop_oid ({oid}):")
                for k, v in (stat or {}).items():
                    print(f"      {k}: {v}")
            except Exception as e:
                print(f"\n    ✗ order_status({oid}) failed: {e}")
    except Exception as e:
        print(f"    ✗ broker access failed: {e}")

    # Trade log — what fired recently?
    print(f"\n[3] TRADE LOG events (resting_stop_* + ratchet + tick) for {product_id}:")
    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        cutoff = time.time() - minutes_back * 60
        events = [e for e in log.events()
                  if isinstance(e, dict) and float(e.get("ts", 0) or 0) >= cutoff]
        matches = []
        for e in events:
            sym = str(e.get("symbol") or "")
            et = str(e.get("event_type") or "")
            if sym == product_id and (
                "resting_stop" in et or "ratchet" in et or "maintain" in et
                or "sleeve_arm" in et or "cancel" in et or "place_failed" in et
                or "reconcile" in et):
                matches.append(e)
        matches.sort(key=lambda e: float(e.get("ts") or 0))
        if not matches:
            print(f"    · no relevant events in last {minutes_back}min")
        else:
            # Show last 40
            for e in matches[-40:]:
                ts = _fmt_ts(e.get("ts"))
                et = e.get("event_type", "?")
                bits = []
                for k in ("sleeve_id", "oid", "old_oid", "new_oid", "stage",
                          "stop_price", "target_px", "old_stop_px", "new_stop_px",
                          "resting_stop_px", "error", "reason", "why", "status",
                          "current_position", "intended_position"):
                    if k in e and e[k] not in (None, "", 0, 0.0):
                        v = e[k]
                        if isinstance(v, float):
                            v = f"{v:.4f}"
                        bits.append(f"{k}={v}")
                print(f"    {ts}  {et:42s}  {' '.join(bits)[:220]}")
    except Exception as e:
        print(f"    ✗ trade log read failed: {e}")

    print("\n" + "=" * 78)
    print("DIAGNOSIS")
    print("=" * 78)
    # Simple heuristic diagnosis + state-vs-Coinbase divergence check
    print("→ Compare BOT's resting_stop_px in [1] vs the LAST resting_stop_replaced")
    print("  event's new_stop_px in [3]. If the trade log shows recent state walks")
    print("  (e.g. maintain_resting_stop_updated) but [2] shows NO matching order,")
    print("  we found it: bot state advancing without an actual broker call OR the")
    print("  broker call is throwing silently.")
    for sid, ss in sleeves_state.items():
        oid = ss.get("resting_stop_oid")
        px = ss.get("resting_stop_px")
        if oid and px:
            print(f"\n→ Sleeve {sid}: bot claims a resting stop OID={oid[:12]} @ ${px}")
            print(f"  If Coinbase [2] shows no matching OPEN order, the gap is CONFIRMED.")
        elif ss.get("state") == "ARMED_SELL":
            print(f"\n→ Sleeve {sid} is ARMED_SELL (holding) but NO resting_stop_oid.")
            print(f"  Either never placed, cancelled without replacement, or")
            print(f"  resting_stop_enabled=False for this sleeve.")


if __name__ == "__main__":
    main()
