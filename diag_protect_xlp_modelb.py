"""Enable expert-derived stop-loss protection for XLP Model B sleeve.

Adam 2026-07-19: diag_trail_below_entry revealed XLP sleeve
`smrsguvi7` ("Model B — Defensive plus...") is holding 1 contract
with stop_loss_enabled=False + no live resting stop. §3.6 violation.

Fix routes through expert_stop consensus per §3.15 (Adam 2026-07-19
amend): stop distance = median(Wilder 2N, CJP OFI-widened, Kyle λ),
floored at Menkveld 3× fees, capped at Van Tharp 10% 1R. Uses XLP's
own historical ATR fetched from Coinbase — no hardcoded percentage.

Also inspects (report only, no change): XLP sleeve `scan-mrqn4az1`
and ZEC sleeve `scan-mrqhf1qs` — both have stop_loss_enabled=False
but live stops from prior config. Your call whether to enable via
config, cancel the ghost, or leave.

Read-only by default. Usage:

    python3 diag_protect_xlp_modelb.py                   # dry-run
    python3 diag_protect_xlp_modelb.py --apply           # persist
"""
from __future__ import annotations
import os
import sys
import time


TENANT = "adam-live"
TARGET_SYMBOL = "XLP-20DEC30-CDE"
TARGET_SLEEVE_ID = "smrsguvi7"


def _q(v, d=0.0):
    try: return float(v) if v is not None else d
    except (TypeError, ValueError): return d


def _fetch_closes(broker, product_id: str) -> list[float]:
    """Fetch historical closes for ATR estimation. Same fallback ladder
    as diag_check_experts.py — try tight window first, widen on shortfall."""
    attempts = [
        ("FIVE_MINUTE", 8 * 3600, "last 8h @ 5m"),
        ("FIVE_MINUTE", 24 * 3600, "last 24h @ 5m"),
        ("ONE_HOUR", 3 * 86400, "last 3d @ 1h"),
    ]
    for granularity, span, label in attempts:
        try:
            end = int(time.time())
            start = end - span
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
                    try: closes.append(float(close))
                    except (TypeError, ValueError): pass
            closes.reverse()  # Coinbase returns newest-first
            print(f"  candle fetch: {label} → {len(closes)} closes")
            if len(closes) >= 20:
                return closes
        except Exception as e:
            print(f"  candle fetch: {label} → FAILED: {type(e).__name__}: {e}")
    return []


