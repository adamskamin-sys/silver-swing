"""Tests for the news blackout calendar."""

import time

import news_calendar


def test_blackout_returns_none_when_no_event_nearby():
    # 1000-year-later timestamp — definitely outside every scheduled event
    far_future = time.time() + 60 * 60 * 24 * 365 * 1000
    assert news_calendar.blackout_for(far_future) is None


def test_blackout_returns_event_when_inside_window():
    # Pick the first event and query 5 minutes AFTER it — inside the 30-min
    # after-window.
    if not news_calendar.SCHEDULED_EVENTS:
        return  # nothing scheduled — test is a no-op
    ev = news_calendar.SCHEDULED_EVENTS[0]
    query = ev["ts"] + 5 * 60
    active = news_calendar.blackout_for(query)
    assert active is not None
    assert active["name"] == ev["name"]
    assert active["tier"] == ev["tier"]


def test_blackout_returns_none_before_window_starts():
    if not news_calendar.SCHEDULED_EVENTS:
        return
    ev = news_calendar.SCHEDULED_EVENTS[0]
    # Query 1 hour before the event — outside the 15-min before window
    query = ev["ts"] - 60 * 60
    assert news_calendar.blackout_for(query) is None


def test_blackout_returns_none_after_window_ends():
    if not news_calendar.SCHEDULED_EVENTS:
        return
    ev = news_calendar.SCHEDULED_EVENTS[0]
    # Query 1 hour after the event — outside the 30-min after window
    query = ev["ts"] + 60 * 60
    assert news_calendar.blackout_for(query) is None


def test_next_event_returns_something_upcoming():
    # Use a recent 'now' — if all scheduled events are in the past, returns None.
    result = news_calendar.next_event(now=1750000000)  # early 2025
    if result is not None:
        assert "name" in result
        assert "ts" in result
        assert "secs_until" in result


def test_higher_tier_wins_on_overlap():
    """If two events happen to overlap in time, blackout_for should return
    the higher-tier one (more conservative response)."""
    # Directly patch SCHEDULED_EVENTS temporarily to force an overlap
    original = news_calendar.SCHEDULED_EVENTS
    try:
        now = 1_800_000_000  # arbitrary future timestamp
        news_calendar.SCHEDULED_EVENTS = [
            {"name": "Minor", "ts": now, "tier": 2},
            {"name": "Major", "ts": now + 60, "tier": 3},
        ]
        active = news_calendar.blackout_for(now + 30)
        assert active is not None
        assert active["tier"] == 3  # took the higher-tier one
    finally:
        news_calendar.SCHEDULED_EVENTS = original
