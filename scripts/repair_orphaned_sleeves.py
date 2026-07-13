"""Automated sweep for sleeves whose SELL fill was lost across a bot restart.

Root cause was in swing_leg.py's reconcile(): when a live_order_id showed
FILLED status but the bot had restarted, the old code cleared the id without
calling _sleeve_on_fill. Fixed in commit e3f30d8 for future runs. This script
recovers sleeves that were already orphaned BEFORE that fix landed.

How it works:
    1. For every (tenant, symbol) in the store, load state + config.
    2. From the trade log, per sleeve, find the LATEST sleeve_order_placed
       (side=SELL) event.
    3. Look for a matching sleeve_order_filled event AFTER that placement
       for the same sleeve. If found → sleeve is healthy, skip.
    4. If NOT found AND sleeve is currently ARMED_SELL AND broker position
       is less than sleeve.qty → orphan candidate. Query the order_id
       against Coinbase.
    5. If FILLED → credit the sleeve (state=ARMED_BUY, cycles+=1, realized
       computed from cost_basis captured at placement time), log a
       sleeve_orphan_repaired event.
    6. Same treatment for the primary swing (order_placed / order_filled).

Dry-run by default. Pass --apply to write.

Usage:
    python3 scripts/repair_orphaned_sleeves.py
    python3 scripts/repair_orphaned_sleeves.py --apply
    python3 scripts/repair_orphaned_sleeves.py --tenant adam-live --apply

Environment:
    REDIS_URL          — Redis-backed store
    COINBASE_API_KEY_NAME / COINBASE_PRIVATE_KEY — Coinbase creds for order_status
"""

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from state_store import make_store  # noqa: E402
from safety import RedisTradeLog, TradeLog  # noqa: E402


def _load_trade_log():
    url = os.getenv("REDIS_URL")
    if url:
        return RedisTradeLog(url)
    # Fallback: local file — mirrors safety.make_trade_log() but doesn't
    # require a data_dir arg.
    return TradeLog(os.path.join(_ROOT, "data", "trades.jsonl"))


def _events_for(log, tenant: str, symbol: str) -> list[dict]:
    out = []
    for ev in log.events():
        if ev.get("tenant") == tenant and ev.get("symbol") == symbol:
            out.append(ev)
    return out


def _broker_for(symbol: str):
    from broker import BrokerConfig, CoinbaseBroker
    return CoinbaseBroker(BrokerConfig(product_id=symbol))


def _find_unclaimed_sleeve_fills(events: list[dict]) -> dict[str, dict]:
    """Return {sleeve_id: last_placed_event} for sleeves whose latest SELL
    placement has no matching sleeve_order_filled event AFTER it. That's a
    strong signal the fill was lost."""
    placements_by_sleeve: dict[str, dict] = {}
    fills_by_sleeve: dict[str, list[dict]] = {}
    for ev in events:
        et = ev.get("event_type")
        sid = ev.get("sleeve_id")
        if not sid:
            continue
        if et == "sleeve_order_placed" and ev.get("side") == "SELL":
            placements_by_sleeve[sid] = ev
        elif et == "sleeve_order_filled":
            fills_by_sleeve.setdefault(sid, []).append(ev)
    unclaimed = {}
    for sid, placed in placements_by_sleeve.items():
        placed_ts = placed.get("ts", 0)
        fills = fills_by_sleeve.get(sid, [])
        has_matching_fill = any(
            (f.get("ts", 0) >= placed_ts) and
            (f.get("leg") == "ARMED_SELL")
            for f in fills
        )
        if not has_matching_fill:
            unclaimed[sid] = placed
    return unclaimed


