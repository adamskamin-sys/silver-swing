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

    # Ratcheting stop-loss (chandelier-style). Once unrealized/contract crosses
    # ratchet_activation, the effective stop is max(stop_loss_px, HWM − ratchet_distance).
    # Locks in gains as silver rises; never moves down. Independently toggled
    # from the fixed stop_loss above.
    stop_loss_ratchet_enabled: bool = False
    stop_loss_ratchet_distance: float = 1.50  # $ below HWM
    stop_loss_ratchet_activation: float = 0.50  # $/contract unrealized before ratchet arms

    # Reanchor buy_px + sell_px to bracket current mark after the stop fires.
    # Without this, the sleeve halts and requires manual Resume; with it, the
    # sleeve keeps trading at the new price level (silver at $60 → buy $59.90,
    # sell $60.10 instead of leaving the old $61.336 / $61.539 stranded).
    stop_loss_reanchor_on_trigger: bool = False

    # Safety cap: after N consecutive stop-out cycles without a winning
    # round-trip in between, halt the sleeve for manual review. Protects
    # against reanchor+stop chains during multi-day bleeds. 0 = unlimited.
    stop_loss_max_consecutive: int = 0

    # Signal-based re-entry after a stop-out (Van Tharp SafeZone / volatility
    # contraction). Modes:
    #   off        — no re-entry (sleeve halts, waits for manual Resume)
    #   reanchor   — instant reanchor (matches stop_loss_reanchor_on_trigger)
    #   volatility — wait for range to contract below contraction × pre-stop
    #                range, then buy at market. Prevents chasing a still-
    #                falling market.
    reentry_mode: str = "off"                      # off | reanchor | volatility
    reentry_range_contraction: float = 0.5         # current range < X × pre-stop range
    reentry_range_window: int = 60                 # ticks in rolling range calc
    reentry_min_wait_secs: float = 30.0            # earliest re-entry after stop

    # Time-based reanchor: if the sleeve has been ARMED_BUY (waiting to rebuy
    # after a completed cycle) for at least this many seconds AND price is
    # still above buy_px, walk targets forward to bracket current market. Keeps
    # the sleeve trading when a directional run has priced us out. 0 = off.
    time_reanchor_secs: float = 0.0

    # Volatility-aware reanchor: reanchors when last_price sits at or above the
    # Nth percentile of recent price history — a signal the market is at (or
    # near) a run's peak and unlikely to revert to our stale buy target soon.
    # 0 = off. Typical: 90 (top 10% of recent bars = strong upward run).
    vol_reanchor_percentile: float = 0.0
    vol_reanchor_window: int = 60                  # bars in the percentile calc

    # Scale-in on re-entry (Livermore-style progressive entry):
    #   stage 1: half qty at re-entry signal
    #   stage 2: other half after price moves 0.5 × pre-stop-range in expected
    #            direction (i.e., market confirms recovery)
    reentry_scale_in: bool = False
    reentry_second_half_move_pct: float = 0.5      # of pre-stop range

    # News event blackout — pause new arms during scheduled high-uncertainty
    # events. Tier levels: 0=off, 1=tighten only, 2=pause new arms hold
    # existing, 3=full exit any position. Bot compares 'now' against
    # blackout_windows (list of {start_ts, end_ts, tier} entries) written
    # by the news calendar module.
    news_blackout_enabled: bool = False
    news_blackout_tier: int = 2  # default: pause new arms, hold existing

    # Microstructure gates on sleeve arms. When true, sleeve consults the
    # existing microstructure filter (OBI, VPIN, Kyle-λ) before arming. Uses
    # whichever SWING_MS_* env vars are set on the bot.
    microstructure_gate_enabled: bool = False

    # NOTE: mean_reversion / Bollinger / momentum fields deliberately not
    # declared here yet — those exit_modes aren't wired in swing_leg._sleeve_step,
    # so declaring config fields would let a user pick an unwired preset that
    # silently falls through to fixed_limit behavior. Add fields the same
    # commit that wires the strategy.

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
            stop_loss_ratchet_enabled=bool(d.get("stop_loss_ratchet_enabled") or False),
            stop_loss_ratchet_distance=float(d.get("stop_loss_ratchet_distance") or 1.50),
            stop_loss_ratchet_activation=float(d.get("stop_loss_ratchet_activation") or 0.50),
            stop_loss_reanchor_on_trigger=bool(d.get("stop_loss_reanchor_on_trigger") or False),
            stop_loss_max_consecutive=int(d.get("stop_loss_max_consecutive") or 0),
            reentry_mode=str(d.get("reentry_mode") or "off"),
            reentry_range_contraction=float(d.get("reentry_range_contraction") or 0.5),
            reentry_range_window=int(d.get("reentry_range_window") or 60),
            reentry_min_wait_secs=float(d.get("reentry_min_wait_secs") or 30.0),
            reentry_scale_in=bool(d.get("reentry_scale_in") or False),
            reentry_second_half_move_pct=float(d.get("reentry_second_half_move_pct") or 0.5),
            time_reanchor_secs=float(d.get("time_reanchor_secs") or 0.0),
            vol_reanchor_percentile=float(d.get("vol_reanchor_percentile") or 0.0),
            vol_reanchor_window=int(d.get("vol_reanchor_window") or 60),
            news_blackout_enabled=bool(d.get("news_blackout_enabled") or False),
            news_blackout_tier=int(d.get("news_blackout_tier") or 2),
            microstructure_gate_enabled=bool(d.get("microstructure_gate_enabled") or False),
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
    # State the sleeve was in immediately BEFORE it halted. Resume restores
    # this so a sleeve halted while ARMED_BUY (waiting to rebuy) comes back
    # as ARMED_BUY — not forced back to ARMED_SELL, which would sell the
    # position AGAIN and bleed contracts on every resume cycle. Written by
    # _sleeve_halt, cleared on resume.
    pre_halt_state: Optional[str] = None

    # Ratcheting stop-loss HWM — highest price seen while holding contracts.
    # Reset when the sleeve fully exits (position → 0). Never moves down.
    stop_loss_hwm: Optional[float] = None
    # Number of stop-out cycles in a row without a completed winning cycle in
    # between. Reset to 0 on a successful SELL fill at target.
    consecutive_stops: int = 0

    # Post-stop re-entry state — only used when reentry_mode = 'volatility'.
    # reentry_pending = True while watching for volatility contraction after
    # a stop fired. reentry_stop_ts records when the stop fired (used with
    # reentry_min_wait_secs to prevent instant re-entry). pre_stop_range is
    # the observed price range in the window BEFORE the stop, used as the
    # baseline to detect contraction (current_range < pre_stop_range × X).
    reentry_pending: bool = False
    reentry_stop_ts: Optional[float] = None
    pre_stop_range: float = 0.0
    # Scale-in staging: 0 = not scaling, 1 = half in (bought half of qty),
    # 2 = fully in. Only used when reentry_scale_in enabled.
    reentry_scale_in_stage: int = 0
    reentry_stage_1_price: Optional[float] = None  # for measuring the second-half trigger

    # News blackout state — timestamp until which this sleeve is paused.
    # None or 0 = not in blackout. If set, sleeve doesn't arm new orders
    # until now > blackout_until_ts.
    blackout_until_ts: Optional[float] = None

    # When the sleeve most recently entered ARMED_BUY (after a completed
    # cycle). Powers time-based reanchor — reset to now on every ARMED_BUY
    # transition (fill flip + explicit reanchor). None = not yet in ARMED_BUY
    # this session (legacy state).
    armed_buy_since_ts: Optional[float] = None

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
            pre_halt_state=d.get("pre_halt_state"),
            stop_loss_hwm=d.get("stop_loss_hwm"),
            consecutive_stops=int(d.get("consecutive_stops") or 0),
            reentry_pending=bool(d.get("reentry_pending") or False),
            reentry_stop_ts=d.get("reentry_stop_ts"),
            pre_stop_range=float(d.get("pre_stop_range") or 0.0),
            reentry_scale_in_stage=int(d.get("reentry_scale_in_stage") or 0),
            reentry_stage_1_price=d.get("reentry_stage_1_price"),
            blackout_until_ts=d.get("blackout_until_ts"),
            armed_buy_since_ts=d.get("armed_buy_since_ts"),
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
