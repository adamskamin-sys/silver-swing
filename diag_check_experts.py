"""Check the current expert-derived recommendation for any product.

Answers: "if the experts re-evaluated right now, what would they say
the buy_px should be, and how does that compare to what the sleeve
actually has saved?"

Runs the same 7-expert stack (Kaufman regime, Ehlers cycle, Elder Triple
Screen, Connors mean-reversion, VPIN calm, Chan OU band, Vince optimal-f)
that `experts_reentry.compute_reentry` uses — the same function called
after every sell — but against current market conditions and the current
saved sleeve state.

If the experts' recommendation matches what's saved on the sleeve → the
sleeve's levels are still optimal per the experts. If they DIFFER
significantly → the sleeve is stale and needs re-tiling (or the
auto-refresh feature we're building).

Usage (Render silver-swing-bot-live shell):
    python3 diag_check_experts.py XLP-20DEC30-CDE
    python3 diag_check_experts.py ZEC-20DEC30-CDE
    python3 diag_check_experts.py HYP-20DEC30-CDE

Read-only. Does not modify any state.
"""
from __future__ import annotations
import os
import sys
import time

from state_store import make_store


TENANT = "adam-live"


def _fetch_recent_prices(product_id: str, target_bars: int = 100) -> list[float]:
    """Fetch enough closes for the expert chain to run (>= 40 bars).
    Strategy: try progressively larger 5-min windows so low-liquidity
    futures like XLP-20DEC30-CDE get enough history to bypass the
    experts_reentry.py min_history_bars=40 fallback.

    Attempts, in order:
      1. Last 8 hours of 5-min bars    (~96 bars)
      2. Last 24 hours of 5-min bars   (~288 bars)
      3. Last 3 days of 1-hour bars    (~72 bars)
    """
    from broker import CoinbaseBroker, BrokerConfig
    broker = CoinbaseBroker(BrokerConfig(product_id=product_id))

    attempts = [
        ("FIVE_MINUTE",   8 * 3600,   "last 8h @ 5m"),
        ("FIVE_MINUTE",   24 * 3600,  "last 24h @ 5m"),
        ("ONE_HOUR",      3 * 86400,  "last 3d @ 1h"),
    ]

    for granularity, span_secs, label in attempts:
        try:
            end = int(time.time())
            start = end - span_secs
            resp = broker.client.get_candles(
                product_id=product_id,
                start=str(start),
                end=str(end),
                granularity=granularity,
            )
            candles = getattr(resp, "candles", None) or resp.get("candles", [])
            closes = []
            for c in candles:
                close = c.get("close") if isinstance(c, dict) else getattr(c, "close", None)
                if close is not None:
                    try:
                        closes.append(float(close))
                    except (TypeError, ValueError):
                        pass
            # Coinbase returns newest-first — reverse to oldest→newest
            closes.reverse()
            print(f"  Attempt: {label} → {len(closes)} valid closes")
            if len(closes) >= 40:
                return closes
        except Exception as e:
            print(f"  Attempt: {label} → FAILED: {type(e).__name__}: {e}")

    print("  All fetch attempts insufficient — returning whatever we got last")
    return closes if 'closes' in locals() else []


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python3 diag_check_experts.py <PRODUCT_ID>")
        print("  e.g., python3 diag_check_experts.py XLP-20DEC30-CDE")
        sys.exit(1)
    product_id = sys.argv[1].upper()

    store = make_store(os.getenv("SWING_DATA_DIR", "data"))

    # ---- 1) Current sleeve state
    print("=" * 70)
    print(f"CURRENT SLEEVE STATE ({TENANT}/{product_id}):")
    print("=" * 70)
    cfg = store.get_config(TENANT, product_id) or {}
    state = store.get_state(TENANT, product_id) or {}
    sleeves_cfg = cfg.get("sleeves") or []
    sleeves_st = state.get("sleeves") or {}

    if not sleeves_cfg:
        print(f"  NO SLEEVES configured on {product_id}. Nothing to check.")
        sys.exit(0)

    for sc in sleeves_cfg:
        sid = sc.get("id")
        ss = sleeves_st.get(sid, {})
        st = str(ss.get("state", "?")).upper()
        print(f"  Sleeve {sid} ({sc.get('name', '?')})")
        print(f"    state        = {st}")
        print(f"    buy_px       = {sc.get('buy_px', '?')}")
        print(f"    sell_px      = {sc.get('sell_px', '?')}")
        print(f"    stop_loss_px = {sc.get('stop_loss_px', '?')}")
        print(f"    qty          = {sc.get('qty', '?')}")
        print(f"    live_order   = {ss.get('live_order_id') or '(none)'}")
        armed_ts = ss.get("armed_buy_since_ts")
        if armed_ts:
            age_h = (time.time() - float(armed_ts)) / 3600
            print(f"    armed_since  = {age_h:.1f} hours ago")

    print()

    # ---- 2) Fetch recent price history
    print("=" * 70)
    print(f"FETCHING RECENT PRICES for {product_id}...")
    print("=" * 70)
    prices = _fetch_recent_prices(product_id, n=120)
    if not prices:
        print("  Could not fetch prices. Cannot run expert chain.")
        sys.exit(1)
    print(f"  Fetched {len(prices)} 1-min closes")
    print(f"  Range: {min(prices):.6f} → {max(prices):.6f}")
    print(f"  Current: {prices[-1]:.6f}")
    print()

    # ---- 3) Run expert chain for each sleeve
    print("=" * 70)
    print("EXPERT CHAIN OUTPUT (per sleeve):")
    print("=" * 70)
    import experts_reentry as _er

    for sc in sleeves_cfg:
        sid = sc.get("id")
        ss = sleeves_st.get(sid, {})
        spread = float(sc.get("sell_px", 0)) - float(sc.get("buy_px", 0))
        if spread <= 0:
            print(f"  Sleeve {sid}: spread={spread:.6f} — skipping (invalid)")
            continue
        # Use last_sell_fill_price if available, else current buy_px as reference
        sold_ref = float(ss.get("last_sell_fill_price") or sc.get("buy_px") or prices[-1])
        strategy_qty = int(sc.get("qty", 1))

        print(f"\n--- Sleeve {sid} ({sc.get('name', '?')}) ---")
        print(f"  Inputs: sold_ref={sold_ref:.6f}, spread={spread:.6f}, qty={strategy_qty}")
        print(f"  Current SAVED buy_px = {sc.get('buy_px', '?')}")
        print(f"  Current SAVED sell_px = {sc.get('sell_px', '?')}")

        try:
            decision = _er.compute_reentry(
                prices=prices,
                sold_price=sold_ref,
                spread=spread,
                strategy_qty=strategy_qty,
                account_equity=0.0,  # disables Vince cap for this check
                worst_loss_per_contract=0.0,
                recent_cycle_pnls=ss.get("recent_cycle_pnls") or [],
                ms=None,  # no VPIN data in this diag context
            )
        except Exception as e:
            print(f"  EXPERT CHAIN ERROR: {type(e).__name__}: {e}")
            continue

        # Show the recommendation
        print(f"\n  EXPERT RECOMMENDATION:")
        print(f"    should_arm     = {decision.get('should_arm')}")
        print(f"    recommended buy_px  = {decision.get('buy_px')}")
        print(f"    recommended sell_px = {decision.get('sell_px')}")
        print(f"    recommended qty     = {decision.get('qty')}")

        # Drift from saved
        saved_buy = float(sc.get("buy_px") or 0)
        rec_buy = float(decision.get("buy_px") or 0)
        if saved_buy > 0 and rec_buy > 0:
            drift = rec_buy - saved_buy
            drift_pct = (drift / saved_buy) * 100
            print(f"\n  DRIFT FROM SAVED:")
            print(f"    saved buy_px       = {saved_buy:.6f}")
            print(f"    recommended buy_px = {rec_buy:.6f}")
            print(f"    drift              = {drift:+.6f} ({drift_pct:+.3f}%)")
            if abs(drift_pct) < 0.5:
                print(f"    → verdict: MATCHES (drift <0.5% — sleeve is still expert-current)")
            elif abs(drift_pct) < 2.0:
                print(f"    → verdict: MINOR drift (0.5-2%) — sleeve slightly stale")
            else:
                print(f"    → verdict: SIGNIFICANT DRIFT (>2%) — sleeve is STALE, needs re-tile")

        # Reasons + snapshot
        print(f"\n  REASONS: {decision.get('reasons')}")
        snap = decision.get("expert_snapshot", {})
        print(f"\n  EXPERT VOTES (snapshot):")
        for expert, data in snap.items():
            if expert == "thresholds_used":
                continue  # skip config dump
            if isinstance(data, dict):
                # Print just the headline fields
                summary = {}
                for k in ("regime", "cycle_phase", "in_bounce_zone", "buy_ok",
                         "blocked_by", "band_center", "std", "suggested_buy_px",
                         "error"):
                    if k in data:
                        summary[k] = data[k]
                print(f"    {expert}: {summary}")
            else:
                print(f"    {expert}: {data}")


if __name__ == "__main__":
    main()
