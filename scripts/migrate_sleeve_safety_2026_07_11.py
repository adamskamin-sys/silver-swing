"""One-shot migration — walk every sleeve in the store and update to the
2026-07-11 safety defaults (Models B/C/D/E preset):

  stop_loss_enabled                  → True   (was likely False)
  stop_loss_px                       → max(0.01, buy_px − 1.5) if 0
  stop_loss_reanchor_on_trigger      → False  (safe re-entry via Path B)
  stop_loss_protect_realized_enabled → True
  stop_loss_protect_realized_frac    → 0.5
  entry_trend_filter_enabled         → True
  entry_trend_sma_window             → 20     (only set if missing)
  reentry_mode                       → 'volatility' (if 'off' or missing)
  microstructure_gate_enabled        → True
  stop_loss_ratchet_enabled          → True (if missing)
  stop_loss_ratchet_distance         → 1.5 (if missing)
  stop_loss_ratchet_activation       → 0.5 (if missing)
  stop_loss_max_consecutive          → 3 (if 0 or missing)

Sleeves named 'Custom' are skipped so hand-tuned setups keep their values.

Reads REDIS_URL from env — same code path the bot uses — so this works
against local JSON storage OR Render's Redis (Valkey). Defaults to DRY RUN;
pass --apply to actually write.

Usage:
  # Local (uses ./data/store.json):
  python3 scripts/migrate_sleeve_safety_2026_07_11.py
  python3 scripts/migrate_sleeve_safety_2026_07_11.py --apply

  # Render (against the deployed Redis):
  #   Set REDIS_URL from the Render dashboard, then run in a shell/one-off:
  REDIS_URL=redis://... python3 scripts/migrate_sleeve_safety_2026_07_11.py
  REDIS_URL=redis://... python3 scripts/migrate_sleeve_safety_2026_07_11.py --apply

  # Scope to one tenant (paper-only test run):
  python3 scripts/migrate_sleeve_safety_2026_07_11.py --tenant adam-paper --apply
"""

import argparse
import os
import sys

# Repo root on sys.path so `import state_store` works when run from scripts/.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from state_store import make_store  # noqa: E402


NEW_DEFAULTS = {
    "stop_loss_enabled": True,
    "stop_loss_reanchor_on_trigger": False,
    "stop_loss_protect_realized_enabled": True,
    "stop_loss_protect_realized_frac": 0.5,
    "entry_trend_filter_enabled": True,
    "microstructure_gate_enabled": True,
    "reentry_mode": "volatility",
    # Flavor 3: after a trail-based sell, wait for volatility to contract
    # THEN a new high before re-arming buys. Sequential Kaufman-then-Turtle.
    "post_trail_reentry_mode": "sequential",
}

# Fields we only fill in when missing / zero — we don't overwrite user tuning.
FILL_IF_ABSENT = {
    "stop_loss_ratchet_enabled": True,
    "stop_loss_ratchet_distance": 1.5,
    "stop_loss_ratchet_activation": 0.5,
    "stop_loss_max_consecutive": 3,
    "entry_trend_sma_window": 20,
    "reentry_range_contraction": 0.5,
    "reentry_min_wait_secs": 30.0,
    "reentry_range_window": 60,
    "post_trail_stage_b_max_wait_secs": 3600.0,
}


