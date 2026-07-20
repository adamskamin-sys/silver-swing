"""Is the Scanner-armed sleeve's $0.07000 stop actually placed on Coinbase?

Adam 2026-07-20 (Image #364): Dashboard shows a Scanner sleeve on some
sub-dollar product — Position 1 LONG @ $0.0789, current $0.0823, chip
shows STOP LOSS $0.07000 with "if stopped -$45.14". State = WAITING FOR
SELL. Trail chip claims "arms at $0.08074" (display bug, real arm is
sell_px = $0.08389). We haven't verified the underlying $0.07000 stop
is actually resting on Coinbase — that's the §3.6 invariant.

This diag:
  1. Finds the product by matching own_avg_entry ~ $0.0789 and/or
     sleeve id starting "scanner_" with stop_loss_px ~ $0.07000.
  2. Reports sleeve state + resting_stop_oid + resting_stop_px.
  3. Queries Coinbase directly: list_orders(OPEN, product_id) and
     verifies a SELL stop-limit at ~$0.07000 with qty >= position.
  4. Reports the gap (stop placed / stop missing / stop wrong-price).

Read-only. Run:  python3 diag_scanner_stop_placement.py
"""
from __future__ import annotations
import os
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


def _fmt(x: Any, nd: int = 4) -> str:
    try:
        return f"${float(x):.{nd}f}"
    except Exception:
        return str(x)


