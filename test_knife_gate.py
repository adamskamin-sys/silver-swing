"""Tests for knife_gate.knife_gate — the entry velocity / falling-knife gate.
Velocity (Lee-Mykland jump) self-scales per instrument; forced flow needs a
consensus of factors; slow movers fill at target.
Standalone: `PYTHONPATH=. python3 test_knife_gate.py`."""
import knife_gate as kg


def test_slow_drift_fills():
    d = kg.knife_gate([-0.0004] * 25, ms={"vpin": 0.3})
    assert d["block"] is False, d          # copper-like slow drift -> buy at target
    print("[1] slow orderly drift -> DON'T block (fills at target)")


def test_velocity_jump_blocks():
    series = [0.0005 * ((-1) ** i) for i in range(24)] + [-0.05]   # -5% vs ~0.05% noise
    d = kg.knife_gate(series, ms={"vpin": 0.3})
    assert d["block"] is True and d["velocity"] and d["velocity"] >= 4, d
    print("[2] sharp jump-down (velocity) -> BLOCK")


def test_forced_flow_blocks():
    d = kg.knife_gate([-0.002] * 25, ms={"vpin": 0.85, "trade_ofi_60s": -0.8, "obi": -0.7})
    assert d["block"] is True, d            # >=2 flow factors = a forced drop
    print("[3] toxic + persistent + depleted flow -> BLOCK (forced drop)")


def test_single_soft_factor_no_overgate():
    d = kg.knife_gate([-0.002] * 25, ms={"vpin": 0.72, "trade_ofi_60s": -0.2, "obi": -0.1})
    assert d["block"] is False, d           # one moderate reading must not over-gate
    print("[4] single soft factor -> DON'T block (needs consensus)")


def test_no_data_fail_safe():
    assert kg.knife_gate([], ms=None)["block"] is False
    print("[5] no data -> fail-safe, DON'T block")


def test_up_jump_not_a_knife():
    series = [0.0005 * ((-1) ** i) for i in range(24)] + [0.05]    # fast RISE
    assert kg.knife_gate(series, ms={"vpin": 0.3})["block"] is False
    print("[6] up-jump (fast rise) -> not a down-knife, DON'T block")


if __name__ == "__main__":
    test_slow_drift_fills()
    test_velocity_jump_blocks()
    test_forced_flow_blocks()
    test_single_soft_factor_no_overgate()
    test_no_data_fail_safe()
    test_up_jump_not_a_knife()
    print("\nKNIFE-GATE PASSED")
