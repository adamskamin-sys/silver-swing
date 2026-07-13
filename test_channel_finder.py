"""Tests for channel_finder.find_channel — locate a NEW swing channel after a
significant drop. Standalone: `PYTHONPATH=. python3 test_channel_finder.py`."""
import channel_finder as cf


def test_finds_new_channel_after_drop():
    old = [500 + (2 if i % 2 else -2) for i in range(20)]
    drop = [500, 492, 483, 470, 458, 449, 443]
    newch = [440 + (2 if i % 2 else -2) for i in range(20)]
    d = cf.find_channel(old + drop + newch, atr=4.0)
    assert d["broke"] is True, d
    assert d["stabilized"] is True, d
    assert 435 <= d["center"] <= 445, d              # centers on the NEW range, not old 500
    assert d["buy_px"] < d["center"] < d["sell_px"], d
    assert d["lower"] >= d["floor"] - 1e-6 and d["upper"] <= d["ceiling"] + 1e-6, d
    print("[1] finds the new channel after a drop; targets bracket the new center")


def test_no_break_steady_channel():
    steady = [500 + (3 if i % 2 else -3) for i in range(40)]
    d = cf.find_channel(steady, atr=4.0)
    assert d["broke"] is False and 495 <= d["center"] <= 505, d
    print("[2] steady channel -> no break, center holds")


def test_mid_crash_waits():
    falling = [500 - i * 3 for i in range(40)]
    d = cf.find_channel(falling, atr=4.0)
    assert d["broke"] is True and d["stabilized"] is False, d
    print("[3] mid-crash -> break detected but not stabilized -> wait")


def test_thin_history_safe():
    d = cf.find_channel([500, 499, 501], atr=4.0)
    assert d["broke"] is False and d["stabilized"] is False, d
    print("[4] thin history -> no action")


if __name__ == "__main__":
    test_finds_new_channel_after_drop()
    test_no_break_steady_channel()
    test_mid_crash_waits()
    test_thin_history_safe()
    print("\nCHANNEL-FINDER PASSED")
