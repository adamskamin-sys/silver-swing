from state_store import JsonFileStateStore


def test_round_trip_config(tmp_path):
    s = JsonFileStateStore(tmp_path / "store.json")
    s.put_config("adam", "SLR", {"core_qty": 10, "swing_qty": 2})
    assert s.get_config("adam", "SLR") == {"core_qty": 10, "swing_qty": 2}


def test_round_trip_state(tmp_path):
    s = JsonFileStateStore(tmp_path / "store.json")
    s.put_state("adam", "SLR", {"state": "ARMED_SELL", "cycles": 3})
    assert s.get_state("adam", "SLR") == {"state": "ARMED_SELL", "cycles": 3}


def test_config_and_state_are_isolated_scopes(tmp_path):
    s = JsonFileStateStore(tmp_path / "store.json")
    s.put_config("adam", "SLR", {"core_qty": 10})
    s.put_state("adam", "SLR", {"cycles": 5})
    assert s.get_config("adam", "SLR") == {"core_qty": 10}
    assert s.get_state("adam", "SLR") == {"cycles": 5}


def test_missing_key_returns_none(tmp_path):
    s = JsonFileStateStore(tmp_path / "store.json")
    assert s.get_config("nobody", "NOTHING") is None
    assert s.get_state("nobody", "NOTHING") is None


def test_tenant_isolation(tmp_path):
    s = JsonFileStateStore(tmp_path / "store.json")
    s.put_config("adam", "SLR", {"core_qty": 10})
    s.put_config("intruder", "SLR", {"core_qty": 999})
    assert s.get_config("adam", "SLR") == {"core_qty": 10}
    assert s.get_config("intruder", "SLR") == {"core_qty": 999}


def test_symbol_isolation_within_tenant(tmp_path):
    s = JsonFileStateStore(tmp_path / "store.json")
    s.put_config("adam", "SLR", {"core_qty": 10})
    s.put_config("adam", "GOLD", {"core_qty": 5})
    assert s.get_config("adam", "SLR") == {"core_qty": 10}
    assert s.get_config("adam", "GOLD") == {"core_qty": 5}


def test_list_symbols_sorted(tmp_path):
    s = JsonFileStateStore(tmp_path / "store.json")
    s.put_config("adam", "SLR", {})
    s.put_config("adam", "GOLD", {})
    s.put_config("adam", "CL", {})
    assert s.list_symbols("adam") == ["CL", "GOLD", "SLR"]


def test_list_tenants_sorted(tmp_path):
    s = JsonFileStateStore(tmp_path / "store.json")
    s.put_config("adam", "SLR", {})
    s.put_config("charlie", "BTC", {})
    assert s.list_tenants() == ["adam", "charlie"]


def test_survives_new_instance(tmp_path):
    p = tmp_path / "store.json"
    JsonFileStateStore(p).put_config("adam", "SLR", {"core_qty": 10})
    # fresh instance, same file
    assert JsonFileStateStore(p).get_config("adam", "SLR") == {"core_qty": 10}


def test_write_leaves_no_tmp_file(tmp_path):
    s = JsonFileStateStore(tmp_path / "store.json")
    s.put_config("adam", "SLR", {"core_qty": 10})
    assert list(tmp_path.glob("*.tmp")) == []


def test_overwrite_replaces_not_merges(tmp_path):
    """put_config replaces the whole config block; it does NOT deep-merge.
    The dashboard sends the full config every write, so a partial write would
    silently drop fields."""
    s = JsonFileStateStore(tmp_path / "store.json")
    s.put_config("adam", "SLR", {"core_qty": 10, "swing_qty": 2})
    s.put_config("adam", "SLR", {"core_qty": 12})
    assert s.get_config("adam", "SLR") == {"core_qty": 12}


def test_parent_dir_is_created(tmp_path):
    """StateStore should create its parent dir on init, so the first write doesn't fail."""
    JsonFileStateStore(tmp_path / "nested" / "deep" / "store.json").put_config(
        "adam", "SLR", {"core_qty": 10}
    )
    assert (tmp_path / "nested" / "deep" / "store.json").exists()
