"""Adopt an extra Coinbase-held contract that the bot doesn't currently
track — creates a new sleeve so the extra contract is managed instead
of sitting orphaned.

Adam 2026-07-19: XLP-20DEC30-CDE reconciliation persistently shows
exchange=2 vs bot=1 (Δ+1). You manually bought 1 extra XLP contract
at some point; the bot's existing sleeve only knows about 1 of them.
The extra contract has no stop-loss protection because no sleeve owns
it. This diag creates a matching sleeve with your actual buy_avg so
the bot adopts it into the same management pipeline as the first.

Generic — works on any product with an extra Coinbase-held contract.
Usage:
    python3 diag_adopt_xlp_position.py XLP-20DEC30-CDE               # dry-run
    python3 diag_adopt_xlp_position.py XLP-20DEC30-CDE --apply       # execute
    python3 diag_adopt_xlp_position.py XLP-20DEC30-CDE --apply --buy-avg 0.18999
"""
from __future__ import annotations
import os
import sys
import time
import uuid


def main() -> None:
    if len(sys.argv) < 2:
        print("USAGE: python3 diag_adopt_xlp_position.py <PRODUCT_ID> "
              "[--apply] [--buy-avg PRICE]")
        return
    product_id = sys.argv[1]
    apply = "--apply" in sys.argv
    manual_buy_avg = None
    if "--buy-avg" in sys.argv:
        try:
            manual_buy_avg = float(sys.argv[sys.argv.index("--buy-avg") + 1])
        except (IndexError, ValueError):
            print("✗ --buy-avg needs a numeric price")
            return

    print("=" * 78)
    print(f"ADOPT EXTRA CONTRACT{'  (APPLYING)' if apply else '  (dry-run)'} — {product_id}")
    print("=" * 78)

    # Coinbase ground truth
    from broker import BrokerConfig, CoinbaseBroker
    b = CoinbaseBroker(BrokerConfig(product_id=product_id))
    try:
        cb_qty = int(b.position_qty())
    except Exception as e:
        print(f"✗ position_qty() failed: {e}")
        return
    try:
        cb_avg = float(b.position.avg_entry or 0)
    except Exception:
        cb_avg = 0.0
    print(f"\nCoinbase position: {cb_qty} contract(s) @ avg ${cb_avg}")

    if cb_qty <= 0:
        print(f"✗ No position on Coinbase for {product_id}. Nothing to adopt.")
        return

    # Bot state
    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))
    raw = store._load()

    target_tenant = None
    target_state = None
    target_cfg = None
    for tenant, tdata in raw.items():
        if not isinstance(tdata, dict): continue
        entry = tdata.get(product_id)
        if not isinstance(entry, dict): continue
        if not tenant.endswith("-live"): continue
        target_tenant = tenant
        target_state = entry.get("state") or {}
        target_cfg = entry.get("config") or {}
        break

    if target_tenant is None:
        print(f"✗ {product_id} not found on any -live tenant. Attach a strategy first.")
        return

    # Count bot's known qty across all sleeves
    sleeves_cfg = list(target_cfg.get("sleeves") or [])
    sleeves_state = target_state.get("sleeves") or {}
    bot_qty = 0
    for sc in sleeves_cfg:
        ss = sleeves_state.get(sc.get("id"), {}) or {}
        # Only count sleeves that hold a position (own_avg_entry set)
        if ss.get("own_avg_entry"):
            bot_qty += int(sc.get("qty") or 0)

    print(f"Bot's tracked qty: {bot_qty} across {len(sleeves_state)} sleeve(s)")
    for sid, ss in sleeves_state.items():
        if not isinstance(ss, dict): continue
        oa = ss.get("own_avg_entry")
        state = ss.get("state")
        cfg_qty = next((int(sc.get("qty") or 0) for sc in sleeves_cfg
                         if sc.get("id") == sid), 0)
        marker = " ← holds" if oa else ""
        print(f"  {sid}: state={state} qty={cfg_qty} own_avg={oa}{marker}")

    delta = cb_qty - bot_qty
    if delta <= 0:
        print(f"\n✓ Bot already tracks {bot_qty} of {cb_qty} contracts. Nothing to adopt.")
        return

    print(f"\n⚠ {delta} contract(s) on Coinbase are NOT tracked by any sleeve")

    # Pick buy_avg for the new sleeve
    adopt_avg = manual_buy_avg if manual_buy_avg is not None else cb_avg
    if adopt_avg <= 0:
        print(f"✗ Coinbase avg=0 and no --buy-avg provided. Provide --buy-avg PRICE.")
        return

    new_sid = f"adopt-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    print(f"\nWOULD create new sleeve:")
    print(f"  id: {new_sid}")
    print(f"  qty: {delta}")
    print(f"  state: ARMED_SELL (holds position)")
    print(f"  own_avg_entry: ${adopt_avg}")
    print(f"  buy_px / sell_px: copied from existing sleeve config OR safe defaults")

    if not apply:
        print(f"\n(dry-run — pass --apply to execute)")
        return

    # Build the new sleeve config from an existing sleeve as a template
    # (uses same strategy shape / experts / stop-loss cfg). Fall back to
    # a safe minimal config if no existing sleeve.
    template = sleeves_cfg[0] if sleeves_cfg else {}
    new_sleeve_cfg = dict(template)
    new_sleeve_cfg["id"] = new_sid
    new_sleeve_cfg["name"] = f"adopt orphan {product_id}"
    new_sleeve_cfg["qty"] = delta
    # Anchor the sell/buy targets around the adopt_avg (small profit +
    # generous stop). Don't inherit a stale sell_px from the template
    # that might not match the actual buy.
    new_sleeve_cfg.setdefault("exit_mode", "fixed_limit")
    new_sleeve_cfg["sell_px"] = round(adopt_avg * 1.005, 6)  # +0.5% target
    new_sleeve_cfg["buy_px"] = round(adopt_avg * 0.995, 6)   # -0.5% rebuy
    new_sleeve_cfg["stop_loss_enabled"] = True
    new_sleeve_cfg["resting_stop_enabled"] = True

    # Corresponding sleeve state — ARMED_SELL, holds position at adopt_avg
    from swing_leg import SleeveState, SleeveStateEnum
    new_ss = SleeveState(
        id=new_sid, state=SleeveStateEnum.ARMED_SELL,
        own_avg_entry=float(adopt_avg),
    )

    # Persist
    target_cfg["sleeves"] = list(sleeves_cfg) + [new_sleeve_cfg]
    store.put_config(target_tenant, product_id, target_cfg)

    sleeves_state[new_sid] = new_ss.to_dict()
    target_state["sleeves"] = sleeves_state
    store.put_state(target_tenant, product_id, target_state)

    print(f"\n✓ APPLIED. New sleeve {new_sid} adopted {delta} contract(s) @ ${adopt_avg}.")
    print(f"  Bot's next tick will:")
    print(f"    1. Reload sleeve state from Redis (commit 83dd31b)")
    print(f"    2. See new sleeve in ARMED_SELL")
    print(f"    3. Place a resting stop via _maintain_resting_stop")
    print(f"    4. Reconciliation mismatch clears within 60s")


if __name__ == "__main__":
    main()
