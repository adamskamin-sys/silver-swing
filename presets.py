"""
Risk presets (spec §7 risk dial).

Each preset is a callable that returns a config-dict shape. The dashboard
picks a preset name, we materialize the dict, the user optionally overrides
individual fields, and the resulting dict lands in the StateStore.

Presets bundle the risk-facing knobs together (spec §7): `trail_distance`,
`scale_up_buffer_mult`, `max_swing_qty`, `abort_below`, `abort_above`, and
the strategy toggle. They're a starting point, not a lock — the user hand-
tunes from there.

Values below are grounded in Adam's current SLR-27AUG26-CDE reality:
  - Current price ~$62.80
  - Adam's baseline range: 63 ↔ 65 (2-point swing)
  - Adam holds 12 contracts (10 core + 2 swing)
  - Est. liquidation at $50.10

The `abort_*` levels bracket the practical trading range; `trail_distance`
scales with volatility appetite.
"""

from __future__ import annotations

from typing import Callable

# ---- Base defaults shared by all presets ----------------------------------

_BASE = {
    "core_qty": 10,
    "swing_qty": 2,
    "contract_size": 50,
    "margin_per_contract": 275.0,
    "fee_per_contract_roundtrip": 4.68,
    "fee_sanity_multiplier": 2.0,
    "tick_size": 0.005,
    # Trailing config that's the same across presets
    "trail_trigger": 65.0,
    "reanchor_threshold": 2.0,  # gap above sell_px that triggers re-anchor
}


def preset_conservative() -> dict:
    """Tight risk envelope. Small position, wide abort bracket, wait for a
    lot of profit before adding a contract. Range-scalp only — no trailing."""
    return {
        **_BASE,
        "exit_mode": "fixed_limit",
        "max_swing_qty": 3,
        "sell_px": 65.0,
        "buy_px": 63.0,
        "abort_below": 58.0,
        "abort_above": 72.0,
        "scale_up_buffer_mult": 2.0,   # need 2× a contract's margin banked
        "trail_distance": 0.25,        # $0.25 = 50 ticks; unused unless mode toggled
    }


def preset_moderate() -> dict:
    """Adam's current setup as a preset. 2-point range, trailing available."""
    return {
        **_BASE,
        "exit_mode": "fixed_limit",
        "max_swing_qty": 5,
        "sell_px": 65.0,
        "buy_px": 63.0,
        "abort_below": 60.0,
        "abort_above": 70.0,
        "scale_up_buffer_mult": 1.5,
        "trail_distance": 0.20,
    }


def preset_aggressive() -> dict:
    """Trailing-first, tight abort_above (trail through the breakout), lower
    scale-up bar. More cycles per unit time; larger drawdowns when regime is
    wrong."""
    return {
        **_BASE,
        "exit_mode": "trailing_stop",
        "max_swing_qty": 8,
        "sell_px": 65.0,
        "buy_px": 63.0,
        "abort_below": 61.0,
        "abort_above": 80.0,           # let it run further
        "scale_up_buffer_mult": 1.0,
        "trail_distance": 0.15,        # tight trail — captures more of the top
    }


PRESETS: dict[str, Callable[[], dict]] = {
    "conservative": preset_conservative,
    "moderate": preset_moderate,
    "aggressive": preset_aggressive,
}


def preset(name: str) -> dict:
    """Materialize a preset by name. Callers can .update() with overrides."""
    if name not in PRESETS:
        raise ValueError(f"unknown preset {name!r}. Options: {sorted(PRESETS)}")
    return PRESETS[name]()
