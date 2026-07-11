"""Read-only fee auditor. Walks every (tenant, symbol) config in the store,
prints stored `fee_per_contract_roundtrip` alongside a live Coinbase preview
of what BUY-side and SELL-side commissions would be right now, and flags any
config whose stored value is materially off from the live truth.

The Coinbase preview requires an ECDSA/ES256 API key. If auth fails
(e.g., running against a machine that only has an Ed25519 key), the script
falls back to Redis-only mode — still prints every stored value so you can
eyeball vs. observed fills manually.

Backend selection follows the bot: REDIS_URL env → Redis; otherwise local
JSON file at ./data/store.json.

Usage:
  # Local (paper store):
  python3 scripts/check_stored_fees.py

  # Render Redis (external URL from Render dashboard):
  REDIS_URL='redis://...' python3 scripts/check_stored_fees.py

  # Only show discrepancies above N percent:
  python3 scripts/check_stored_fees.py --drift-threshold 20

Exit code 0 always (this is a read-only audit; a bad fee isn't a shell error).
"""

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from state_store import make_store  # noqa: E402


def _try_coinbase_client():
    """Return an authenticated Coinbase RESTClient, or None if we can't auth
    from this machine. Never raises — the audit still works Redis-only."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
        from coinbase.rest import RESTClient
        key_path = os.getenv("COINBASE_API_KEY_JSON_PATH")
        if not key_path or not os.path.exists(key_path):
            return None
        # The SDK expects ECDSA/PEM. Try both signatures.
        try:
            return RESTClient(key_file=key_path)
        except Exception:
            pass
        with open(key_path) as f:
            kj = json.load(f)
        return RESTClient(api_key=kj.get("name"), api_secret=kj.get("privateKey"))
    except Exception:
        return None


def _preview_side(client, product_id: str, side: str) -> float | None:
    """Return commission_total for a 1-contract preview on `side`, or None
    if the call fails. Uses a far-away limit price so nothing can fill."""
    try:
        if side.upper() == "BUY":
            preview = client.preview_limit_order_gtc_buy(
                product_id=product_id, base_size="1", limit_price="0.001",
            )
        else:
            preview = client.preview_limit_order_gtc_sell(
                product_id=product_id, base_size="1", limit_price="99999999.99",
            )
        pd = preview.to_dict() if hasattr(preview, "to_dict") else preview
        v = pd.get("commission_total")
        return float(v) if v is not None else None
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--drift-threshold", type=float, default=15.0,
                    help="Flag configs whose stored fee drifts more than N%% from live. Default 15.")
    ap.add_argument("--tenant", help="Only audit this tenant.")
    ap.add_argument("--data-dir", default="./data")
    args = ap.parse_args()

    store = make_store(args.data_dir)
    backend = "Redis" if os.getenv("REDIS_URL") else f"JSON ({args.data_dir}/store.json)"
    client = _try_coinbase_client()
    print(f"Backend: {backend}")
    print(f"Live Coinbase preview: {'AVAILABLE' if client else 'UNAVAILABLE (Redis-only mode)'}")
    print(f"Drift threshold: {args.drift_threshold:.1f}%")
    print()

    tenants = store.list_tenants()
    if args.tenant:
        tenants = [t for t in tenants if t == args.tenant]
    if not tenants:
        print("No tenants." if not args.tenant else f"No tenants matching {args.tenant!r}.")
        return 0

    rows = []
    for tenant in tenants:
        for symbol in store.list_symbols(tenant):
            if symbol.startswith("__"):
                continue
            cfg = store.get_config(tenant, symbol) or {}
            stored_rt = cfg.get("fee_per_contract_roundtrip")
            stored_pfe = cfg.get("fee_per_fill_empirical")
            stored_buy = cfg.get("fee_per_fill_buy")
            stored_sell = cfg.get("fee_per_fill_sell")
            n_sleeves = len(cfg.get("sleeves") or [])
            live_buy = _preview_side(client, symbol, "BUY") if client else None
            live_sell = _preview_side(client, symbol, "SELL") if client else None
            live_rt = None
            if live_buy is not None and live_sell is not None:
                live_rt = round(live_buy + live_sell, 4)
            drift_pct = None
            if live_rt and stored_rt:
                drift_pct = abs(stored_rt - live_rt) / live_rt * 100.0
            rows.append({
                "tenant": tenant, "symbol": symbol, "sleeves": n_sleeves,
                "stored_rt": stored_rt, "stored_pfe": stored_pfe,
                "stored_buy": stored_buy, "stored_sell": stored_sell,
                "live_buy": live_buy, "live_sell": live_sell,
                "live_rt": live_rt, "drift_pct": drift_pct,
            })

    def _fmt_money(v):
        return f"${v:.4f}" if isinstance(v, (int, float)) else "—"

    print(f"{'TENANT/SYMBOL':40} {'SLV':>3} {'STORED-RT':>10} {'LIVE-BUY':>10} {'LIVE-SELL':>10} {'LIVE-RT':>10} {'DRIFT':>8}")
    print("-" * 100)
    flagged = []
    for r in rows:
        drift_s = f"{r['drift_pct']:.1f}%" if r["drift_pct"] is not None else "—"
        line = (f"{r['tenant']+'/'+r['symbol']:40} "
                f"{r['sleeves']:>3} "
                f"{_fmt_money(r['stored_rt']):>10} "
                f"{_fmt_money(r['live_buy']):>10} "
                f"{_fmt_money(r['live_sell']):>10} "
                f"{_fmt_money(r['live_rt']):>10} "
                f"{drift_s:>8}")
        if r["drift_pct"] is not None and r["drift_pct"] > args.drift_threshold:
            print("!!  " + line)
            flagged.append(r)
        else:
            print("    " + line)

    print()
    if flagged:
        print(f"⚠  {len(flagged)} product(s) exceed {args.drift_threshold:.1f}% drift.")
        print("Each flagged product's realized_pnl and target_net gates are using the wrong fee.")
        print("Fix: edit any attached sleeve in the dashboard to trigger a config re-save,")
        print("which forces main.py:_refresh_contract_spec_into_config to re-preview and update.")
    elif client:
        print("All stored fees match live Coinbase preview within threshold. ✓")
    else:
        print("Live preview not available — compare stored values against your Coinbase fills")
        print("(Portfolio → History) to catch any drift manually.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