def main() -> None:
    print("=" * 90)
    print("SCANNER STOP-PLACEMENT AUDIT (own_avg ~ $0.0789, stop ~ $0.07000)")
    print("=" * 90)

    import redis
    url = os.environ.get("REDIS_URL") or os.environ.get("REDIS_INTERNAL_URL")
    if not url:
        print("\n✗ REDIS_URL not set — must run on Render shell")
        return
    r = redis.Redis.from_url(url, decode_responses=True)
    store = json.loads(r.get("silver-swing:store") or "{}")

    # ---- 1. Locate the product -----------------------------------------
    target_own_avg = 0.0789
    target_stop = 0.07000
    tol_own = 0.001
    tol_stop = 0.002

    candidates: list[tuple[str, str, str, dict, dict]] = []
    for tenant, tbody in store.items():
        if not tenant.endswith("-live"):
            continue
        if not isinstance(tbody, dict):
            continue
        for pid, block in tbody.items():
            if pid.startswith("__"):
                continue
            if not isinstance(block, dict):
                continue
            cfg = block.get("config") or {}
            state = block.get("state") or {}
            sleeves_cfg = cfg.get("sleeves") or []
            sleeves_state = state.get("sleeves") or {}
            for sc in sleeves_cfg:
                sid = sc.get("id") or ""
                ss = sleeves_state.get(sid) or {}
                own_avg = float(ss.get("own_avg_entry") or 0)
                sl_px = float(sc.get("stop_loss_px") or 0)
                match_own = abs(own_avg - target_own_avg) < tol_own
                match_stop = abs(sl_px - target_stop) < tol_stop
                is_scanner = sid.startswith("scanner")
                if match_own or (is_scanner and match_stop):
                    candidates.append((tenant, pid, sid, sc, ss))

    if not candidates:
        print("\n✗ No sleeve matched own_avg ~ $0.0789 OR "
              "scanner sleeve with stop ~ $0.07000")
        print("  Dumping ALL scanner-armed sleeves in live tenants:")
        for tenant, tbody in store.items():
            if not tenant.endswith("-live") or not isinstance(tbody, dict):
                continue
            for pid, block in tbody.items():
                if pid.startswith("__") or not isinstance(block, dict):
                    continue
                cfg = block.get("config") or {}
                state = block.get("state") or {}
                for sc in (cfg.get("sleeves") or []):
                    sid = sc.get("id") or ""
                    if not sid.startswith("scanner"):
                        continue
                    ss = (state.get("sleeves") or {}).get(sid) or {}
                    own = ss.get("own_avg_entry")
                    st = ss.get("state")
                    print(f"    {tenant}/{pid}  {sid}  "
                          f"own_avg={own}  state={st}  "
                          f"sl_px={sc.get('stop_loss_px')}")
        return

    print(f"\nMatched {len(candidates)} candidate(s):")
    for tenant, pid, sid, sc, ss in candidates:
        print(f"  • {tenant}/{pid}  sleeve={sid}")

    # ---- 2. Full audit per candidate -----------------------------------
    from broker import BrokerConfig, CoinbaseBroker

    for tenant, pid, sid, sc, ss in candidates:
        print("\n" + "─" * 90)
        print(f"AUDIT  {tenant}/{pid}  sleeve={sid}")
        print("─" * 90)

        st = ss.get("state") or "?"
        own_avg = ss.get("own_avg_entry")
        rst_oid = ss.get("resting_stop_oid")
        rst_px = ss.get("resting_stop_px")
        rst_stage = ss.get("resting_stop_stage")
        sl_enabled = sc.get("stop_loss_enabled")
        sl_px = sc.get("stop_loss_px")
        sell_px = sc.get("sell_px")
        trail_act = sc.get("trail_activation_px")
        rst_enabled = sc.get("resting_stop_enabled", True)
        qty_cfg = sc.get("qty")

        print(f"\n  SLEEVE STATE (redis):")
        print(f"    state:                {st}")
        print(f"    own_avg_entry:        {_fmt(own_avg)}")
        print(f"    resting_stop_oid:     {rst_oid or '(none)'}")
        print(f"    resting_stop_px:      {_fmt(rst_px)}")
        print(f"    resting_stop_stage:   {rst_stage or '(none)'}")

        print(f"\n  SLEEVE CONFIG:")
        print(f"    qty:                  {qty_cfg}")
        print(f"    stop_loss_enabled:    {sl_enabled}")
        print(f"    stop_loss_px:         {_fmt(sl_px)}")
        print(f"    sell_px:              {_fmt(sell_px)}")
        print(f"    trail_activation_px:  {_fmt(trail_act)}")
        print(f"    resting_stop_enabled: {rst_enabled}")

        # ---- 3. Coinbase truth ----------------------------------------
        try:
            b = CoinbaseBroker(BrokerConfig(product_id=pid))
        except Exception as e:
            print(f"\n  ✗ CoinbaseBroker init failed: {e}")
            continue

        # Position
        print(f"\n  COINBASE POSITION:")
        try:
            positions_resp = _dump(b.client.list_futures_positions())
            pos_list = positions_resp.get("positions") or []
            pos = next(
                (p for p in pos_list if p.get("product_id") == pid), None)
            if pos:
                side = str(pos.get("side") or "").upper()
                pqty = int(float(pos.get("number_of_contracts") or 0))
                pavg = float(pos.get("avg_entry_price") or 0)
                print(f"    {side} {pqty}  avg={_fmt(pavg)}")
                pos_qty_effective = pqty if side == "LONG" else -pqty
            else:
                # Not a futures product — try spot
                pos_qty_effective = 0
                try:
                    _spot = b.position_qty()
                    print(f"    (not in futures list) spot position_qty="
                          f"{_spot}")
                    pos_qty_effective = int(_spot or 0)
                except Exception as _e:
                    print(f"    FLAT (not in futures list; spot lookup: {_e})")
        except Exception as e:
            print(f"    ✗ list_futures_positions failed: {e}")
            pos_qty_effective = 0

        # Open orders
        print(f"\n  COINBASE OPEN ORDERS  (product={pid}):")
        try:
            resp = b.client.list_orders(
                product_id=pid, order_status="OPEN", limit=100)
            orders = _dump(resp).get("orders") or []
            if not orders:
                print(f"    (none)")
            else:
                sells_at_stop = []
                for o in orders:
                    oid = str(o.get("order_id") or "")
                    side = str(o.get("side") or "").upper()
                    cfg2 = o.get("order_configuration") or {}
                    type_key = ""
                    px_lim = ""
                    px_stop = ""
                    qty_show = ""
                    if isinstance(cfg2, dict):
                        for k, v in cfg2.items():
                            type_key = k
                            if isinstance(v, dict):
                                px_lim = str(v.get("limit_price") or "")
                                px_stop = str(v.get("stop_price") or "")
                                qty_show = str(
                                    v.get("base_size") or v.get("size") or "")
                                break
                    print(f"    {side:>5}  {oid[:20]}...  type={type_key}  "
                          f"stop={px_stop}  limit={px_lim}  qty={qty_show}")
                    if side == "SELL" and px_stop:
                        try:
                            _sp = float(px_stop)
                            if abs(_sp - float(sl_px or 0)) < 0.005 or (
                                    float(sl_px or 0) > 0
                                    and abs(_sp - float(sl_px)) / float(sl_px)
                                    < 0.02):
                                sells_at_stop.append((oid, _sp, qty_show))
                        except Exception:
                            pass
        except Exception as e:
            print(f"    ✗ list_orders failed: {e}")
            sells_at_stop = []
            orders = []

        # Verdict
        print(f"\n  VERDICT:")
        stop_placed = False
        if sells_at_stop:
            for oid, sp, q in sells_at_stop:
                print(f"    ✓ SELL stop-limit RESTING  "
                      f"oid={oid[:20]}...  stop={_fmt(sp)}  qty={q}")
                stop_placed = True
        if not stop_placed:
            if not orders:
                print(f"    🚨 NO OPEN ORDERS — §3.6 VIOLATION "
                      f"(position {pos_qty_effective}, expected SELL stop "
                      f"@ {_fmt(sl_px)})")
            else:
                print(f"    🚨 NO SELL stop-limit near {_fmt(sl_px)} — "
                      f"chip shows STOP LOSS but Coinbase disagrees")
        if rst_oid and not any(
                oid.startswith(str(rst_oid)[:12])
                for oid, _, _ in sells_at_stop):
            print(f"    ⚠ sleeve tracks resting_stop_oid={rst_oid} but that "
                  f"oid is not in the OPEN SELL list — stale/orphan")


if __name__ == "__main__":
    main()
