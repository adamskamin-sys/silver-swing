"""Phase B shadow vs Phase A actual — did the ratcheting bracket win?

For each completed cycle (sleeve_cycle_completed event), find the
matching Phase B shadow decision (phase_b_would_exit) for the same
sleeve BETWEEN the buy fill and the actual sell fill. Compare:

  Phase A actual exit_px     vs  Phase B hypothetical shadow_exit_px
  Phase A actual cycle_pnl   vs  Phase B hypothetical_pnl
  Peak reached during hold   (phase_b_hwm_ratchet events)

If Phase B hypothetical_pnl > Phase A cycle_pnl → shadow would
have exited BETTER (ratchet locked in more than the fixed LIMIT).
If < → shadow would have exited WORSE (fixed LIMIT captured
more than the trail).

Also prints:
  - Per-sleeve rollup: N cycles seen, wins/losses under Phase B
  - Fleet total: Δ$ (Phase B - Phase A)
"""
import os
import time
from collections import defaultdict


def main():
    from safety import make_trade_log
    log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    events = log.tail(20000)
    now = time.time()

    # Index events by (symbol, sleeve_id)
    completed = defaultdict(list)   # cycles that actually finished
    shadow_exits = defaultdict(list)  # Phase B would-exit events
    hwm_ratchets = defaultdict(list)  # HWM ratchet events

    for e in events:
        et = e.get("event_type") or ""
        k = (e.get("symbol"), e.get("sleeve_id"))
        if et == "sleeve_cycle_completed":
            completed[k].append(e)
        elif et == "phase_b_would_exit":
            shadow_exits[k].append(e)
        elif et == "phase_b_hwm_ratchet":
            hwm_ratchets[k].append(e)

    print("=" * 96)
    print(f"PHASE B SHADOW vs PHASE A ACTUAL — {time.strftime('%H:%M:%S UTC', time.gmtime())}")
    print("=" * 96)

    if not any(completed.values()):
        print("\nNo completed cycles yet — Phase B shadow needs a full BUY→SELL cycle "
              "to compare. Come back after some sleeves cycle.")
        return

    total_actual_pnl = 0.0
    total_shadow_pnl = 0.0
    per_sleeve_delta = {}

    for k, cycles in sorted(completed.items()):
        pid, sid = k
        cycles_sorted = sorted(cycles, key=lambda e: float(e.get("ts") or 0))
        shadow_sorted = sorted(shadow_exits.get(k, []),
                                key=lambda e: float(e.get("ts") or 0))
        hwm_sorted = sorted(hwm_ratchets.get(k, []),
                             key=lambda e: float(e.get("ts") or 0))

        actual_sum = 0.0
        shadow_sum = 0.0
        n_matched = 0
        peak_hwms = []

        # Rough pairing: for each completed cycle at ts T, find the shadow
        # would_exit event closest to T (within ±30 min) that PRECEDED T.
        # phase_b_would_exit is idempotent per cycle so at most one per
        # actual cycle.
        for c in cycles_sorted:
            c_ts = float(c.get("ts") or 0)
            actual_pnl = float(c.get("cycle_pnl") or 0)
            actual_sum += actual_pnl

            # Find shadow that fired before this cycle (within 6h)
            _cands = [s for s in shadow_sorted
                      if 0 < c_ts - float(s.get("ts") or 0) < 6 * 3600]
            if _cands:
                s = _cands[-1]  # closest preceding
                shadow_pnl = float(s.get("hypothetical_pnl") or 0)
                shadow_sum += shadow_pnl
                n_matched += 1

            # Peak HWM during this cycle
            _hwms = [float(h.get("new_hwm") or 0) for h in hwm_sorted
                     if 0 < c_ts - float(h.get("ts") or 0) < 6 * 3600]
            if _hwms:
                peak_hwms.append(max(_hwms))

        if not cycles_sorted:
            continue

        delta = shadow_sum - actual_sum
        total_actual_pnl += actual_sum
        total_shadow_pnl += shadow_sum
        per_sleeve_delta[k] = delta

        print(f"\n{pid} · {sid}")
        print(f"  cycles completed:       {len(cycles_sorted)}")
        print(f"  matched shadow events:  {n_matched}")
        print(f"  actual Phase A P&L:     ${actual_sum:+.4f}")
        print(f"  shadow Phase B P&L:     ${shadow_sum:+.4f}")
        print(f"  Δ (shadow - actual):    ${delta:+.4f}"
              + ("  ← Phase B WOULD WIN" if delta > 0.01
                 else "  ← Phase A won" if delta < -0.01
                 else "  ← tie"))
        if peak_hwms:
            print(f"  peak HWM tracked:       {sum(peak_hwms) / len(peak_hwms):.6f} (avg)")

    print("\n" + "=" * 96)
    print("FLEET TOTAL")
    print("=" * 96)
    print(f"  Phase A actual P&L:  ${total_actual_pnl:+.4f}")
    print(f"  Phase B shadow P&L:  ${total_shadow_pnl:+.4f}")
    print(f"  Δ (would-have):      ${total_shadow_pnl - total_actual_pnl:+.4f}")
    if total_shadow_pnl > total_actual_pnl + 0.10:
        print("\n  Phase B ratcheting bracket looks PROMISING — captured more P&L in shadow.")
    elif total_shadow_pnl < total_actual_pnl - 0.10:
        print("\n  Phase B ratcheting bracket UNDER-PERFORMED — fixed LIMITs captured more.")
    else:
        print("\n  Phase B ~ Phase A parity. Need more cycles to draw a conclusion.")

    # Bonus: how many hwm_ratchet events fired?
    total_ratchets = sum(len(v) for v in hwm_ratchets.values())
    total_shadows = sum(len(v) for v in shadow_exits.values())
    print(f"\n  Total phase_b_hwm_ratchet events: {total_ratchets}")
    print(f"  Total phase_b_would_exit events:  {total_shadows}")


if __name__ == "__main__":
    main()
