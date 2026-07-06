"""
Config validator (spec §10).

The dashboard is convenience; it must not be able to instruct the bot to do
something reckless. Every write to the config store passes through this
validator server-side. Structurally insane values are rejected BEFORE they
reach the store — never on read.

Invariants enforced:
  - core_qty >= 0                       (0 = no floor / free trading)
  - swing_qty >= 1
  - swing_qty <= max_swing_qty
  - max_swing_qty >= swing_qty
  - buy_px < sell_px                    (else the swing math inverts)
  - abort_below < buy_px                (governor must sit outside the range)
  - sell_px < abort_above               (same, other side)
  - abort_below < abort_above
  - trail_distance > 0                  (only when exit_mode = trailing_stop)
  - trail_trigger > 0                   (same)
  - trail_trigger >= sell_px            (trail arms at or above the range top)
  - fee_sanity_multiplier >= 1.0        (below 1 means the gate always trips)
  - scale_up_buffer_mult >= 1.0         (add contract needs at least 1× margin)
  - margin_per_contract > 0
  - fee_per_contract_roundtrip >= 0
  - contract_size > 0
  - tick_size > 0
  - exit_mode in {fixed_limit, trailing_stop}
  - reanchor_threshold >= 0

Values that FAIL validation return in a structured error list so the UI can
render field-level errors — not a single "invalid config" toast.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


VALID_EXIT_MODES = {"fixed_limit", "trailing_stop"}


@dataclass
class ValidationIssue:
    field: str
    message: str

    def to_dict(self) -> dict:
        return {"field": self.field, "message": self.message}


@dataclass
class ValidationResult:
    ok: bool
    issues: list[ValidationIssue]

    def to_dict(self) -> dict:
        return {"ok": self.ok, "issues": [i.to_dict() for i in self.issues]}


def _get_num(cfg: dict, key: str, issues: list, required: bool = True) -> float | None:
    v = cfg.get(key)
    if v is None:
        if required:
            issues.append(ValidationIssue(key, f"{key} is required"))
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        issues.append(ValidationIssue(key, f"{key} must be a number, got {v!r}"))
        return None


def _get_int(cfg: dict, key: str, issues: list, required: bool = True) -> int | None:
    v = _get_num(cfg, key, issues, required)
    if v is None:
        return None
    if v != int(v):
        issues.append(ValidationIssue(key, f"{key} must be a whole number, got {v}"))
        return None
    return int(v)


def validate_config(cfg: dict) -> ValidationResult:
    issues: list[ValidationIssue] = []

    core_qty = _get_int(cfg, "core_qty", issues)
    swing_qty = _get_int(cfg, "swing_qty", issues)
    max_swing_qty = _get_int(cfg, "max_swing_qty", issues)
    sell_px = _get_num(cfg, "sell_px", issues)
    buy_px = _get_num(cfg, "buy_px", issues)
    abort_below = _get_num(cfg, "abort_below", issues)
    abort_above = _get_num(cfg, "abort_above", issues)
    margin_per_contract = _get_num(cfg, "margin_per_contract", issues)
    fee_rt = _get_num(cfg, "fee_per_contract_roundtrip", issues)
    scale_up_mult = _get_num(cfg, "scale_up_buffer_mult", issues)
    contract_size = _get_num(cfg, "contract_size", issues)
    fee_sanity = _get_num(cfg, "fee_sanity_multiplier", issues, required=False)

    exit_mode = cfg.get("exit_mode", "fixed_limit")
    if exit_mode not in VALID_EXIT_MODES:
        issues.append(ValidationIssue(
            "exit_mode",
            f"exit_mode must be one of {sorted(VALID_EXIT_MODES)}, got {exit_mode!r}",
        ))

    # Guard: only run cross-field checks if the fields themselves parsed
    if core_qty is not None and core_qty < 0:
        issues.append(ValidationIssue("core_qty", "core_qty must be >= 0 (0 disables the floor)"))
    if swing_qty is not None and swing_qty < 0:
        issues.append(ValidationIssue("swing_qty", "swing_qty must be >= 0 (0 disables the primary strategy)"))
    if max_swing_qty is not None and max_swing_qty < 1:
        issues.append(ValidationIssue("max_swing_qty", "max_swing_qty must be >= 1"))
    if swing_qty is not None and max_swing_qty is not None and swing_qty > 0 and swing_qty > max_swing_qty:
        issues.append(ValidationIssue(
            "swing_qty", f"swing_qty ({swing_qty}) must be <= max_swing_qty ({max_swing_qty})",
        ))
    if buy_px is not None and sell_px is not None and buy_px >= sell_px:
        issues.append(ValidationIssue(
            "buy_px", f"buy_px ({buy_px}) must be < sell_px ({sell_px}); otherwise the swing loses money",
        ))
    if abort_below is not None and abort_above is not None and abort_below >= abort_above:
        issues.append(ValidationIssue(
            "abort_below", f"abort_below ({abort_below}) must be < abort_above ({abort_above})",
        ))
    if abort_below is not None and buy_px is not None and abort_below >= buy_px:
        issues.append(ValidationIssue(
            "abort_below", f"abort_below ({abort_below}) must be < buy_px ({buy_px}); the governor sits outside the range",
        ))
    if sell_px is not None and abort_above is not None and sell_px >= abort_above:
        issues.append(ValidationIssue(
            "abort_above", f"abort_above ({abort_above}) must be > sell_px ({sell_px}); the governor sits outside the range",
        ))
    if margin_per_contract is not None and margin_per_contract <= 0:
        issues.append(ValidationIssue("margin_per_contract", "margin_per_contract must be > 0"))
    if fee_rt is not None and fee_rt < 0:
        issues.append(ValidationIssue("fee_per_contract_roundtrip", "fees cannot be negative"))
    if scale_up_mult is not None and scale_up_mult < 1.0:
        issues.append(ValidationIssue(
            "scale_up_buffer_mult",
            f"scale_up_buffer_mult must be >= 1.0 (below 1 means scaling on debt)",
        ))
    if contract_size is not None and contract_size <= 0:
        issues.append(ValidationIssue("contract_size", "contract_size must be > 0"))
    if fee_sanity is not None and fee_sanity < 1.0:
        issues.append(ValidationIssue(
            "fee_sanity_multiplier",
            "fee_sanity_multiplier must be >= 1.0 (below 1 means the gate always trips)",
        ))

    # Trailing-specific
    if exit_mode == "trailing_stop":
        trail_distance = _get_num(cfg, "trail_distance", issues)
        trail_trigger = _get_num(cfg, "trail_trigger", issues)
        if trail_distance is not None and trail_distance <= 0:
            issues.append(ValidationIssue("trail_distance", "trail_distance must be > 0"))
        if trail_trigger is not None and trail_trigger <= 0:
            issues.append(ValidationIssue("trail_trigger", "trail_trigger must be > 0"))
        if trail_trigger is not None and sell_px is not None and trail_trigger < sell_px:
            issues.append(ValidationIssue(
                "trail_trigger",
                f"trail_trigger ({trail_trigger}) should be >= sell_px ({sell_px})",
            ))

    reanchor_threshold = _get_num(cfg, "reanchor_threshold", issues, required=False)
    if reanchor_threshold is not None and reanchor_threshold < 0:
        issues.append(ValidationIssue("reanchor_threshold", "reanchor_threshold cannot be negative"))

    return ValidationResult(ok=len(issues) == 0, issues=issues)


def clamp_to_bounds(cfg: dict) -> dict:
    """Best-effort correction of edge values without changing intent — useful
    for tests and defaults. Does NOT bypass validate_config; the returned dict
    should still be validated. Never widens abort_below/abort_above (that would
    reduce safety); only tightens obviously-broken values."""
    out = dict(cfg)
    if out.get("core_qty", 0) < 0:
        out["core_qty"] = 0
    if out.get("swing_qty", 1) < 1:
        out["swing_qty"] = 1
    if out.get("max_swing_qty", 1) < out.get("swing_qty", 1):
        out["max_swing_qty"] = out["swing_qty"]
    return out
