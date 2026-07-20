"""Enable stop-loss protection for XLP Model B sleeve (§3.6 violation fix).

Adam 2026-07-19: diag_trail_below_entry revealed XLP sleeve
`smrsguvi7` ("Model B — Defensive plus...") is holding 1 contract at
own_avg $0.18892 with:
  - stop_loss_enabled: False
  - stop_loss_px: $0.000000
  - resting_stop_oid: — (no live stop on Coinbase)

That's an unprotected held position — §3.6 hard invariant violation
(resting ratchet-stop must NEVER leave a held position unprotected).
The sleeve name literally says "Defensive plus" — stop-loss disabled
was almost certainly a config error, not intent.

Proposed correction (config change only — no order placement here;
the bot's next tick will pick it up and place the stop):
  - stop_loss_enabled: False → True
  - stop_loss_px: $0.00000 → $0.17948 (own_avg × 0.95, 5% floor)

Also inspects (report only, no change): XLP sleeve `scan-mrqn4az1`
and ZEC sleeve `scan-mrqhf1qs` — both have stop_loss_enabled=False
but live stops from prior config. Adam's call whether to (a) enable
config so the bot maintains those stops, (b) leave as ghost, or (c)
cancel and go unprotected.

Read-only by default. Usage:

    python3 diag_protect_xlp_modelb.py                   # dry-run
    python3 diag_protect_xlp_modelb.py --apply           # persist
"""
from __future__ import annotations
import os
import sys


TENANT = "adam-live"
TARGET_SYMBOL = "XLP-20DEC30-CDE"
TARGET_SLEEVE_ID = "smrsguvi7"
STOP_LOSS_FRACTION = 0.95  # 5% below own_avg


def _q(v, d=0.0):
    try: return float(v) if v is not None else d
    except (TypeError, ValueError): return d


