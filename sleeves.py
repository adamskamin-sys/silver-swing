"""
Sleeves — layered/parallel strategies within a single symbol.

Each sleeve is an independent state machine trading its own qty of contracts
with its own exit_mode / sell_px / buy_px / trail settings. All sleeves share
the same underlying position on the exchange; the floor guard is enforced at
the symbol level (total sells armed + swing_held ≤ position - core).

Legacy single-strategy configs auto-inflate to a single sleeve so nothing
breaks. New configs specify a `sleeves` list explicitly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class SleeveStateEnum(str, Enum):
    ARMED_SELL = "ARMED_SELL"
    ARMED_BUY = "ARMED_BUY"
    HALTED = "HALTED"


@dataclass
class SleeveConfig:
    id: str
    name: str
    qty: int
    exit_mode: str = "fixed_limit"
    sell_px: float = 65.0
    buy_px: float = 63.0
    trail_trigger: float = 65.0
    trail_distance: float = 0.20
    reanchor_threshold: float = 2.0
    # Hybrid mode (exit_mode="hybrid"): once sell_px is crossed, wait
    # hybrid_delay_secs to see if the market pushes through trail_activation_px.
    # If it does → engage trailing stop and ride the breakout. If it doesn't →
    # market-sell at the end of the delay window (took the swing at target).
    trail_activation_px: float = 65.5
    hybrid_delay_secs: float = 5.0
    # Per-sleeve accumulation. When enabled, the sleeve grows its own qty
    # (up to max_qty) after each completed cycle if banked profit covers
    # margin_per_contract × scale_up_buffer_mult. Mirrors the primary's
    # scale-up mechanism but scoped to this sleeve's own realized_pnl —
    # so each strategy compounds independently.
    accumulate_enabled: bool = False
    max_qty: int = 0                          # 0 or <= qty disables
    scale_up_buffer_mult: float = 1.5

    # Per-sleeve stop-loss. Fires independently: only this sleeve halts,
    # rest of the strategies keep running. Qty modes match the primary's:
    #   all      → flatten this sleeve's held contracts (respecting core)
    #   original → sell only the sleeve's starting cfg.qty
    #   custom   → user-specified qty
    stop_loss_enabled: bool = False
    stop_loss_px: float = 0.0
    stop_loss_qty_mode: str = "all"           # "all" | "original" | "custom"
    stop_loss_qty_custom: int = 0

    # Mean reversion (exit_mode="mean_reversion") — Ornstein-Uhlenbeck style
    # regime signal. Sleeve maintains a rolling window of prices, computes
    # mean μ and stddev σ every tick, and arms buy at μ − k×σ, sell at μ + k×σ.
    # Adapts to whatever regime silver is in without hand-tuned levels.
    # Theory: Roll (1984), Ornstein-Uhlenbeck mean-reversion literature.
    mr_window: int = 100                       # ticks in rolling window
    mr_k: float = 2.0                          # bands at μ ± k×σ
    mr_min_spread: float = 0.10                # never arm tighter than this
    # Bollinger, momentum, Avellaneda-Stoikov, VPIN-gated go here in follow-ups.

    @classmethod
    def from_dict(cls, d: dict) -> "SleeveConfig":
        return cls(
            id=d["id"],
            name=d.get("name") or d["id"],
            qty=int(d.get("qty") or 1),
            exit_mode=d.get("exit_mode") or "fixed_limit",
            sell_px=float(d.get("sell_px") or 65.0),
            buy_px=float(d.get("buy_px") or 63.0),
            trail_trigger=float(d.get("trail_trigger") or 65.0),
            trail_distance=float(d.get("trail_distance") or 0.20),
            reanchor_threshold=float(d.get("reanchor_threshold") or 2.0),
            trail_activation_px=float(d.get("trail_activation_px") or 65.5),
            hybrid_delay_secs=float(d.get("hybrid_delay_secs") or 5.0),
            accumulate_enabled=bool(d.get("accumulate_enabled") or False),
            max_qty=int(d.get("max_qty") or 0),
            scale_up_buffer_mult=float(d.get("scale_up_buffer_mult") or 1.5),
            stop_loss_enabled=bool(d.get("stop_loss_enabled") or False),
            stop_loss_px=float(d.get("stop_loss_px") or 0.0),
            stop_loss_qty_mode=str(d.get("stop_loss_qty_mode") or "all"),
            stop_loss_qty_custom=int(d.get("stop_loss_qty_custom") or 0),
            mr_window=int(d.get("mr_window") or 100),
            mr_k=float(d.get("mr_k") or 2.0),
            mr_min_spread=float(d.get("mr_min_spread") or 0.10),
        )


@dataclass
class SleeveState:
    id: str
    state: SleeveStateEnum = SleeveStateEnum.ARMED_SELL
    live_order_id: Optional[str] = None
    filled_qty: int = 0
    last_sell_qty: int = 0
    last_sell_fill_price: Optional[float] = None
    realized_pnl: float = 0.0
    cycles: int = 0
    trail_armed: bool = False
    trail_high_water_price: float = 0.0
    # Per-sleeve accumulation. current_qty grows from cfg.qty toward cfg.max_qty
    # as realized_pnl covers margin_per_contract × scale_up_buffer_mult. Starts
    # at 0 which the SwingTrader interprets as "not yet initialized — use cfg.qty".
    current_qty: int = 0
    # Hybrid mode: timestamp when sell_px was first crossed. None = not yet
    # triggered; a value = we're inside the delay window watching for either
    # trail_activation_px (→ engage trail) or delay expiry (→ market sell).
    hybrid_sell_triggered_ts: Optional[float] = None
    # Weighted-avg entry price of the contracts THIS sleeve owns at the moment
    # it arms a sell — the anchor for realized-P/L when the sell fills. Set by
    # _sleeve_avg_entry when we place the sell order, cleared after the fill
    # credits realized. None = not captured yet (or between cycles).
    sell_entry_avg: Optional[float] = None
    # Fill price of contracts THIS sleeve BOUGHT via its own state machine.
    # Set on a BUY fill, cleared on a SELL fill. Used for the sleeve-row
    # unrealized display so newly-created sleeves show $0 (until they trade)
    # instead of inheriting mark-to-market on pre-existing paper lots — that
    # inherited paper gain still shows up in the account-level unrealized at
    # the top of the card, just not double-counted per sleeve.
    own_avg_entry: Optional[float] = None
    # Human-readable reason the sleeve is HALTED. Surfaced on the strategy
    # row so the user knows WHY it stopped (paused via dashboard vs stop-loss
    # fired vs abort band vs core-floor breach). Empty when running.
    halt_reason: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict, sleeve_id: str) -> "SleeveState":
        return cls(
            id=sleeve_id,
            state=SleeveStateEnum(d.get("state", "ARMED_SELL")),
            live_order_id=d.get("live_order_id"),
            filled_qty=int(d.get("filled_qty") or 0),
            last_sell_qty=int(d.get("last_sell_qty") or 0),
            last_sell_fill_price=d.get("last_sell_fill_price"),
            realized_pnl=float(d.get("realized_pnl") or 0.0),
            cycles=int(d.get("cycles") or 0),
            trail_armed=bool(d.get("trail_armed") or False),
            trail_high_water_price=float(d.get("trail_high_water_price") or 0.0),
            hybrid_sell_triggered_ts=d.get("hybrid_sell_triggered_ts"),
            current_qty=int(d.get("current_qty") or 0),
            own_avg_entry=d.get("own_avg_entry"),
            halt_reason=d.get("halt_reason"),
        )

    def to_dict(self) -> dict:
        return {**asdict(self), "state": self.state.value}


def inflate_legacy_config(cfg: dict) -> list[SleeveConfig]:
    """If cfg has no sleeves, build one from the flat legacy fields.
    That keeps every existing test and existing store file working."""
    if cfg.get("sleeves"):
        return [SleeveConfig.from_dict(s) for s in cfg["sleeves"]]
    return [SleeveConfig(
        id="s1",
        name="main",
        qty=int(cfg.get("swing_qty") or 2),
        exit_mode=cfg.get("exit_mode") or "fixed_limit",
        sell_px=float(cfg.get("sell_px") or 65.0),
        buy_px=float(cfg.get("buy_px") or 63.0),
        trail_trigger=float(cfg.get("trail_trigger") or cfg.get("sell_px") or 65.0),
        trail_distance=float(cfg.get("trail_distance") or 0.20),
        reanchor_threshold=float(cfg.get("reanchor_threshold") or 2.0),
    )]


def inflate_legacy_state(state: dict, sleeves: list[SleeveConfig]) -> dict[str, SleeveState]:
    """Load per-sleeve state. Migration: if the state has no `sleeves` sub-dict,
    map the flat legacy fields onto the single legacy sleeve."""
    per_sleeve = state.get("sleeves") or {}
    result: dict[str, SleeveState] = {}
    for sc in sleeves:
        if sc.id in per_sleeve:
            result[sc.id] = SleeveState.from_dict(per_sleeve[sc.id], sc.id)
        elif not per_sleeve and len(sleeves) == 1:
            # legacy: one sleeve, flat fields on state
            result[sc.id] = SleeveState(
                id=sc.id,
                state=SleeveStateEnum(state.get("state", "ARMED_SELL")),
                live_order_id=state.get("live_order_id"),
                filled_qty=int(state.get("filled_qty") or 0),
                last_sell_qty=int(state.get("last_sell_qty") or 0),
                last_sell_fill_price=state.get("last_sell_fill_price"),
                realized_pnl=float(state.get("realized_pnl") or 0.0),
                cycles=int(state.get("cycles") or 0),
                trail_armed=bool(state.get("trail_armed") or False),
                trail_high_water_price=float(state.get("trail_high_water_price") or 0.0),
            )
        else:
            # new sleeve added mid-session; start fresh
            result[sc.id] = SleeveState(id=sc.id)
    return result


def sleeves_to_state_dict(sleeves: dict[str, SleeveState]) -> dict:
    return {sid: s.to_dict() for sid, s in sleeves.items()}
