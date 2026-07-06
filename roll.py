"""
Roll handling near expiry (spec §9B).

Dated contracts (SLR-27AUG26-CDE) have a fixed expiry. Holding through
expiration is not a thing you want to happen accidentally — the position
either settles or gets forced out with unfavorable behavior. Roll before it
does: close the near contract, open the same qty on the next month.

MVP scope of this file (deliberately narrow):
  - detect when the active contract is within roll_days_before of expiry
  - identify the next available dated contract from the venue
  - HALT the strategy and alert the human with roll instructions

We do NOT auto-execute the roll here. Live rolls have edge cases (fills
happen at different times for the close vs. the open, margin fluctuates
mid-roll, spreads widen) and the safety cost of getting it wrong outweighs
the convenience. Manual with clear instructions until we've done enough of
them by hand to trust the automation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional


@dataclass
class RollDetection:
    active_symbol: str
    active_expiry: datetime
    days_to_expiry: float
    should_roll: bool
    next_symbol: Optional[str]
    next_expiry: Optional[datetime]

    def summary(self) -> str:
        if not self.should_roll:
            return f"{self.active_symbol}: {self.days_to_expiry:.1f} days to expiry — no roll yet"
        if self.next_symbol:
            return (
                f"ROLL {self.active_symbol} → {self.next_symbol}: "
                f"{self.days_to_expiry:.1f} days to expiry, "
                f"next contract expires {self.next_expiry.isoformat() if self.next_expiry else '?'}"
            )
        return f"ROLL {self.active_symbol}: within window but no next contract found"


def _parse_expiry(iso_str: str) -> Optional[datetime]:
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def check_roll(
    coinbase_broker,
    active_symbol: str,
    roll_days_before: int = 5,
    now: Optional[datetime] = None,
) -> RollDetection:
    """Query Coinbase, compute time to expiry for the active contract, and
    identify the next dated contract for the same family.

    `coinbase_broker` must be a `CoinbaseBroker` instance (real, not paper).
    Everything's a read — no orders touched.
    """
    now = now or datetime.now(timezone.utc)

    # Grab all futures products; filter to the same contract family (SLR- for silver, etc)
    resp = coinbase_broker.client.get_products(product_type="FUTURE", get_all_products=True)
    resp_d = resp.to_dict() if hasattr(resp, "to_dict") else resp
    products = resp_d.get("products", []) or []

    family = _contract_family(active_symbol)
    same_family = [p for p in products if _contract_family(p.get("product_id") or "") == family]

    active = next((p for p in same_family if p.get("product_id") == active_symbol), None)
    if active is None:
        return RollDetection(active_symbol, now, 0.0, False, None, None)
    active_expiry = _parse_expiry(
        ((active.get("future_product_details") or {}).get("contract_expiry")) or ""
    )
    if active_expiry is None:
        return RollDetection(active_symbol, now, 0.0, False, None, None)
    days_left = (active_expiry - now).total_seconds() / 86400.0
    should_roll = days_left <= roll_days_before

    # Find next contract: same family, later expiry, closest to active
    candidates: list[tuple[float, str, datetime]] = []
    for p in same_family:
        if p.get("product_id") == active_symbol:
            continue
        exp = _parse_expiry(((p.get("future_product_details") or {}).get("contract_expiry")) or "")
        if exp is None or exp <= active_expiry:
            continue
        candidates.append(((exp - active_expiry).total_seconds(), p.get("product_id"), exp))
    candidates.sort()
    next_symbol = candidates[0][1] if candidates else None
    next_expiry = candidates[0][2] if candidates else None

    return RollDetection(
        active_symbol=active_symbol,
        active_expiry=active_expiry,
        days_to_expiry=days_left,
        should_roll=should_roll,
        next_symbol=next_symbol,
        next_expiry=next_expiry,
    )


def _contract_family(symbol: str) -> str:
    """Extract the family prefix from a product_id (e.g., SLR-27AUG26-CDE → SLR).

    Also handles perp shapes like SILVER-PERP-INTX → SILVER-PERP (not a roll
    candidate, but doesn't crash the check)."""
    if not symbol:
        return ""
    if "-PERP-" in symbol:
        return symbol.split("-PERP-")[0] + "-PERP"
    parts = symbol.split("-")
    return parts[0] if parts else ""
