"""Read-only: how long has a sleeve been ARMED_SELL without a resting_stop_oid?

Adam 2026-07-19: XLP scan-mrqn4az1 holds own_avg_entry=0.18775 with no
resting stop protecting it. Rule feedback_ratchet_stop_never_gap says a
held position must ALWAYS have a stop; a gap is a blocker.

But: right after arming the position, there's a legitimate window between
'sleeve holds' and 'stop placed on Coinbase'. This diag measures that
window against the bot's heartbeat so we can tell:
  - stop is being placed now (normal, wait a tick)
  - stop failed to place N minutes ago (needs intervention)
  - stop-loss is disabled in cfg (by design)

Usage:  python3 diag_check_stop_gap.py XLP-20DEC30-CDE
"""
from __future__ import annotations
import os
import sys
import time


def _fmt_age(secs: float) -> str:
    if secs < 60:
        return f"{secs:.0f}s"
    if secs < 3600:
        return f"{secs/60:.1f}min"
    return f"{secs/3600:.2f}h"


def main() -> None:
    if len(sys.argv) < 2:
        print("USAGE: python3 diag_check_stop_gap.py <PRODUCT_ID>")
        return
    product_id = sys.argv[1]
    print("=" * 78)
    print(f"STOP-GAP CHECK — {product_id}")
    print("=" * 78)

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))
    raw = store._load()

    now = time.time()
    hits = 0
    findings: list[str] = []

    for tenant, tenant_data in raw.items():
        if not isinstance(tenant_data, dict):
            continue
        entry = tenant_data.get(product_id)
        if not isinstance(entry, dict):
            continue
        cfg = entry.get("config") or {}
        state = entry.get("state") or {}
        sleeves_cfg = {s.get("id"): s for s in (cfg.get("sleeves") or [])}
        sleeves_state = state.get("sleeves") or {}
        hb = state.get("last_heartbeat_ts")
        hb_age = (now - float(hb)) if hb else None

        print(f"\n--- tenant={tenant} ---")
        print(f"bot heartbeat age: {_fmt_age(hb_age) if hb_age is not None else 'unknown'}")

        for sid, ss in sleeves_state.items():
            hits += 1
            sc = sleeves_cfg.get(sid, {})
            st = ss.get("state")
            own_avg = ss.get("own_avg_entry")
            stop_oid = ss.get("resting_stop_oid")
            armed_ts = ss.get("armed_sell_since_ts") or ss.get("own_avg_entry_ts")
            armed_age = (now - float(armed_ts)) if armed_ts else None

            stop_cfg = sc.get("stopLoss") or sc.get("stop_loss") or {}
            stop_enabled = stop_cfg.get("enabled") if isinstance(stop_cfg, dict) else None

            print(f"\n  sid={sid}")
            print(f"    state={st}  own_avg={own_avg}  stop_oid={stop_oid}")
            print(f"    armed_sell_since={_fmt_age(armed_age) if armed_age is not None else 'unknown'}")
            print(f"    cfg stop_loss.enabled={stop_enabled}")

            # Verdict
            holds_position = (st == "ARMED_SELL") and own_avg is not None
            if not holds_position:
                print(f"    → OK (no position held)")
                continue
            if stop_oid:
                print(f"    → OK (stop_oid present)")
                continue
            if stop_enabled is False:
                print(f"    → OK (stop_loss disabled in cfg — by design)")
                continue

            # Held position, no stop, stop enabled (or unspecified).
            if armed_age is None:
                findings.append(f"{tenant}/{sid}: HELD w/o stop, armed_ts unknown")
                print(f"    ⚠ GAP: holds position, no resting stop, unknown arm time")
            elif armed_age < 30:
                print(f"    → PROBABLY OK (just armed {_fmt_age(armed_age)} ago — stop should place next tick)")
            elif armed_age < 120:
                print(f"    ⚠ WATCHING: armed {_fmt_age(armed_age)} ago, stop not placed — one more tick then escalate")
            else:
                findings.append(f"{tenant}/{sid}: HELD w/o stop for {_fmt_age(armed_age)} — blocker")
                print(f"    ✗ BLOCKER: held {_fmt_age(armed_age)} without resting stop (ratchet_stop_never_gap rule)")

    if hits == 0:
        print(f"\nNo {product_id} sleeves found.")
        return

    print("\n" + "=" * 78)
    if findings:
        print(f"⚠ {len(findings)} STOP-GAP FINDING(S):")
        for f in findings:
            print(f"  - {f}")
    else:
        print("✓ No stop-gap findings.")


if __name__ == "__main__":
    main()