def migrate_sleeve(sleeve: dict, position_avg_entry: float = 0.0) -> tuple[dict, list[str]]:
    """Return (updated_sleeve, list_of_changes).

    position_avg_entry: the actual cost basis of the underlying position
    (from snapshot.position_avg_entry). Used as the anchor for stop_loss_px
    so the stop is 5% below what you actually paid, not 5% below the
    sleeve's next-buy target. Pass 0 if unknown — falls back to sleeve.buy_px.
    """
    changes = []
    updated = dict(sleeve)  # shallow copy — sleeve dicts are flat

    # Skip Custom sleeves — user hand-tunes those.
    if str(sleeve.get("name", "")).lower().startswith("custom"):
        return updated, ["skipped (Custom sleeve)"]

    # Unconditional flips.
    for k, v in NEW_DEFAULTS.items():
        if updated.get(k) != v:
            changes.append(f"{k}: {updated.get(k)!r} → {v!r}")
            updated[k] = v

    # Fill missing / zero-ish.
    for k, v in FILL_IF_ABSENT.items():
        cur = updated.get(k)
        if cur is None or cur == 0 or cur == 0.0:
            changes.append(f"{k}: {cur!r} → {v!r}")
            updated[k] = v

    # stop_loss_px must be > 0 AND < buy_px for the validator. Rewrite it to
    # 5% below buy_px so the stop scales to the product's price (silver at
    # $60 gets $3 distance, XLP at $0.19 gets $0.0095, PLAT at $1640 gets
    # $82). Prior versions of this migration used a fixed $1.50 distance
    # from silver, which put the NGS stop at $1.52 — 50% below its $3 buy —
    # and the PLAT stop 0.09% below its $1640 buy. Neither scales sanely.
    #
    # Overwrite even when the current value is > 0 and < buy_px: the earlier
    # migration produced valid-but-wrong values (silver-scale, applied to
    # every product), and those need to be normalized. A user's hand-tuned
    # values would only survive if this script is re-run in error — flip
    # to "only fill when 0" if that becomes a concern.
    buy_px = float(updated.get("buy_px") or 0.0)
    cur_sl_px = float(updated.get("stop_loss_px") or 0.0)
    # Anchor on actual position avg entry when we have it — that's what the
    # user actually paid. Fall back to sleeve.buy_px (the next-buy target)
    # only when the position hasn't been captured yet.
    anchor_px = float(position_avg_entry) if position_avg_entry > 0 else buy_px
    if anchor_px > 0:
        decimals = 4 if anchor_px < 1 else 2
        new_sl_px = round(anchor_px * 0.95, decimals)
        # Safety: never below 0.0001 (would fail the > 0 validator).
        new_sl_px = max(0.0001, new_sl_px)
        # But never above buy_px either — validator enforces stop < buy.
        if buy_px > 0 and new_sl_px >= buy_px:
            new_sl_px = round(buy_px * 0.99, decimals)
        if abs(new_sl_px - cur_sl_px) > 10 ** (-decimals):
            anchor_note = "pos_avg" if position_avg_entry > 0 else "buy_px"
            changes.append(f"stop_loss_px: {cur_sl_px!r} → {new_sl_px!r} (5% below {anchor_note} {anchor_px})")
            updated["stop_loss_px"] = new_sl_px

    return updated, changes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="Actually write changes. Default is dry-run.")
    ap.add_argument("--tenant", help="Only migrate this tenant (e.g., adam-live).")
    ap.add_argument("--data-dir", default="./data",
                    help="Local data dir (used when REDIS_URL is not set).")
    args = ap.parse_args()

    store = make_store(args.data_dir)
    backend = "Redis" if os.getenv("REDIS_URL") else f"JSON file ({args.data_dir}/store.json)"
    print(f"Backend: {backend}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")
    print()

    tenants = store.list_tenants()
    if args.tenant:
        tenants = [t for t in tenants if t == args.tenant]
    if not tenants:
        print(f"No tenants found matching {args.tenant!r}." if args.tenant
              else "No tenants in store.")
        return 1

    total_sleeves = 0
    total_updated = 0
    for tenant in tenants:
        symbols = store.list_symbols(tenant)
        for symbol in symbols:
            if symbol.startswith("__"):
                continue  # skip __portfolio__, __account_kill_switch__, etc.
            cfg = store.get_config(tenant, symbol)
            if not cfg:
                continue
            sleeves = cfg.get("sleeves") or []
            if not sleeves:
                continue
            # Try to fetch actual position cost basis for this product so the
            # stop_loss anchor is what the user paid, not the sleeve's next-buy
            # target. Per-product snapshot first; fall back to the tenant-level
            # __portfolio__.derivatives list (populated by _sync_live_portfolio).
            snap = store.get_snapshot(tenant, symbol) or {}
            pos_avg = float(snap.get("position_avg_entry") or 0.0)
            pos_qty = int(snap.get("position_qty") or 0)
            if pos_avg <= 0:
                pf = store.get_config(tenant, "__portfolio__") or {}
                for d in (pf.get("derivatives") or []):
                    if d.get("product_id") == symbol:
                        pos_avg = float(d.get("avg_entry") or 0.0)
                        pos_qty = pos_qty or abs(int(d.get("qty") or 0))
                        break
            # Only pass avg_entry if we're actually IN the position. If flat,
            # migrate_sleeve falls back to sleeve.buy_px (waiting-to-buy anchor).
            anchor_avg = pos_avg if pos_qty > 0 else 0.0
            new_sleeves = []
            symbol_changed = False
            for s in sleeves:
                total_sleeves += 1
                updated, changes = migrate_sleeve(s, position_avg_entry=anchor_avg)
                if changes and changes != ["skipped (Custom sleeve)"]:
                    total_updated += 1
                    print(f"[{tenant}/{symbol}] {s.get('name', s.get('id'))}:")
                    for c in changes:
                        print(f"    {c}")
                    symbol_changed = True
                new_sleeves.append(updated)
            if symbol_changed and args.apply:
                cfg["sleeves"] = new_sleeves
                store.put_config(tenant, symbol, cfg)
                print(f"    → written to {tenant}/{symbol}")

    print()
    print(f"Total sleeves scanned: {total_sleeves}")
    print(f"Sleeves needing changes: {total_updated}")
    if not args.apply and total_updated > 0:
        print()
        print("DRY RUN — no changes written. Re-run with --apply to commit.")
    elif args.apply and total_updated > 0:
        print()
        print("APPLIED. Bot picks up new config on next tick (no restart needed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