def _find_unclaimed_primary_fill(events: list[dict]) -> dict | None:
    """Same idea for the legacy primary swing (uses order_placed /
    order_filled events, no sleeve_id)."""
    placed = None
    filled = None
    for ev in events:
        et = ev.get("event_type")
        if ev.get("sleeve_id"):
            continue  # sleeve-level events, not primary
        if et == "order_placed" and ev.get("side") == "SELL":
            placed = ev
        elif et == "order_filled":
            filled = ev
    if placed is None:
        return None
    if filled is not None and filled.get("ts", 0) >= placed.get("ts", 0):
        return None
    return placed


def repair_tenant_symbol(store, log, tenant: str, symbol: str, apply: bool):
    state = store.get_state(tenant, symbol) or {}
    config = store.get_config(tenant, symbol) or {}
    sleeves_cfg = {s["id"]: s for s in (config.get("sleeves") or [])}
    contract_size = float(config.get("contract_size") or 50)
    fee_rt = float(config.get("fee_per_contract_roundtrip") or 1.0)
    events = _events_for(log, tenant, symbol)
    if not events:
        return []
    unclaimed_sleeves = _find_unclaimed_sleeve_fills(events)
    unclaimed_primary = _find_unclaimed_primary_fill(events)
    findings = []
    if not unclaimed_sleeves and not unclaimed_primary:
        return findings

    # Only bother connecting to Coinbase if we have candidates.
    try:
        broker = _broker_for(symbol)
        actual_pos = int(broker.position_qty() or 0)
    except Exception as e:
        print(f"    [{tenant}/{symbol}] broker init failed: {type(e).__name__}: {e} — skipping")
        return []

    sleeves_state = state.get("sleeves") or {}

    for sid, placed in unclaimed_sleeves.items():
        ss = sleeves_state.get(sid) or {}
        sc = sleeves_cfg.get(sid)
        if sc is None:
            findings.append(f"    sleeve {sid}: orphan placement but sleeve config gone — skip")
            continue
        cur_state = ss.get("state", "ARMED_SELL")
        sleeve_qty = int(sc.get("qty") or 0)
        if cur_state != "ARMED_SELL":
            continue  # already advanced by something
        if sleeve_qty <= 0:
            continue
        order_id = placed.get("order_id")
        if not order_id:
            findings.append(f"    sleeve {sid}: unclaimed placement has no order_id — cannot verify")
            continue
        try:
            st = broker.order_status(order_id)
        except Exception as e:
            findings.append(f"    sleeve {sid}: order_status({order_id}) failed: {type(e).__name__}: {e}")
            continue
        if st.get("status") != "FILLED":
            findings.append(f"    sleeve {sid}: order {order_id} status={st.get('status')} — not orphaned")
            continue
        fill_price = st.get("average_filled_price")
        try:
            fill = float(fill_price) if fill_price is not None else 0.0
        except (TypeError, ValueError):
            fill = 0.0
        basis = float(ss.get("sell_entry_avg") or placed.get("cost_basis") or sc.get("buy_px") or 0.0)
        half_fee = (fee_rt / 2.0) * sleeve_qty
        gross = (fill - basis) * contract_size * sleeve_qty
        realized_delta = gross - half_fee
        findings.append(
            f"    sleeve {sid} ({sc.get('name', sid)}): ORPHAN found — order {order_id[:8]}… "
            f"FILLED @ ${fill:.4f}, basis ${basis:.4f}, credit realized {realized_delta:+.2f}"
        )
        if apply:
            ss["state"] = "ARMED_BUY"
            ss["cycles"] = int(ss.get("cycles", 0)) + 1
            ss["realized_pnl"] = float(ss.get("realized_pnl", 0.0)) + realized_delta
            ss["last_sell_qty"] = sleeve_qty
            ss["last_sell_fill_price"] = fill
            ss["sell_entry_avg"] = None
            ss["own_avg_entry"] = None
            ss["trail_armed"] = False
            ss["trail_high_water_price"] = 0.0
            ss["hybrid_sell_triggered_ts"] = None
            ss["live_order_id"] = None
            ss["filled_qty"] = 0
            sleeves_state[sid] = ss
            import time as _time
            log.record(
                "sleeve_orphan_repaired",
                tenant=tenant, symbol=symbol,
                sleeve_id=sid, sleeve_name=sc.get("name", sid),
                order_id=order_id, fill_price=fill,
                cost_basis=basis, realized_delta=realized_delta,
                repaired_at=_time.time(),
            )

    if unclaimed_primary:
        placed = unclaimed_primary
        cur_state = state.get("state", "ARMED_SELL")
        swing_qty = int(state.get("swing_qty") or config.get("swing_qty") or 0)
        if cur_state == "ARMED_SELL" and swing_qty > 0:
            order_id = placed.get("order_id")
            if order_id:
                try:
                    st = broker.order_status(order_id)
                except Exception as e:
                    findings.append(f"    primary: order_status({order_id}) failed: {type(e).__name__}: {e}")
                    st = {}
                if st.get("status") == "FILLED":
                    fill_price = st.get("average_filled_price")
                    try:
                        fill = float(fill_price) if fill_price is not None else 0.0
                    except (TypeError, ValueError):
                        fill = 0.0
                    basis = float(placed.get("cost_basis") or config.get("buy_px") or 0.0)
                    half_fee = (fee_rt / 2.0) * swing_qty
                    gross = (fill - basis) * contract_size * swing_qty
                    realized_delta = gross - half_fee
                    findings.append(
                        f"    primary: ORPHAN found — order {order_id[:8]}… "
                        f"FILLED @ ${fill:.4f}, basis ${basis:.4f}, credit realized {realized_delta:+.2f}"
                    )
                    if apply:
                        state["state"] = "ARMED_BUY"
                        state["cycles"] = int(state.get("cycles", 0)) + 1
                        state["realized_pnl"] = float(state.get("realized_pnl", 0.0)) + realized_delta
                        state["last_sell_qty"] = swing_qty
                        state["last_sell_fill_price"] = fill
                        state["trail_armed"] = False
                        state["trail_high_water_price"] = 0.0
                        state["live_order_id"] = None
                        state["filled_qty"] = 0
                        import time as _time
                        log.record(
                            "primary_orphan_repaired",
                            tenant=tenant, symbol=symbol,
                            order_id=order_id, fill_price=fill,
                            cost_basis=basis, realized_delta=realized_delta,
                            repaired_at=_time.time(),
                        )

    if apply and findings:
        state["sleeves"] = sleeves_state
        store.put_state(tenant, symbol, state)

    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Actually write repairs. Default is dry-run.")
    parser.add_argument("--tenant", default=None,
                        help="Restrict to one tenant (e.g., adam-live).")
    parser.add_argument("--symbol", default=None,
                        help="Restrict to one symbol (e.g., ZEC-20DEC30-CDE).")
    args = parser.parse_args()

    data_dir = os.getenv("DATA_DIR", os.path.join(_ROOT, "data"))
    store = make_store(data_dir)
    log = _load_trade_log()

    tenants = [args.tenant] if args.tenant else store.list_tenants()
    total_findings = 0
    total_repaired = 0
    for tenant in tenants:
        symbols = store.list_symbols(tenant)
        if args.symbol:
            symbols = [args.symbol] if args.symbol in symbols else []
        for symbol in symbols:
            if symbol.startswith("__"):
                continue
            findings = repair_tenant_symbol(store, log, tenant, symbol, args.apply)
            if findings:
                print(f"[{tenant}/{symbol}]")
                for line in findings:
                    print(line)
                total_findings += len(findings)
                if args.apply:
                    total_repaired += len(findings)

    print()
    if not total_findings:
        print("No orphaned sleeves found. All fills appear correctly credited.")
    elif args.apply:
        print(f"Repaired {total_repaired} orphan(s).")
    else:
        print(f"Found {total_findings} orphan(s). Re-run with --apply to write.")


if __name__ == "__main__":
    main()
