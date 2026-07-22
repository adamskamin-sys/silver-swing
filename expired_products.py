"""Parse product_id date suffixes and identify expired / near-expiry futures.

Coinbase CFM/CDE product_ids follow the pattern PREFIX-DDMMMYY-CDE
(e.g. NOL-20JUL26-CDE = 20 Jul 2026). Perpetuals (-PERP-) and spot
(-USD/-USDC) have no expiry.

Adam 2026-07-22: "if a contract expires just remove it from the list."
This module provides pure logic. live_runner calls it at boot to prune
expired products from the store and warn on near-expiry contracts.
"""
from __future__ import annotations
import re
from datetime import datetime, timezone
from typing import Optional


_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# Match e.g. "NOL-20JUL26-CDE" → captures day=20, month=JUL, year=26
_DATE_RE = re.compile(r"-(\d{1,2})([A-Z]{3})(\d{2})-CDE$", re.IGNORECASE)


def parse_expiry_from_product_id(pid: str) -> Optional[datetime]:
    """Return datetime (UTC) of expiry parsed from CDE product_id.
    Returns None if pid is spot/perp or doesn't match the pattern.
    Assumes 20YY for 2-digit years."""
    if not pid:
        return None
    m = _DATE_RE.search(pid.upper())
    if not m:
        return None
    day = int(m.group(1))
    mon_name = m.group(2).upper()
    yr2 = int(m.group(3))
    mon = _MONTHS.get(mon_name)
    if mon is None:
        return None
    # Assume 20YY. 2-digit years like "30" become 2030.
    year = 2000 + yr2
    try:
        # Coinbase CFM settles at end of trading day UTC on expiry date
        return datetime(year, mon, day, 23, 59, 59, tzinfo=timezone.utc)
    except (ValueError, OverflowError):
        return None


def classify_products(product_ids: list[str], now: Optional[datetime] = None
                       ) -> tuple[list[str], list[tuple[str, int]], list[str]]:
    """Categorize product_ids by expiry.

    Returns (expired, near_expiry_days_pairs, healthy):
      - expired: list of pids whose expiry is in the past
      - near_expiry: list of (pid, days_to_expiry) for expiry within 3 days
      - healthy: everything else (including spot/perp with no expiry, and
        contracts with plenty of runway)
    """
    if now is None:
        now = datetime.now(timezone.utc)
    expired: list[str] = []
    near_expiry: list[tuple[str, int]] = []
    healthy: list[str] = []
    for pid in product_ids:
        exp = parse_expiry_from_product_id(pid)
        if exp is None:
            # spot / perp / unparseable — treat as healthy
            healthy.append(pid)
            continue
        delta_days = (exp - now).days
        if delta_days < 0:
            expired.append(pid)
        elif delta_days <= 3:
            near_expiry.append((pid, delta_days))
        else:
            healthy.append(pid)
    return expired, near_expiry, healthy


def is_safe_to_prune(store, tenant: str, pid: str) -> tuple[bool, str]:
    """Only prune expired products with NO held positions across all sleeves.
    A held-but-expired sleeve is a manual-intervention situation: Coinbase
    auto-settled at expiry, but the bot's state still says ARMED_SELL
    with own_avg_entry set — needs cleanup, not silent deletion.

    Returns (safe, reason).
    """
    try:
        state = store.get_state(tenant, pid) or {}
    except Exception:
        return False, "store.get_state raised"
    sleeves = state.get("sleeves") or {}
    for sid, ss in sleeves.items():
        if not isinstance(ss, dict):
            continue
        if ss.get("state") == "ARMED_SELL" and ss.get("own_avg_entry"):
            return False, (f"sleeve {sid} shows ARMED_SELL with own_avg_entry="
                            f"{ss.get('own_avg_entry')} — position needs "
                            f"manual reconcile after Coinbase auto-settle")
    return True, "no held positions"
