"""Tests for avg_down_signal.average_down_signal — the notification-only
average-down GREEN LIGHT. Green only in the narrow expert-endorsed case
(disciplined scale-in near support in a mean-reverting range); RED in the
dangerous ones (downtrend, active crash, no margin).
Standalone: `PYTHONPATH=. python3 test_avg_down_signal.py`."""
import random
import avg_down_signal as ad


def _calm_range():
    random.seed(7)
    rng = [100.0]
    for _ in range(80):
        pull = (100 - rng[-1]) * 0.3            # mean reversion toward 100
        rng.append(rng[-1] + pull + random.uniform(-2, 2))
    rng[-1] = min(rng)                          # currently at the floor
    return rng


def test_green_calm_range_near_floor():
    rng = _calm_range()
    d = ad.average_down_signal(rng, ms={"vpin": 0.3}, position_avg=102.0,
                               last_price=rng[-1], have_margin=True)
    assert d["light"] == "green" and d["ok"] is True, d
    assert d["checks"]["regime"] == "mean_revert" and d["checks"]["near_floor"], d
    print("[1] calm mean-revert range at floor, below avg, margin -> GREEN")


def test_red_downtrend():
    dt = [200 - i * 1.5 for i in range(60)]
    d = ad.average_down_signal(dt, ms={"vpin": 0.3}, position_avg=200, last_price=dt[-1])
    assert d["light"] == "red", d                # falling knife
    print("[2] downtrend -> RED (falling knife)")


def test_red_toxic_cascade():
    rng = _calm_range()
    d = ad.average_down_signal(rng, ms={"vpin": 0.9, "trade_ofi_60s": -0.85, "obi": -0.8},
                               position_avg=102, last_price=rng[-1])
    assert d["light"] == "red", d
    print("[3] active liquidation cascade -> RED (never add into forced selling)")


def test_red_no_margin():
    rng = _calm_range()
    d = ad.average_down_signal(rng, ms={"vpin": 0.3}, position_avg=102,
                               last_price=rng[-1], have_margin=False)
    assert d["light"] == "red", d
    print("[4] no margin headroom -> RED")


def test_amber_not_below_avg():
    rng = _calm_range()
    d = ad.average_down_signal(rng, ms={"vpin": 0.3}, position_avg=90, last_price=rng[-1])
    assert d["light"] == "amber", d              # nothing to average down
    print("[5] price above your avg -> AMBER (nothing to average down)")


def test_amber_top_of_range():
    rng = _calm_range()
    rng[-1] = max(rng)                           # at the ceiling, not the floor
    d = ad.average_down_signal(rng, ms={"vpin": 0.3}, position_avg=102, last_price=rng[-1])
    assert d["light"] == "amber", d
    print("[6] top of the range (not near floor) -> AMBER")


if __name__ == "__main__":
    test_green_calm_range_near_floor()
    test_red_downtrend()
    test_red_toxic_cascade()
    test_red_no_margin()
    test_amber_not_below_avg()
    test_amber_top_of_range()
    print("\nAVG-DOWN SIGNAL PASSED")
