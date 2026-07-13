"""Tests for short_manager.cover_decision — the reversal short's cover brain,
encoding Adam's break-even rule. Standalone: `PYTHONPATH=. python3 test_short_manager.py`."""
import short_manager as sm

CS = 1.0  # $ per point (ZEC-like); short entered at 500, price FALLING = profit.


def test_hold_while_running():
    d = sm.cover_decision(500, 480, 1, CS, realized_banked=200, peak_low=480, atr=10,
                          cfg={"arm_x_atr": 0.75, "stop_x_atr": 1.5, "trail_x_atr": 1.0})
    assert d["action"] == "HOLD", d
    print("[1] falling/in-profit -> HOLD")


def test_protect_realized_lock():
    d = sm.cover_decision(500, 490, 1, CS, realized_banked=200, peak_low=480, atr=10,
                          cfg={"arm_x_atr": 0.75, "lock_frac": 0.5, "stop_x_atr": 1.5, "trail_x_atr": 99})
    assert d["action"] == "COVER" and d["trigger"] == "protect_realized", d
    print("[2] retrace past lock -> COVER (protect-realized)")


def test_break_even_guarantee():
    # armed short (was profitable) drifts back to entry -> covers at >= break-even, never a loss
    d = sm.cover_decision(500, 500, 1, CS, realized_banked=200, peak_low=485, atr=10,
                          cfg={"arm_x_atr": 0.75, "lock_frac": 0.0, "stop_x_atr": 1.5, "trail_x_atr": 99})
    assert d["action"] == "COVER" and d["u_now"] >= 0, d
    print("[3] armed short back to entry -> COVER at u_now >= 0 (break-even guaranteed)")


def test_hard_stop_caps_loss():
    d = sm.cover_decision(500, 516, 1, CS, realized_banked=200, peak_low=500, atr=10,
                          cfg={"arm_x_atr": 0.75, "stop_x_atr": 1.5})
    assert d["action"] == "COVER" and d["trigger"] == "hard_stop", d
    print("[4] immediate adverse -> hard stop caps loss")


def test_realized_cap_tightens_stop():
    # banked only $8; realized cap (8 pts) is tighter than the atr stop (15 pts)
    d = sm.cover_decision(500, 509, 1, CS, realized_banked=8, peak_low=500, atr=10,
                          cfg={"arm_x_atr": 0.75, "stop_x_atr": 1.5, "protect_realized": True})
    assert d["action"] == "COVER" and "realized-capped" in d["reason"], d
    off = sm.cover_decision(500, 509, 1, CS, realized_banked=8, peak_low=500, atr=10,
                            cfg={"arm_x_atr": 0.75, "stop_x_atr": 1.5, "protect_realized": False})
    assert off["action"] == "HOLD", off
    print("[5] loss capped at banked realized (and not when protect off)")


def test_trailing_continuation():
    d = sm.cover_decision(500, 470, 1, CS, realized_banked=0, peak_low=460, atr=10,
                          cfg={"arm_x_atr": 99, "lock_frac": 0.5, "stop_x_atr": 99,
                               "trail_x_atr": 1.0, "trail_enabled": True})
    assert d["action"] == "COVER" and d["trigger"] == "trail", d
    print("[6] trailing continuation -> COVER")


def test_no_atr_fractional():
    d = sm.cover_decision(500, 511, 1, CS, realized_banked=1000, peak_low=500, atr=None,
                          cfg={"stop_x_atr": 0.02, "arm_x_atr": 0.01})
    assert d["action"] == "COVER", d
    print("[7] no-ATR mode: x_atr read as price fraction")


if __name__ == "__main__":
    test_hold_while_running()
    test_protect_realized_lock()
    test_break_even_guarantee()
    test_hard_stop_caps_loss()
    test_realized_cap_tightens_stop()
    test_trailing_continuation()
    test_no_atr_fractional()
    print("\nSHORT-MANAGER PASSED")
