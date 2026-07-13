"""Wiring tests for the crash-guard / cascade re-entry gate / reversal shadow /
roll-blackout (crew). Standalone: `PYTHONPATH=. python3 test_crash_reversal_wiring.py`.

Covers the pieces that live in swing_leg + sleeves (not the pure signal math,
which test_cascade_state.py / test_reversal_suite.py already cover):
  - SleeveConfig round-trips crash_guard_enabled + reversal_enabled (the toggle
    must actually persist onto the config the engine reads — regression for the
    bug where the dashboard toggle was silently dropped in from_dict).
  - the exact re-entry gate predicate used in _sleeve_step.
  - crash_guard computes the shadow flip direction only when reversal is on.
  - the near-expiry roll blackout: suppresses ONLY when we affirmatively know
    we're inside the window; fail-safe (guard stays active) on unknown expiry.
"""
import time
from datetime import datetime, timezone

import dataclasses
import sleeves
import cascade_state
import crash_guard


def _blocked(c):
    # the precise predicate _sleeve_step uses to veto a rebuy
    return c.get("phase") == "crashing" or c.get("second_leg_risk")


def test_toggle_persistence():
    base = {"id": "s1", "name": "S1", "qty": 1}
    d = sleeves.SleeveConfig.from_dict(base)
    assert d.crash_guard_enabled is False and d.reversal_enabled is False, "defaults must be OFF"
    on = sleeves.SleeveConfig.from_dict({**base, "crash_guard_enabled": True, "reversal_enabled": True})
    assert on.crash_guard_enabled is True and on.reversal_enabled is True
    rt = sleeves.SleeveConfig.from_dict(dataclasses.asdict(on))  # full round-trip
    assert rt.crash_guard_enabled is True and rt.reversal_enabled is True
    print("[1] toggle persistence + defaults OK")


def test_reentry_gate_predicate():
    calm = [{"price": 100 + (i % 2) * 0.01, "vpin": 0.2, "ofi": 0.0, "vol": 0.0005} for i in range(30)]
    assert not _blocked(cascade_state.assess(calm))
    crash = [{"price": 100 - i * 0.5, "vpin": 0.85, "ofi": -0.8, "vol": 0.02} for i in range(20)]
    assert _blocked(cascade_state.assess(crash))
    bounce = ([{"price": 100 - i * 0.9, "vpin": 0.6 + i * 0.03, "ofi": -0.8, "vol": 0.02} for i in range(8)]
              + [{"price": 92.8 + (i % 2) * 0.05, "vpin": 0.82, "ofi": -0.6, "vol": 0.018} for i in range(8)]
              + [{"price": 92.9 + i * 0.12, "vpin": 0.62, "ofi": -0.45, "vol": 0.015} for i in range(8)])
    assert _blocked(cascade_state.assess(bounce)), "dead-cat bounce must block re-entry"
    assert not _blocked(cascade_state.assess([{"price": 100, "vpin": 0.2} for _ in range(3)])), "thin history permissive"
    print("[2] re-entry gate: calm/thin->allow, crash+bounce->block")


def test_shadow_flip_direction():
    ms, rets = {"vpin": 0.9, "trade_ofi_60s": -0.85, "obi": -0.8}, [-0.02] * 25
    on = crash_guard.crash_assessment(ms, rets, "LONG", {"guard_enabled": True, "flip_enabled": True})
    assert on["action"] == "FLATTEN_AND_FLIP" and on["flip_to"] == "SHORT"
    off = crash_guard.crash_assessment(ms, rets, "LONG", {"guard_enabled": True, "flip_enabled": False})
    assert off["action"] == "FLATTEN" and off.get("flip_to") is None, "reversal off must be defensive-only"
    print("[3] shadow flip computed only when reversal enabled; else FLATTEN-only")


def test_roll_blackout():
    # imported here so the signal tests above still run even if swing_leg's
    # runtime deps (alerting/strategies) are absent in a bare scratch env.
    from swing_leg import SwingTrader

    def iso(secs_from_now):
        return datetime.fromtimestamp(time.time() + secs_from_now, tz=timezone.utc).isoformat()

    class FakeBroker:
        def __init__(self, exp): self._exp = exp
        def contract_spec(self): return {"contract_expiry": self._exp}

    def mk(hours, exp, broker=None):
        t = object.__new__(SwingTrader)
        t._roll_guard_blackout_hours = hours
        t._roll_expiry_ts = None
        t._roll_expiry_checked = 0.0
        t.b = broker if broker is not None else FakeBroker(exp)
        return t

    t = object.__new__(SwingTrader)
    assert t._parse_expiry(None) is None and t._parse_expiry("garbage") is None
    exp = datetime(2026, 8, 27, 16, 0, 0, tzinfo=timezone.utc).timestamp()
    assert abs(t._parse_expiry("2026-08-27T16:00:00Z") - exp) < 1
    assert abs(t._parse_expiry(int(exp * 1000)) - exp) < 1  # ms epoch tolerated

    assert mk(0, iso(3600))._within_roll_blackout() is False          # disabled by default
    assert mk(4, iso(2 * 3600))._within_roll_blackout() is True       # inside window
    assert mk(4, iso(10 * 3600))._within_roll_blackout() is False     # outside window
    assert mk(4, None)._within_roll_blackout() is False               # unknown -> active (fail-safe)
    assert mk(4, iso(-5 * 86400))._within_roll_blackout() is False    # stale past -> active

    class Dumb: pass
    assert mk(4, None, broker=Dumb())._within_roll_blackout() is False  # paper broker -> active
    print("[4] roll blackout: suppress only inside known window; fail-safe otherwise")


if __name__ == "__main__":
    test_toggle_persistence()
    test_reentry_gate_predicate()
    test_shadow_flip_direction()
    try:
        test_roll_blackout()
    except ImportError as e:
        print(f"[4] roll blackout SKIPPED (swing_leg deps missing in this env: {e})")
    print("\nCRASH/REVERSAL WIRING PASSED")
