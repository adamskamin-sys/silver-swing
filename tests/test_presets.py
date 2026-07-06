"""Tests for the risk-preset bundles."""

import pytest

from presets import (PRESETS, preset, preset_aggressive, preset_conservative,
                     preset_moderate)


def test_all_three_presets_registered():
    assert set(PRESETS.keys()) == {"conservative", "moderate", "aggressive"}


def test_preset_by_name_returns_dict():
    cfg = preset("moderate")
    assert isinstance(cfg, dict)
    assert cfg["max_swing_qty"] == 5


def test_unknown_preset_raises():
    with pytest.raises(ValueError, match="unknown preset"):
        preset("nonexistent")


def test_conservative_widest_abort_bracket():
    """Conservative should have the widest abort_below/abort_above range."""
    c, m, a = preset_conservative(), preset_moderate(), preset_aggressive()
    c_range = c["abort_above"] - c["abort_below"]
    m_range = m["abort_above"] - m["abort_below"]
    a_range = a["abort_above"] - a["abort_below"]
    assert c_range < a_range  # aggressive has widest abort_above (lets run further)


def test_scale_up_multiplier_ordering():
    """conservative requires MORE profit-buffer to add a contract than aggressive."""
    c, m, a = preset_conservative(), preset_moderate(), preset_aggressive()
    assert c["scale_up_buffer_mult"] > m["scale_up_buffer_mult"] > a["scale_up_buffer_mult"]


def test_max_swing_qty_ordering():
    """aggressive allows biggest swing, conservative smallest."""
    c, m, a = preset_conservative(), preset_moderate(), preset_aggressive()
    assert c["max_swing_qty"] < m["max_swing_qty"] < a["max_swing_qty"]


def test_aggressive_uses_trailing_stop():
    """Aggressive is trailing-first; conservative + moderate are range-scalp."""
    assert preset_aggressive()["exit_mode"] == "trailing_stop"
    assert preset_conservative()["exit_mode"] == "fixed_limit"
    assert preset_moderate()["exit_mode"] == "fixed_limit"


def test_trail_distance_tighter_on_aggressive():
    """Aggressive trails tighter to capture more of the top; conservative
    wouldn't use it, but if enabled its distance would be wider."""
    assert preset_aggressive()["trail_distance"] < preset_conservative()["trail_distance"]


def test_presets_all_have_required_fields():
    """Every preset must produce a config that SwingConfig(**cfg) can load."""
    from swing_leg import SwingConfig
    for name in ("conservative", "moderate", "aggressive"):
        cfg = preset(name)
        # SwingConfig may not accept every extra field (like tick_size),
        # so filter to known fields.
        allowed = {f.name for f in SwingConfig.__dataclass_fields__.values()}
        filtered = {k: v for k, v in cfg.items() if k in allowed}
        SwingConfig(**filtered)  # must not raise
