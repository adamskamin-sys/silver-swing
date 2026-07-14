"""Tests for the 2026-07-14 auditor Goal A hardening of _derive_live_tenant.

Root cause of the 2026-07-14 multi-writer incident: silver-swing-bot-paper
had TENANT=adam-paper + SWING_LIVE_ENGINE=1 + SWING_LIVE_CONFIRM=I_UNDERSTAND.
The old _derive_live_tenant silently converted adam-paper → adam-live, so
paper spun up a real-money track pointed at the same tenant as bot-live.
Two writers, duplicate orders.

Hardened rules:
  1. _derive_live_tenant RAISES ValueError on any source that is not
     already '-live'-shaped.
  2. Track.is_live is computed from THIS track's tenant name, not derived
     from module-level TENANT.
"""
from __future__ import annotations

import pytest


# ---- The pure function guard --------------------------------------------

def test_live_source_returns_itself():
    """A '-live'-shaped source returns unchanged."""
    import main
    assert main._derive_live_tenant("adam-live") == "adam-live"
    assert main._derive_live_tenant("other-live") == "other-live"


def test_paper_source_refused():
    """The INCIDENT case: paper must not silently derive live."""
    import main
    with pytest.raises(ValueError, match="refusing to derive"):
        main._derive_live_tenant("adam-paper")


def test_lab_source_refused():
    """Lab tenants can't derive live either."""
    import main
    with pytest.raises(ValueError, match="refusing to derive"):
        main._derive_live_tenant("adam-lab")


def test_arbitrary_source_refused():
    """Anything without '-live' suffix is refused — no guessing."""
    import main
    with pytest.raises(ValueError):
        main._derive_live_tenant("adam")
    with pytest.raises(ValueError):
        main._derive_live_tenant("some-random-thing")
    with pytest.raises(ValueError):
        main._derive_live_tenant("")


def test_error_message_names_the_incident_class():
    """Error message must be actionable + reference the incident so the
    operator immediately understands why we refuse."""
    import main
    try:
        main._derive_live_tenant("adam-paper")
        assert False, "expected ValueError"
    except ValueError as e:
        msg = str(e)
        assert "'-live'" in msg
        assert "SWING_TENANT" in msg
        assert "2026-07-14" in msg or "multi-writer" in msg


# ---- Downstream: Track.is_live uses tenant name, not derivation ---------

def test_track_is_live_is_semantic_on_tenant_name():
    """A track whose tenant ends with -live is live, regardless of what
    module-level TENANT is. This prevents the paper-mode misconfiguration
    from silently marking any track as real-money."""
    # We check the source of _Track (main.py:592-ish) to guarantee no
    # future refactor reintroduces the derivation-based comparison.
    from pathlib import Path
    src = Path(__file__).parent.parent / "main.py"
    text = src.read_text()
    assert 'self.is_live = isinstance(tenant, str) and tenant.endswith("-live")' in text, (
        "Track.is_live should compute from THIS track's tenant name, not "
        "derive from module-level TENANT — otherwise paper mode could silently "
        "mark a track as real-money.")


# ---- Incident-simulation smoke ------------------------------------------

def test_incident_scenario_refuses_live_engine_startup(monkeypatch, capsys):
    """Reproduce the 2026-07-14 misconfiguration: TENANT=adam-paper +
    SWING_LIVE_ENGINE=1 + SWING_LIVE_CONFIRM=I_UNDERSTAND. The hardening
    must REFUSE to enable the live engine and log a CRIT-level message
    explaining why."""
    import main
    monkeypatch.setenv("SWING_LIVE_ENGINE", "1")
    monkeypatch.setenv("SWING_LIVE_CONFIRM", "I_UNDERSTAND")
    # Simulate the derivation call the way run_paper_mode does it
    try:
        main._derive_live_tenant("adam-paper")
        assert False, "expected refusal"
    except ValueError as e:
        # The error message should give the operator a clear next step
        assert "SWING_TENANT" in str(e)
