"""Entry velocity / falling-knife gate (crew).

Answers one question: "is price dropping too fast / too forcefully to buy into
right now?" — a per-instrument, SELF-SCALING replacement for the blanket
bounce-wait. For a slow mover (copper) it essentially never fires, so the buy
just fills at the target; for a fast/knife-prone contract it holds the buy only
during the actual dangerous drop, then releases.

Velocity is the PRIMARY trigger; the experts add three flow-continuation factors
that say whether a fast drop will KEEP going:

  1. VELOCITY / JUMP (Lee-Mykland 2008). |last return| scaled by LOCAL volatility
     (bipower variation, robust to the jump itself). >= jump_sigma = a
     discontinuous momentum move, not orderly diffusion — don't buy the knife.
     Self-scaling: strict on fast crypto, permissive on slow metals, no
     per-product tuning. This alone blocks.
  2. FLOW TOXICITY (VPIN — Easley-Lopez de Prado-O'Hara 2012). Informed/forced
     selling persists; don't add liquidity into it.
  3. FLOW PERSISTENCE (OFI — Cont-Kukanov-Stoikov 2014; Lillo-Farmer long
     memory; aggressor runs). Persistent one-sided sell flow has momentum.
  4. LIQUIDITY EVAPORATION (Kyle's lambda 1985; OBI — Cartea-Jaimungal). A
     vanishing bid means even a slow drop can gap; the fill is into a vacuum.

A velocity jump-down blocks on its own. The softer flow factors block only when
>= min_flow_signals of them agree (a genuinely forced drop), so a single
moderately-elevated reading doesn't over-gate an ordinary entry.

Read-only: returns {block, reason, ...}; the strategy holds or arms the buy.
Regime (trend vs range) + support proximity live in the entry-quality light.
"""

from __future__ import annotations

from typing import Optional

import crash_guard as _crash   # reuse the tested Lee-Mykland jump_stat


DEFAULT_KNIFE_CONFIG = {
    "jump_sigma": 4.0,        # Lee-Mykland: |ret|/local-sigma >= this = a jump-down
    "vpin_toxic": 0.70,       # VPIN at/above this = toxic/forced flow
    "ofi_sell": 0.60,         # normalized OFI <= -this = persistent selling
    "obi_depleted": 0.60,     # order-book imbalance <= -this = bid depleted
    "kyle_spike_x": 3.0,      # Kyle's lambda vs baseline = liquidity gone
    "aggressor_run": 5,       # consecutive sell-initiated trades
    "min_flow_signals": 2,    # this many soft flow factors agreeing also blocks
}


def _get(ms, *keys):
    for k in keys:
        if isinstance(ms, dict) and ms.get(k) is not None:
            return ms[k]
    return None


def knife_gate(recent_returns, ms: Optional[dict] = None, cfg: Optional[dict] = None) -> dict:
    """Decide whether to HOLD a buy because price is dropping too fast/forcefully.

    recent_returns : recent per-bar returns (oldest -> newest).
    ms             : microstructure snapshot (vpin/ofi/obi/kyle/aggressor) if any.
    Returns {block: bool, reason, velocity, fired:[...]}.
    Fail-safe: with no data it does NOT block (buy fills normally)."""
    c = {**DEFAULT_KNIFE_CONFIG, **(cfg or {})}
    rets = [float(x) for x in (recent_returns or []) if x is not None]
    last = rets[-1] if rets else 0.0

    # 1. velocity (Lee-Mykland jump) — the primary trigger
    jump = _crash.jump_stat(rets)
    velocity_block = jump is not None and jump >= c["jump_sigma"] and last < 0

    fired = []
    if velocity_block:
        fired.append(f"velocity {jump:.1f}sigma jump-down")

    # 2-4. flow-continuation factors (soft; need min_flow_signals to agree)
    flow = []
    vpin = _get(ms, "vpin")
    ofi = _get(ms, "trade_ofi_60s", "ofi", "trade_ofi")
    obi = _get(ms, "obi", "order_book_imbalance")
    kyle = _get(ms, "kyle_lambda")
    kbase = _get(ms, "kyle_lambda_baseline", "kyle_lambda_avg")
    arun = _get(ms, "aggressor_run", "aggressor_run_len")

    if vpin is not None and float(vpin) >= c["vpin_toxic"]:
        flow.append(f"toxic flow VPIN {float(vpin):.2f}")
    if ofi is not None and float(ofi) <= -abs(c["ofi_sell"]):
        flow.append(f"persistent selling OFI {float(ofi):+.2f}")
    if obi is not None and float(obi) <= -abs(c["obi_depleted"]):
        flow.append(f"bid depleted OBI {float(obi):+.2f}")
    if kyle is not None and kbase and float(kbase) > 0 and float(kyle) >= c["kyle_spike_x"] * float(kbase):
        flow.append(f"liquidity gone Kyle {float(kyle) / float(kbase):.1f}x")
    if arun is not None and float(arun) >= c["aggressor_run"]:
        flow.append(f"aggressor run {int(float(arun))}")

    flow_block = len(flow) >= int(c["min_flow_signals"])
    fired += flow

    block = bool(velocity_block or flow_block)
    return {
        "block": block,
        "velocity": round(jump, 2) if jump is not None else None,
        "fired": fired,
        "reason": ("; ".join(fired) if block
                   else "drop is orderly (not a jump, flow calm) — safe to fill at target"),
    }