def main() -> None:
    apply = "--apply" in sys.argv
    print("=" * 78)
    print(f"PROTECT XLP MODEL B {'(APPLY)' if apply else '(dry-run)'} — "
          f"expert-consensus stop-loss")
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

    target_cfg = None
    target_idx = None
    for i, scfg in enumerate(sleeves_cfg):
        if isinstance(scfg, dict) and str(scfg.get("id")) == TARGET_SLEEVE_ID:
            target_cfg = scfg
            target_idx = i
            break
    if target_cfg is None:
        print(f"\n✗ Sleeve {TARGET_SLEEVE_ID} not found — aborting.")
        return

    target_state = sleeves_state.get(TARGET_SLEEVE_ID) or {}
    own_avg = _q(target_state.get("own_avg_entry"))
    if own_avg <= 0:
        print(f"\n✗ Sleeve {TARGET_SLEEVE_ID} not holding — no protection needed.")
        return

    qty = max(1, int(_q(target_state.get("qty"), 1) or 1))
    fee_rt = _q(config.get("fee_per_contract_roundtrip"))
    tick_size = _q(config.get("tick_size"))

    # Broker + contract_size (§3.14 source of truth)
    from broker import BrokerConfig, CoinbaseBroker
    b = CoinbaseBroker(BrokerConfig(product_id=TARGET_SYMBOL))
    spec = b.contract_spec() or {}
    contract_size = _q(spec.get("contract_size"))
    if contract_size <= 0:
        print(f"\n✗ contract_size unavailable — aborting.")
        return

    # ATR from XLP's own historical closes
    print(f"\nFetching XLP historical closes for ATR estimation:")
    closes = _fetch_closes(b, TARGET_SYMBOL)
    if len(closes) < 3:
        print(f"\n✗ Not enough close history to compute ATR — aborting.")
        return
    deltas = [abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    recent = deltas[-14:]
    atr_est = sum(recent) / len(recent)
    print(f"  ATR-14 estimate: ${atr_est:.8f}")
    if atr_est <= 0:
        print(f"\n✗ ATR estimate is 0 — flat price history — aborting.")
        return

    # Expert consensus stop
    import expert_stop
    if getattr(expert_stop, "MODE", "expert") != "expert":
        print(f"\n✗ expert_stop.MODE != 'expert' — kill switch flipped, aborting.")
        return

    decision = expert_stop.optimal_stop_distance(
        mark=float(own_avg),
        atr_est=float(atr_est),
        fee_per_roundtrip=float(fee_rt),
        contract_size=float(contract_size),
        qty=qty,
        wilder_multiplier=2.0,
        tick_size=(tick_size if tick_size > 0 else None),
    )
    if decision is None:
        print(f"\n✗ expert_stop returned None (bad inputs) — aborting.")
        return

    current_sl_enabled = bool(target_cfg.get("stop_loss_enabled", False))
    current_sl_px = _q(target_cfg.get("stop_loss_px"))
    proposed_sl_px = float(decision.stop_px)

    print(f"\nTARGET SLEEVE: {TARGET_SLEEVE_ID} "
          f"(name: {target_cfg.get('name')})")
    print(f"  own_avg_entry:       ${own_avg:.6f}")
    print(f"  qty:                 {qty}")
    print(f"  contract_size:       {contract_size}")
    print(f"  fee_per_rt:          ${fee_rt}")

    print(f"\nEXPERT CONSENSUS (§3.15):")
    print(f"  method:              {decision.method}")
    print(f"  candidates:          {decision.candidates}")
    print(f"  consensus distance:  ${decision.consensus:.8f}")
    print(f"  Menkveld fee floor:  ${decision.fee_floor:.8f} "
          f"(binding: {decision.fee_floor_binding})")
    print(f"  Van Tharp cap:       ${decision.sanity_cap:.8f} "
          f"(binding: {decision.sanity_cap_binding})")
    print(f"  final distance:      ${decision.stop_distance:.8f}")
    print(f"  → stop_px:           ${proposed_sl_px:.6f}")
    print(f"  citation:            {decision.citation}")

    print(f"\nCONFIG DELTA (proposed):")
    print(f"  stop_loss_enabled:   {current_sl_enabled} → True")
    print(f"  stop_loss_px:        ${current_sl_px:.6f} → ${proposed_sl_px:.6f}")

    print(f"\nAfter apply, next tick will:")
    print(f"  1. Read updated config from Redis (reload-on-tick)")
    print(f"  2. _maintain_resting_stop enters stop_loss_px branch")
    print(f"  3. Places stop-limit at ${proposed_sl_px:.6f} on Coinbase")
    print(f"  4. Records resting_stop_oid + stage=hard_bottom")

    # Report-only inspection of the two ghost cases
    print(f"\n{'-' * 78}")
    print(f"NOT TOUCHED — for your review:")
    print(f"{'-' * 78}")
    for sym, entry_sid in [
        (TARGET_SYMBOL, "scan-mrqn4az1"),
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
        print(f"    ghost from prior config. Bot won't refresh (config disabled). "
              f"To fix via expert consensus, re-run this diag targeting that "
              f"sleeve.")

    if not apply:
        print(f"\n(dry-run — pass --apply to persist the Model B fix)")
        return

    # Apply — modify config in place, persist
    target_cfg["stop_loss_enabled"] = True
    target_cfg["stop_loss_px"] = round(proposed_sl_px, 6)
    sleeves_cfg[target_idx] = target_cfg
    config["sleeves"] = sleeves_cfg

    if hasattr(store, "put_config"):
        store.put_config(TENANT, TARGET_SYMBOL, config)
    else:
        raw[TENANT][TARGET_SYMBOL]["config"] = config
        if hasattr(store, "_save"): store._save(raw)

    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        log.record(
            "protect_xlp_modelb_applied",
            tenant=TENANT, symbol=TARGET_SYMBOL, sleeve_id=TARGET_SLEEVE_ID,
            sleeve_name=target_cfg.get("name"),
            own_avg=own_avg, atr_est=round(atr_est, 8),
            stop_loss_enabled_before=current_sl_enabled,
            stop_loss_px_before=round(current_sl_px, 6),
            stop_loss_enabled_after=True,
            stop_loss_px_after=round(proposed_sl_px, 6),
            expert_method=decision.method,
            expert_citation=decision.citation,
            expert_candidates=decision.candidates,
            fee_floor_binding=decision.fee_floor_binding,
            sanity_cap_binding=decision.sanity_cap_binding,
            reason=("§3.6 violation fix routed through §3.15 expert consensus."),
            severity="warn",
        )
    except Exception as e:
        print(f"\n(note: trade log record failed: {e})")

    print(f"\n✓ APPLIED. Config saved.")
    print(f"  Bot places resting stop at ${proposed_sl_px:.6f} on next tick.")
    print(f"  Verify: python3 diag_trail_below_entry.py")


if __name__ == "__main__":
    main()
