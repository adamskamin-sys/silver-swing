"""Check NGS state before flipping sleeve safety defaults to ON.

Adam explicitly wants NO stop-loss on NGS ("I don't want to lose on this
one so we can just let it roll"). Before I flip default stop_loss_enabled
to True across sleeves, verify:

  1. Does NGS have a sleeve at all? (If not, defaults change is safe —
     raw position isn't managed by any sleeve.)
  2. If sleeve exists, what's its current stop_loss_enabled state?
  3. Is `stop_loss_enabled` explicitly present in the saved dict?
     If missing, `d.get("stop_loss_enabled", True)` after the default flip
     would silently enable it — which VIOLATES Adam's directive.

Usage (Render silver-swing-bot-live shell):
    python3 diag_ngs_check.py
    python3 diag_ngs_check.py NGS-28JUL26-CDE   # override product id
"""
from __future__ import annotations
import json
import os
import sys


DEFAULT_PRODUCT = "NGS-28JUL26-CDE"


def _load_raw_state() -> dict:
    """Load raw JSON of state (bypassing dataclass deserialization) so we
    can see what fields are explicitly present vs. dataclass-defaulted."""
    data_dir = os.getenv("SWING_DATA_DIR", "data")
    for name in ("state.json", "swing_state.json", "trader_state.json"):
        path = os.path.join(data_dir, name)
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception as e:
                print(f"  WARN: could not parse {path}: {e}")
    return {}


def _find_sleeves_for_product(raw: dict, product: str) -> list[dict]:
    """Walk the state blob looking for sleeve dicts whose product matches."""
    hits: list[dict] = []

    def walk(node, path=""):
        if isinstance(node, dict):
            # A sleeve is a dict with 'id' + 'name' + typically 'buy_px' etc.
            if node.get("id") and (
                "buy_px" in node or "sell_px" in node
                or "stop_loss_enabled" in node
                or "state" in node
            ):
                # heuristic: sleeve payload
                # try to find its owning product from ancestor path
                if product in path or node.get("product") == product:
                    hits.append({"path": path, "sleeve": node})
                    return
            for k, v in node.items():
                walk(v, f"{path}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(raw)
    return hits


def main() -> None:
    product = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PRODUCT
    print("=" * 70)
    print(f"NGS CHECK — product={product}")
    print("=" * 70)

    raw = _load_raw_state()
    if not raw:
        print("\nNO STATE FILE FOUND. Set SWING_DATA_DIR env var.")
        return

    # 1) Search state file for any sleeve matching this product
    sleeves = _find_sleeves_for_product(raw, product)

    print(f"\nSLEEVES FOUND on state file matching '{product}': {len(sleeves)}")
    if not sleeves:
        print("  → NO sleeve on this product.")
        print("  → 1000-unit NGS is a RAW position outside sleeve management.")
        print("  → Sleeve-defaults-to-ON change is SAFE for NGS (nothing to flip).")
    else:
        for hit in sleeves:
            s = hit["sleeve"]
            print(f"\n  path: {hit['path']}")
            print(f"  id: {s.get('id')}   name: {s.get('name')}")
            print(f"  state: {s.get('state')}   live_order_id: {s.get('live_order_id')}")
            print(f"  qty: {s.get('qty')}   buy_px: {s.get('buy_px')}   sell_px: {s.get('sell_px')}")
            print()
            print("  SAFETY FLAGS (explicit in JSON):")
            safety_flags = [
                "stop_loss_enabled", "crash_guard_enabled", "reversal_enabled",
                "velocity_gate_enabled", "accumulate_enabled",
                "avg_down_alert_enabled", "entry_quality_alert_enabled",
            ]
            for f in safety_flags:
                if f in s:
                    print(f"    {f:35s} = {s[f]}   (explicit)")
                else:
                    print(f"    {f:35s} = <MISSING — will use default>")
            print()
            print("  ⚠️ AFTER defaults flip:")
            for f in safety_flags:
                if f not in s:
                    print(f"    {f}: currently False (default) → will become True")

    # 2) Also print current broker position for context
    try:
        from broker import CoinbaseBroker  # noqa: F401
        # We won't actually call live broker here — too heavy for a diag.
        # Just report what we know from state.
    except Exception:
        pass

    # 3) Grep recent trade log for NGS mentions
    print()
    print("=" * 70)
    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        events = list(log.events())[-2000:]
        hits = [e for e in events if product in str(e) or "NGS" in str(e)]
        print(f"RECENT NGS ACTIVITY (last 2000 events): {len(hits)} mention NGS")
        for e in hits[-10:]:
            et = e.get("event_type", "?")
            ts = e.get("ts", "?")
            reason = e.get("reason") or e.get("error") or ""
            print(f"  {ts}  {et}  {str(reason)[:70]}")
    except Exception as e:
        print(f"  could not read trade log: {e}")


if __name__ == "__main__":
    main()
