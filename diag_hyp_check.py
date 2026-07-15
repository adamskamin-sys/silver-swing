"""Diagnose HYP 20 DEC 30 phantom-position bug (2026-07-15).

Adam observed: HYP 20 DEC 30 modal shows Position: 1 LONG @ $66.81,
but Coinbase Derivatives page has no HYP 20 DEC 30 — only HYPE PERP
at the exact same qty + avg. Two hypotheses:

  A) Sleeve product_id is wrong — it says HYP-20DEC30-CDE but the
     broker is talking to HYPE-PERP-CDE. This is DANGEROUS because
     a sell trigger would exit the actual HYPE PERP position.

  B) Sleeve state has stale own_avg_entry / position tracking; the
     broker for HYP-20DEC30-CDE returns 0 (no position). Sell would
     400 at Coinbase. Annoying, not dangerous.

Reports:
  1. All sleeves in state.json for any HYP* product — shows the
     product_id the sleeve THINKS it's managing
  2. Coinbase positions for both HYPE-PERP-CDE and HYP-20DEC30-CDE
     (whichever exist) so we can see the mismatch
  3. Recent trade-log events mentioning either product
  4. VERDICT: A, B, or neither

Read-only. Usage: python3 diag_hyp_check.py
"""
from __future__ import annotations
import json
import os
import sys


HYP_PRODUCT_IDS = [
    "HYP-20DEC30-CDE",
    "HYPE-PERP-CDE",
    "HYPE-PERP",
    "HYP-PERP-CDE",
    "HYP-PERP",
]


def _load_raw_state() -> dict:
    """Load the full state blob via the same code path the bot uses.
    make_store() returns RedisJsonStore on Render (REDIS_URL set) or a
    JsonFileStateStore locally. Both expose _load() → full nested dict."""
    data_dir = os.getenv("SWING_DATA_DIR", "data")
    try:
        import state_store
        store = state_store.make_store(data_dir)
        return store._load()
    except Exception as e:
        print(f"  WARN: state_store.make_store failed: {e}")
    # Fallback: try to find a raw JSON file
    for name in ("store.json", "state.json", "swing_state.json"):
        path = os.path.join(data_dir, name)
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception as e:
                print(f"  WARN: could not parse {path}: {e}")
    return {}


def _walk_state_for_hyp(raw: dict) -> list[dict]:
    """Walk state, return every dict entry whose path or content mentions HYP."""
    hits: list[dict] = []

    def walk(node, path=""):
        if isinstance(node, dict):
            path_upper = path.upper()
            for k, v in node.items():
                key_upper = str(k).upper()
                # Product-id keys
                if "HYP" in key_upper and isinstance(v, dict):
                    hits.append({"path": f"{path}/{k}", "value": v})
                walk(v, f"{path}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(raw)
    return hits


def _summarize_state_node(node: dict, path: str) -> None:
    """Print the interesting bits of a state subtree for a product."""
    print(f"\n  PATH: {path}")
    if "config" in node:
        cfg = node["config"]
        product = cfg.get("product_id") or cfg.get("symbol") or "?"
        print(f"    config.product_id = {product}")
        sleeves = cfg.get("sleeves") or []
        print(f"    config.sleeves count = {len(sleeves)}")
        for i, s in enumerate(sleeves):
            print(f"      sleeve[{i}]: id={s.get('id')} name={s.get('name')} qty={s.get('qty')}")
    if "state" in node:
        st = node["state"]
        print(f"    state keys: {list(st.keys())[:15]}")
        # Position info
        for k in ("position_qty", "position_avg", "avg_entry"):
            if k in st:
                print(f"    state.{k} = {st.get(k)}")
        # Sleeve states
        ssm = st.get("sleeves") or {}
        for sid, ss in ssm.items():
            print(f"    sleeve_state[{sid}]:")
            for k in ("state", "own_avg_entry", "live_order_id", "cycles",
                     "realized_pnl", "filled_qty"):
                if k in ss:
                    print(f"        {k} = {ss[k]}")


def _query_coinbase_positions() -> None:
    """Try to hit the live broker and dump positions for HYP variants."""
    print("\n" + "=" * 70)
    print("LIVE COINBASE POSITIONS for HYP variants:")
    print("=" * 70)
    try:
        from broker import BrokerConfig, CoinbaseBroker
        for pid in HYP_PRODUCT_IDS:
            try:
                b = CoinbaseBroker(BrokerConfig(product_id=pid))
                qty = b.position_qty()
                if qty != 0:
                    print(f"  {pid:25s} → position = {qty} contracts")
                else:
                    print(f"  {pid:25s} → position = 0 (no holding)")
            except Exception as e:
                print(f"  {pid:25s} → ERROR: {str(e)[:80]}")
    except Exception as e:
        print(f"  Could not load broker: {e}")


def main() -> None:
    print("=" * 70)
    print("HYP PHANTOM POSITION DIAGNOSIS")
    print("=" * 70)

    raw = _load_raw_state()
    if not raw:
        print("\nNO STATE — set SWING_DATA_DIR or ensure state_store is reachable.")
        return

    # 1) Find every HYP-related subtree in state
    hits = _walk_state_for_hyp(raw)
    print(f"\nState subtrees mentioning HYP: {len(hits)}")
    seen_paths = set()
    for hit in hits[:15]:
        if hit["path"] in seen_paths:
            continue
        seen_paths.add(hit["path"])
        v = hit["value"]
        # Only summarize top-level product entries (have config or state keys)
        if isinstance(v, dict) and ("config" in v or "state" in v):
            _summarize_state_node(v, hit["path"])

    # 2) Query Coinbase for the actual positions
    _query_coinbase_positions()

    # 3) Recent trade log
    print("\n" + "=" * 70)
    print("RECENT HYP TRADE-LOG EVENTS (last 2000):")
    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        events = list(log.events())[-2000:]
        hits = [e for e in events if "HYP" in str(e).upper()]
        print(f"  {len(hits)} events")
        for e in hits[-10:]:
            et = e.get("event_type", "?")
            ts = e.get("ts", "?")
            sym = e.get("symbol", "?")
            reason = e.get("reason") or e.get("error") or ""
            print(f"    {ts}  sym={sym:20s} {et:35s} {str(reason)[:50]}")
    except Exception as e:
        print(f"  could not read trade log: {e}")


if __name__ == "__main__":
    main()