def main() -> None:
    apply = "--apply" in sys.argv
    print("=" * 78)
    print(f"PROTECT XLP MODEL B {'(APPLY)' if apply else '(dry-run)'}")
    print("=" * 78)

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))
    raw = store._load()

    tdata = raw.get(TENANT) or {}
    entry = tdata.get(TARGET_SYMBOL) or {}
    config = entry.get("config") or {}
    state = entry.get("state") or {}
    sleeves_cfg = config.get("sleeves") or []
    sleeves_state = state.get("sleeves") or {}

    # Find the target sleeve config
    target_cfg = None
    target_idx = None
    for i, scfg in enumerate(sleeves_cfg):
        if isinstance(scfg, dict) and str(scfg.get("id")) == TARGET_SLEEVE_ID:
            target_cfg = scfg
            target_idx = i
            break

    if target_cfg is None:
        print(f"\n✗ Sleeve {TARGET_SLEEVE_ID} not found in "
              f"{TENANT}/{TARGET_SYMBOL} config.sleeves — aborting.")
        return

    target_state = sleeves_state.get(TARGET_SLEEVE_ID) or {}
    own_avg = _q(target_state.get("own_avg_entry"))
    if own_avg <= 0:
        print(f"\n✗ Sleeve {TARGET_SLEEVE_ID} has no own_avg_entry "
              f"(not holding) — no protection needed.")
        return

    current_sl_enabled = bool(target_cfg.get("stop_loss_enabled", False))
    current_sl_px = _q(target_cfg.get("stop_loss_px"))
    proposed_sl_px = round(own_avg * STOP_LOSS_FRACTION, 6)

    print(f"\nTARGET SLEEVE: {TARGET_SLEEVE_ID} "
          f"(name: {target_cfg.get('name')})")
    print(f"  own_avg_entry:       ${own_avg:.6f}")
    print(f"  qty:                 {target_state.get('qty', 1)}")
    print(f"  resting_stop_oid:    {target_state.get('resting_stop_oid', '—')}")
    print(f"  resting_stop_px:     ${_q(target_state.get('resting_stop_px')):.6f}")

    print(f"\nCONFIG DELTA (proposed):")
    print(f"  stop_loss_enabled:   {current_sl_enabled} → True")
    print(f"  stop_loss_px:        ${current_sl_px:.6f} → ${proposed_sl_px:.6f}")
    print(f"                       (own_avg × {STOP_LOSS_FRACTION} = 5% floor)")

    print(f"\nAfter apply, the bot's next tick will:")
    print(f"  1. Read updated config from Redis (reload-on-tick pattern)")
    print(f"  2. Enter _maintain_resting_stop for this sleeve")
    print(f"  3. Place a stop-limit at ${proposed_sl_px:.6f} on Coinbase")
    print(f"  4. Track it as resting_stop_oid + stage=hard_bottom")

    # Also inspect (READ ONLY) the other ghost-stop cases
    print(f"\n{'-' * 78}")
    print(f"NOT TOUCHED — for your review:")
    print(f"{'-' * 78}")

    for sym, entry_sid in [
        (TARGET_SYMBOL, "scan-mrqn4az1"),  # XLP sleeve1
        ("ZEC-20DEC30-CDE", "scan-mrqhf1qs"),
    ]:
        te = raw.get(TENANT, {}).get(sym, {})
        cfgs = (te.get("config") or {}).get("sleeves") or []
        sts = (te.get("state") or {}).get("sleeves") or {}
        scfg_i = next((c for c in cfgs
                       if isinstance(c, dict) and str(c.get("id")) == entry_sid), None)
        sst = sts.get(entry_sid) or {}
        if not scfg_i:
            continue
        print(f"\n  {sym} / {entry_sid} ({scfg_i.get('name')}):")
        print(f"    own_avg:              ${_q(sst.get('own_avg_entry')):.6f}")
        print(f"    config.stop_loss:     enabled={scfg_i.get('stop_loss_enabled')} "
              f"px=${_q(scfg_i.get('stop_loss_px')):.6f}")
        print(f"    live resting_stop:    oid={sst.get('resting_stop_oid')} "
              f"px=${_q(sst.get('resting_stop_px')):.6f} "
              f"stage={sst.get('resting_stop_stage')}")
        print(f"    Situation: ghost stop from prior config. Bot won't refresh "
              f"it (config says disabled). Your call: enable in config, cancel "
              f"the ghost, or leave.")

    if not apply:
        print(f"\n(dry-run — pass --apply to persist the Model B fix)")
        return

    # Apply — modify config in place
    target_cfg["stop_loss_enabled"] = True
    target_cfg["stop_loss_px"] = proposed_sl_px
    sleeves_cfg[target_idx] = target_cfg
    config["sleeves"] = sleeves_cfg

    # Persist via the same path the bot uses
    if hasattr(store, "put_config"):
        store.put_config(TENANT, TARGET_SYMBOL, config)
    else:
        # Fallback: raw write (should not happen with make_store)
        raw[TENANT][TARGET_SYMBOL]["config"] = config
        store._save(raw) if hasattr(store, "_save") else None

    # Audit log
    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        log.record(
            "protect_xlp_modelb_applied",
            tenant=TENANT, symbol=TARGET_SYMBOL, sleeve_id=TARGET_SLEEVE_ID,
            sleeve_name=target_cfg.get("name"),
            own_avg=own_avg,
            stop_loss_enabled_before=current_sl_enabled,
            stop_loss_px_before=round(current_sl_px, 6),
            stop_loss_enabled_after=True,
            stop_loss_px_after=proposed_sl_px,
            reason=("§3.6 violation fix: sleeve was holding without any "
                    "resting stop and stop_loss_enabled was False. Enabling "
                    "with 5% protective floor. Sleeve name 'Defensive plus' "
                    "confirms this was likely a config error."),
            severity="warn",
        )
    except Exception as e:
        print(f"\n(note: trade log record failed: {e})")

    print(f"\n✓ APPLIED. Config saved.")
    print(f"  Bot picks up on next tick — expect resting stop at "
          f"${proposed_sl_px:.6f} within 10s.")
    print(f"  Verify via: python3 diag_trail_below_entry.py")


if __name__ == "__main__":
    main()
