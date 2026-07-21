"""Run expert_reentry consensus RIGHT NOW for every ARMED_BUY sleeve.

Adam 2026-07-21: "Are the experts recalculating entries for all the
contracts?" — the wired path (_maybe_auto_refresh_stale_sleeve) has a
20-minute staleness gate, so freshly-armed sleeves won't get the
consensus vote until they age.

This diag bypasses the gate: for every sleeve in ARMED_BUY state on
adam-live, computes the expert_reentry decision using current price
history + config + Vince state, and prints:

  - action (rebuy / wait / cool_off)
  - buy_px (if rebuy)
  - wait_secs
  - every expert's vote + one-line reason
  - citations for the operative expert(s)

Read-only. Does NOT change any config or place any orders.

Run: python3 diag_expert_reentry_now.py
"""
from __future__ import annotations
import os
import sys
import json
import time
from typing import Any


def _dump(o: Any) -> dict:
    if hasattr(o, "to_dict"):
        try:
            return o.to_dict()
        except Exception:
            pass
    if isinstance(o, dict):
        return o
    return {}


def main() -> None:
    print("=" * 96)
    print("EXPERT RE-ENTRY CONSENSUS — LIVE COMPUTE (bypasses 20-min stale gate)")
    print("=" * 96)

    import redis
    url = os.environ.get("REDIS_URL") or os.environ.get("REDIS_INTERNAL_URL")
    if not url:
        print("\n✗ REDIS_URL not set — run on Render shell")
        return
    r = redis.Redis.from_url(url, decode_responses=True)
    store = json.loads(r.get("silver-swing:store") or "{}")

    tenant = "adam-live"
    tbody = store.get(tenant) or {}
    products = sorted([p for p in tbody.keys()
                       if not p.startswith("__") and isinstance(tbody.get(p), dict)])

    try:
        import expert_reentry as _er
    except Exception as e:
        print(f"\n✗ import expert_reentry failed: {e}")
        return

    mode = getattr(_er, "MODE", "expert")
    print(f"\nexpert_reentry.MODE = {mode}")
    if mode != "expert":
        print(f"  ⚠ consensus is DISABLED — running in preview mode only")

    from broker import BrokerConfig, CoinbaseBroker

    now_ts = time.time()
    checked = 0
    rebuy_count = 0
    wait_count = 0
    cool_off_count = 0
    no_armed_buy = 0

    for pid in products:
        block = tbody[pid] or {}
        cfg = block.get("config") or {}
        state = block.get("state") or {}
        sleeves_cfg = cfg.get("sleeves") or []
        sleeves_state = state.get("sleeves") or {}
        if not sleeves_cfg:
            continue

        # Get contract_size from broker
        try:
            b = CoinbaseBroker(BrokerConfig(product_id=pid))
            spec = b.contract_spec() if hasattr(b, "contract_spec") else {}
            contract_size = float((spec or {}).get("contract_size") or 1)
        except Exception:
            contract_size = 1.0

        # Get price history snapshot
        snap = json.loads(r.get(f"silver-swing:snapshot:{tenant}:{pid}") or "{}")
        history = snap.get("price_history") or []
        prices = [float(p) for p in history if p is not None]

        for sc in sleeves_cfg:
            sid = sc.get("id") or "?"
            ss = sleeves_state.get(sid) or {}
            sst = ss.get("state") or "?"
            if sst != "ARMED_BUY":
                no_armed_buy += 1
                continue
            checked += 1

            sold_ref = float(ss.get("last_sell_fill_price") or 0)
            if sold_ref <= 0:
                # Fallback: use current mark
                sold_ref = float(sc.get("buy_px") or 0)
            spread = max(0.005, float(sc.get("sell_px") or 0) - float(sc.get("buy_px") or 0))
            losing_streak = int(ss.get("cycles_losing_streak") or 0)
            last_loss_ts = ss.get("last_loss_ts")
            fee_rt = float(cfg.get("fee_per_contract_roundtrip") or 0)
            qty = max(1, int(sc.get("qty") or 1))

            try:
                dec = _er.compute_reentry_decision(
                    prices=prices,
                    last_sell_price=sold_ref,
                    spread=spread,
                    losing_streak=losing_streak,
                    fee_per_roundtrip=fee_rt,
                    contract_size=contract_size,
                    qty=qty,
                    now_ts=now_ts,
                    last_loss_ts=last_loss_ts,
                )
            except Exception as e:
                print(f"\n  {pid}/{sid}: expert_reentry raised: {e}")
                continue

            print(f"\n{'─' * 96}")
            print(f"{pid}  ·  {sid}   (contract_size={contract_size}, qty={qty})")
            print("─" * 96)
            print(f"  ARMED_BUY   sold_ref=${sold_ref:.6f}   spread=${spread:.6f}")
            print(f"  losing_streak={losing_streak}   last_loss_ts={last_loss_ts}")
            print(f"  price_history len={len(prices)}   (Wilder/Kaufman need >= 30/21)")

            print(f"\n  DECISION: {dec.action.upper()}")
            if dec.buy_px:
                print(f"    buy_px = ${dec.buy_px:.6f}")
            if dec.wait_secs > 0:
                mins = dec.wait_secs / 60.0
                print(f"    wait_secs = {dec.wait_secs} ({mins:.1f} min)")

            if dec.expert_votes:
                print(f"\n  EXPERT VOTES:")
                for expert, vote in dec.expert_votes.items():
                    if isinstance(vote, tuple):
                        vote = vote[0] if vote else ""
                    print(f"    {expert}:")
                    print(f"      {vote}")

            if dec.citations:
                print(f"\n  CITATIONS:")
                for c in dec.citations:
                    print(f"    · {c}")

            if dec.action == "rebuy":
                rebuy_count += 1
            elif dec.action == "wait":
                wait_count += 1
            elif dec.action == "cool_off":
                cool_off_count += 1

    print("\n" + "=" * 96)
    print("SUMMARY")
    print("=" * 96)
    print(f"  ARMED_BUY sleeves checked:   {checked}")
    print(f"    REBUY signal:              {rebuy_count}")
    print(f"    WAIT signal:               {wait_count}")
    print(f"    COOL_OFF (Vince):          {cool_off_count}")
    print(f"  sleeves not ARMED_BUY:       {no_armed_buy}  (holding or other state)")
    print()
    print("  Note: this bypasses the 20-min staleness gate in "
          "_maybe_auto_refresh_stale_sleeve.")
    print("  In production, the bot only invokes expert_reentry after a sleeve "
          "has been")
    print("  ARMED_BUY for >= 20 min AND at most once per minute per sleeve.")
    print()
    print("  To see live decisions from the running bot, tail the trade log "
          "for events")
    print("  named 'expert_reentry_decision' — each has the action + votes + "
          "citations.")


if __name__ == "__main__":
    main()
