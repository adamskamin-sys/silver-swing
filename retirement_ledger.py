"""Retirement ledger — tenant-scoped record of sleeve retirements + cooldowns.

Closes the ghost-sleeve class (PT/HYP/SLR 2026-07):
  1. `diag_retire_sleeves.py` removes the sleeve from state, but config still
     has it → next tick's `_load_sleeves_cfg()` returns the sleeve id →
     `_multi_sleeve_step()` at swing_leg.py:4121-4123 creates a fresh
     SleeveState → `_save_state()` writes it back → ghost.
  2. Even if the sleeve is removed from BOTH state and config, the scanner
     re-scans the same product and can re-arm it minutes later. Nothing
     currently prevents this.

Both failure modes route through a single mechanism: a tenant-scoped ledger
that records `{product_id, sleeve_id, retired_at, cooldown_hours}` entries.
The tick loop and any sleeve-creation path check `is_in_cooldown(product_id)`
before instantiating. If the product is in cooldown, the sleeve is NOT
created — logged and skipped, with a clear reason.

Stored under CONFIG scope with symbol `__retirement_ledger__` (same pattern
as `__portfolio__`). Not per-product, so a retire on product X blocks all
re-arm attempts on X — matches the "I don't want any sleeve on this product
for a while" intent.

Manual override: `diag_clear_retirement.py <product_id>` clears a specific
product's retirement so it can be re-armed immediately.
"""
from __future__ import annotations
import time
from typing import Optional


LEDGER_SYMBOL = "__retirement_ledger__"
# 5 min — beats every fast-race scenario (bot tick every ~1s, scanner every
# 30s), short enough Adam can wait it out. Longer durations available via
# the --cooldown-hours flag on diag_retire_sleeves. Clear immediately with
# diag_clear_retirement.py PRODUCT_ID --apply.
DEFAULT_COOLDOWN_HOURS = 5.0 / 60.0


def _load(store, tenant: str) -> dict:
    return store.get_config(tenant, LEDGER_SYMBOL) or {"entries": []}


def _save(store, tenant: str, data: dict) -> None:
    store.put_config(tenant, LEDGER_SYMBOL, data)


def record_retirement(
    store,
    tenant: str,
    product_id: str,
    sleeve_id: str,
    reason: str,
    cooldown_hours: float = DEFAULT_COOLDOWN_HOURS,
    now_ts: Optional[float] = None,
) -> dict:
    """Append a retirement entry. Returns the new entry.

    Multiple retirements on the same product are allowed and stacked; the
    cooldown that expires LAST wins (`is_in_cooldown` picks max expiry).
    """
    now = now_ts if now_ts is not None else time.time()
    entry = {
        "product_id": product_id,
        "sleeve_id": sleeve_id,
        "retired_at": now,
        "cooldown_hours": float(cooldown_hours),
        "reason": reason,
    }
    data = _load(store, tenant)
    entries = list(data.get("entries") or [])
    entries.append(entry)
    data["entries"] = entries
    _save(store, tenant, data)
    return entry


def is_in_cooldown(
    store,
    tenant: str,
    product_id: str,
    now_ts: Optional[float] = None,
) -> tuple[bool, str, float]:
    """Returns (in_cooldown, reason, seconds_remaining).

    If not in cooldown, reason == "" and seconds_remaining == 0.
    """
    now = now_ts if now_ts is not None else time.time()
    data = _load(store, tenant)
    entries = data.get("entries") or []
    best_expiry = 0.0
    best_reason = ""
    for e in entries:
        if e.get("product_id") != product_id:
            continue
        try:
            retired_at = float(e.get("retired_at") or 0)
            cooldown_h = float(e.get("cooldown_hours") or 0)
        except (TypeError, ValueError):
            continue
        expiry = retired_at + cooldown_h * 3600.0
        if expiry > best_expiry:
            best_expiry = expiry
            best_reason = str(e.get("reason") or "retired")
    if best_expiry > now:
        return True, best_reason, best_expiry - now
    return False, "", 0.0


def clear_product(store, tenant: str, product_id: str) -> int:
    """Remove all ledger entries for product_id. Returns count removed.

    Use when you want to re-arm a retired product before cooldown expires.
    """
    data = _load(store, tenant)
    entries = list(data.get("entries") or [])
    kept = [e for e in entries if e.get("product_id") != product_id]
    removed = len(entries) - len(kept)
    if removed:
        data["entries"] = kept
        _save(store, tenant, data)
    return removed


def prune_expired(store, tenant: str, now_ts: Optional[float] = None) -> int:
    """Drop entries whose cooldown has expired. Returns count pruned.

    Kept for at most 30 days after cooldown expires for audit visibility,
    then dropped to keep the ledger from growing unbounded.
    """
    now = now_ts if now_ts is not None else time.time()
    data = _load(store, tenant)
    entries = list(data.get("entries") or [])
    kept: list[dict] = []
    pruned = 0
    for e in entries:
        try:
            retired_at = float(e.get("retired_at") or 0)
            cooldown_h = float(e.get("cooldown_hours") or 0)
        except (TypeError, ValueError):
            pruned += 1
            continue
        expiry = retired_at + cooldown_h * 3600.0
        keep_until = expiry + 30 * 24 * 3600.0
        if now < keep_until:
            kept.append(e)
        else:
            pruned += 1
    if pruned:
        data["entries"] = kept
        _save(store, tenant, data)
    return pruned


def list_active(
    store, tenant: str, now_ts: Optional[float] = None
) -> list[dict]:
    """Return entries whose cooldown is still active, newest first."""
    now = now_ts if now_ts is not None else time.time()
    data = _load(store, tenant)
    active = []
    for e in data.get("entries") or []:
        try:
            retired_at = float(e.get("retired_at") or 0)
            cooldown_h = float(e.get("cooldown_hours") or 0)
        except (TypeError, ValueError):
            continue
        if retired_at + cooldown_h * 3600.0 > now:
            active.append(e)
    active.sort(key=lambda e: -float(e.get("retired_at") or 0))
    return active
