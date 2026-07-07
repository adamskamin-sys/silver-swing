"""News event calendar for the news-blackout feature.

Silver's price reacts strongly to macro announcements — FOMC decisions,
CPI, NFP, and Fed speeches routinely cause $0.30–$1.50 moves in 30 seconds.
A swing bot sitting through these gets whipsawed. Van Tharp, Cartea-
Jaimungal, and empirical futures studies all agree: reducing exposure
around scheduled announcements is one of the few free wins available to
retail systems.

This module is deliberately simple:
  - SCHEDULED_EVENTS is a hardcoded list of upcoming high-impact events
    (edit as new ones become known; NFP dates are deterministic; FOMC
    dates are set by the Fed and published a year in advance).
  - blackout_for(now) returns the current active window (if any) as a
    dict {name, start_ts, end_ts, tier}, else None.
  - Each event has a `tier` (1=tighten trail, 2=pause new arms,
    3=full exit any position). The sleeve config chooses which tier
    it respects.
  - Times are stored as UTC epoch seconds so the bot doesn't have to
    reason about timezones.

Timezone reference: FOMC statements at 2:00 PM ET, CPI/NFP at 8:30 AM ET.
Blackout window = event_ts - 900 (15 min before) to event_ts + 1800
(30 min after) by default.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from typing import Optional


# Blackout window defaults (seconds relative to event announcement time).
BLACKOUT_BEFORE_SECS = 15 * 60   # 15 min before
BLACKOUT_AFTER_SECS = 30 * 60    # 30 min after


def _et_to_utc(year: int, month: int, day: int, et_hour: int, et_minute: int = 0) -> float:
    """Convert an Eastern Time announcement to a UTC epoch second.

    US ET is UTC-4 during DST (mid-March through early November) and
    UTC-5 otherwise. This heuristic covers the 2026 dates we care about.
    For a bot running long enough to hit the transition, verify against
    the actual DST calendar; the two-day risk of getting the offset wrong
    is that a blackout fires an hour early or late.
    """
    # US DST 2026: March 8 → November 1 (both inclusive)
    dst_start = datetime(year, 3, 8, tzinfo=timezone.utc)
    dst_end = datetime(year, 11, 1, tzinfo=timezone.utc)
    naive = datetime(year, month, day, et_hour, et_minute)
    if dst_start <= datetime(year, month, day, tzinfo=timezone.utc) <= dst_end:
        offset_hours = 4  # EDT
    else:
        offset_hours = 5  # EST
    return naive.replace(tzinfo=timezone.utc).timestamp() + offset_hours * 3600


# Known high-impact events. Tiers:
#   3 — full exit (major FOMC, high-tier CPI)
#   2 — pause new arms hold existing (secondary announcements, minutes)
#   1 — tighten trail only (regional Fed, housing data)
#
# Edit this list as more dates get announced. Times are Eastern (converted
# to UTC internally). Fed press conferences are typically 30 min after the
# statement — merged into the same blackout window for simplicity.
SCHEDULED_EVENTS: list[dict] = [
    # FOMC decisions — Fed publishes dates a year in advance
    {"name": "FOMC Jul 2026 decision", "ts": _et_to_utc(2026, 7, 29, 14, 0), "tier": 3},
    {"name": "FOMC Sep 2026 decision", "ts": _et_to_utc(2026, 9, 16, 14, 0), "tier": 3},
    {"name": "FOMC Oct 2026 decision", "ts": _et_to_utc(2026, 10, 28, 14, 0), "tier": 3},
    {"name": "FOMC Dec 2026 decision", "ts": _et_to_utc(2026, 12, 9, 14, 0), "tier": 3},

    # Monthly Nonfarm Payrolls (NFP) — first Friday of each month, 8:30 AM ET
    {"name": "NFP Aug 2026", "ts": _et_to_utc(2026, 8, 7, 8, 30), "tier": 3},
    {"name": "NFP Sep 2026", "ts": _et_to_utc(2026, 9, 4, 8, 30), "tier": 3},
    {"name": "NFP Oct 2026", "ts": _et_to_utc(2026, 10, 2, 8, 30), "tier": 3},
    {"name": "NFP Nov 2026", "ts": _et_to_utc(2026, 11, 6, 8, 30), "tier": 3},

    # Monthly CPI — released ~10th-15th, 8:30 AM ET (dates approximated)
    {"name": "CPI Jul 2026", "ts": _et_to_utc(2026, 7, 15, 8, 30), "tier": 3},
    {"name": "CPI Aug 2026", "ts": _et_to_utc(2026, 8, 12, 8, 30), "tier": 3},
    {"name": "CPI Sep 2026", "ts": _et_to_utc(2026, 9, 11, 8, 30), "tier": 3},
    {"name": "CPI Oct 2026", "ts": _et_to_utc(2026, 10, 14, 8, 30), "tier": 3},

    # PPI — day after CPI, 8:30 AM ET
    {"name": "PPI Jul 2026", "ts": _et_to_utc(2026, 7, 16, 8, 30), "tier": 2},
    {"name": "PPI Aug 2026", "ts": _et_to_utc(2026, 8, 13, 8, 30), "tier": 2},
    {"name": "PPI Sep 2026", "ts": _et_to_utc(2026, 9, 12, 8, 30), "tier": 2},

    # ISM Manufacturing — first business day of month, 10 AM ET
    {"name": "ISM Aug 2026", "ts": _et_to_utc(2026, 8, 3, 10, 0), "tier": 2},
    {"name": "ISM Sep 2026", "ts": _et_to_utc(2026, 9, 1, 10, 0), "tier": 2},
    {"name": "ISM Oct 2026", "ts": _et_to_utc(2026, 10, 1, 10, 0), "tier": 2},
]


def blackout_for(now: Optional[float] = None,
                 before_secs: float = BLACKOUT_BEFORE_SECS,
                 after_secs: float = BLACKOUT_AFTER_SECS) -> Optional[dict]:
    """Return the currently-active blackout event, or None. If two events
    overlap (rare — CPI + PPI back-to-back), returns the higher-tier one."""
    now = now if now is not None else time.time()
    active = []
    for ev in SCHEDULED_EVENTS:
        start = ev["ts"] - before_secs
        end = ev["ts"] + after_secs
        if start <= now <= end:
            active.append({
                "name": ev["name"],
                "start_ts": start,
                "end_ts": end,
                "tier": int(ev["tier"]),
            })
    if not active:
        return None
    # Return highest-tier when multiple overlap
    return max(active, key=lambda e: e["tier"])


def next_event(now: Optional[float] = None) -> Optional[dict]:
    """Nearest upcoming event, for dashboard display."""
    now = now if now is not None else time.time()
    upcoming = [ev for ev in SCHEDULED_EVENTS if ev["ts"] > now]
    if not upcoming:
        return None
    nxt = min(upcoming, key=lambda e: e["ts"])
    return {
        "name": nxt["name"],
        "ts": nxt["ts"],
        "tier": int(nxt["tier"]),
        "secs_until": nxt["ts"] - now,
    }
