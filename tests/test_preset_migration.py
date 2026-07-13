"""Legacy preset-name migration — Model C/D/E → Model B."""

from state_store import JsonFileStateStore
from live_runner import _migrate_stale_preset_names


def test_migration_renames_model_c(tmp_path):
    store = JsonFileStateStore(str(tmp_path / "store"))
    store.put_config("t1", "SLR-27AUG26-CDE", {
        "sleeves": [
            {"id": "s1", "name": "Model C — Microstructure-informed", "qty": 1},
            {"id": "s2", "name": "Model B — Defensive plus (ratchet + reanchor + volatility re-entry)", "qty": 1},
        ]
    })
    n = _migrate_stale_preset_names(store)
    assert n == 1
    cfg = store.get_config("t1", "SLR-27AUG26-CDE")
    assert cfg["sleeves"][0]["name"] == "Model B — Defensive plus (ratchet + reanchor + volatility re-entry)"
    # Second sleeve was already canonical — unchanged
    assert cfg["sleeves"][1]["name"] == "Model B — Defensive plus (ratchet + reanchor + volatility re-entry)"


def test_migration_is_idempotent(tmp_path):
    store = JsonFileStateStore(str(tmp_path / "store"))
    store.put_config("t1", "SLR", {
        "sleeves": [{"id": "s1", "name": "Model D — News-aware", "qty": 1}]
    })
    n1 = _migrate_stale_preset_names(store)
    n2 = _migrate_stale_preset_names(store)
    assert n1 == 1
    assert n2 == 0  # second pass finds nothing to rename


def test_migration_skips_meta_keys(tmp_path):
    store = JsonFileStateStore(str(tmp_path / "store"))
    store.put_config("t1", "__portfolio__", {"cash": {"total": 1000}})
    store.put_config("t1", "SLR", {"sleeves": [
        {"id": "s1", "name": "Model E — Kitchen sink", "qty": 1}
    ]})
    n = _migrate_stale_preset_names(store)
    assert n == 1
    # __portfolio__ untouched
    pf = store.get_config("t1", "__portfolio__")
    assert pf == {"cash": {"total": 1000}}


def test_migration_handles_no_sleeves_gracefully(tmp_path):
    store = JsonFileStateStore(str(tmp_path / "store"))
    store.put_config("t1", "SLR", {})  # no sleeves field
    n = _migrate_stale_preset_names(store)
    assert n == 0
