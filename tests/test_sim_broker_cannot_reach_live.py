"""
test_sim_broker_cannot_reach_live.py — MERGE GATE for WS3 (remove-paper-lab).

Proves the backtest/sim broker can NEVER: place a live order, write a live-tenant
scope, derive a live tenant, or start the live engine — even when handed the EXACT
env that caused the 2026-07-14 multi-writer incident (a "paper" service running the
live engine on adam-live).

If ANY layer trips, the sim has a reachable live path and the WS3 merge must be
blocked. Wire the CONFIG block to your real symbols and run:

    pytest tests/test_sim_broker_cannot_reach_live.py -v

ALL tests must PASS (green) for WS3 to merge. Add it to CI so it re-runs on every
change — this property must hold forever, not just at cutover.
"""
import importlib
import inspect
import pytest

# ============================ CONFIG — wired to real code =================== #
# Values below reflect the actual silver-swing repo (verified by Explore agent
# 2026-07-14). Update ONLY if the underlying module/class/method is renamed.

SIM_BROKER_MODULE  = "sim_broker"                # fresh module we own (B2 path)
SIM_BROKER_CLASS   = "SimBroker"

# Live order client is a METHOD on CoinbaseBroker (not a module-level fn).
# We patch the class attr — any instance created after the patch calls tripwire.
LIVE_CLIENT_MODULE = "broker"                    # holds CoinbaseBroker
LIVE_ORDER_FUNC    = "CoinbaseBroker.place_limit"  # dotted → walk in _mod_attr
LIVE_MARKET_FUNC   = "CoinbaseBroker.place_market"  # also guard the market path

# Redis-backed store writes go through the store class methods.
STORE_MODULE       = "state_store"
STORE_WRITE_FUNC   = "RedisJsonStore.put_state"    # tenant scope write path (state)
STORE_CONFIG_FUNC  = "RedisJsonStore.put_config"   # tenant scope write path (config)

# The derive-live-tenant footgun that caused the 2026-07-14 incident.
DERIVE_LIVE_MODULE = "main"
DERIVE_LIVE_FUNC   = "_derive_live_tenant"

# Backtest entrypoint — signature is run_backtest(trader_factory, paper_config, candles).
BACKTEST_MODULE    = "backtest"
BACKTEST_ENTRY     = "run_backtest"

LIVE_TENANTS = ("adam-live",)                    # any tenant string that is "live"

# The 2026-07-14 incident env: a paper service that ran the live engine on
# the derived live tenant.
INCIDENT_ENV = {
    "SWING_TENANT": "adam-paper", "SWING_MODE": "paper",
    "SWING_LIVE_ENGINE": "1", "SWING_LIVE_CONFIRM": "I_UNDERSTAND",
}
FULLY_LIVE_ENV = {
    "SWING_TENANT": "adam-live", "SWING_MODE": "live",
    "SWING_LIVE_ENGINE": "1", "SWING_LIVE_CONFIRM": "I_UNDERSTAND",
}


def _make_sim_broker():
    """Instantiate SimBroker with a minimal SimConfig sufficient for tick()."""
    from sim_broker import SimConfig
    cls = getattr(importlib.import_module(SIM_BROKER_MODULE), SIM_BROKER_CLASS)
    return cls(SimConfig(
        product_id="NOL-20JUL26-CDE",
        contract_size=10.0, tick_size=0.01,
        fee_per_fill=1.0, margin_per_contract=100.0,
        starting_balance=10000.0,
    ))


def _run_sim_cycle(broker):
    """A backtest's buy→sell. Must hit ONLY in-memory sim state.
    Note: SimBroker.place_limit signature is (side, qty, price[, post_only]) —
    no `symbol` param, because the broker is instantiated per-product."""
    broker.place_limit("BUY", 1, 74.0)
    broker.tick(74.0, 74.05)                       # marches sim; fills the buy
    broker.place_limit("SELL", 1, 75.0)
    broker.tick(75.0, 75.05)                       # marches sim; fills the sell
# =========================================================================== #


class LivePathReached(AssertionError):
    """A tripwire fired — the sim reached a live path. Merge is blocked."""


def _mod_attr(module, name):
    """Return (parent_object, final_attr_name) so setattr can patch.
    Supports dotted `name` like 'ClassName.method' to walk the attribute chain."""
    mod = importlib.import_module(module)
    parts = name.split(".")
    parent = mod
    for p in parts[:-1]:
        parent = getattr(parent, p)
    return parent, parts[-1]


# --- Layer 1: the sim must never call the real order client ---------------- #
def test_sim_never_calls_live_place_limit(monkeypatch):
    parent, name = _mod_attr(LIVE_CLIENT_MODULE, LIVE_ORDER_FUNC)
    def tripwire(*a, **k):
        raise LivePathReached("SimBroker reached CoinbaseBroker.place_limit")
    monkeypatch.setattr(parent, name, tripwire)
    _run_sim_cycle(_make_sim_broker())              # must not raise


def test_sim_never_calls_live_place_market(monkeypatch):
    parent, name = _mod_attr(LIVE_CLIENT_MODULE, LIVE_MARKET_FUNC)
    def tripwire(*a, **k):
        raise LivePathReached("SimBroker reached CoinbaseBroker.place_market")
    monkeypatch.setattr(parent, name, tripwire)
    b = _make_sim_broker()
    b.place_market("BUY", 1)                        # sim's own place_market
    b.tick(74.0, 74.05)                             # must not raise


# --- Layer 2: the sim must never write a LIVE-tenant scope ----------------- #
def test_sim_never_writes_a_live_tenant_scope_via_put_state(monkeypatch):
    parent, name = _mod_attr(STORE_MODULE, STORE_WRITE_FUNC)
    def guarded(self, tenant, *a, **k):
        if any(lt in str(tenant) for lt in LIVE_TENANTS):
            raise LivePathReached(f"SimBroker wrote via put_state to live tenant: {tenant}")
        return None
    monkeypatch.setattr(parent, name, guarded)
    _run_sim_cycle(_make_sim_broker())              # must not raise


def test_sim_never_writes_a_live_tenant_scope_via_put_config(monkeypatch):
    parent, name = _mod_attr(STORE_MODULE, STORE_CONFIG_FUNC)
    def guarded(self, tenant, *a, **k):
        if any(lt in str(tenant) for lt in LIVE_TENANTS):
            raise LivePathReached(f"SimBroker wrote via put_config to live tenant: {tenant}")
        return None
    monkeypatch.setattr(parent, name, guarded)
    _run_sim_cycle(_make_sim_broker())              # must not raise


# --- Layer 3: the sim must never derive a live tenant --------------------- #
def test_sim_never_derives_a_live_tenant(monkeypatch):
    parent, name = _mod_attr(DERIVE_LIVE_MODULE, DERIVE_LIVE_FUNC)
    def tripwire(*a, **k):
        raise LivePathReached("SimBroker reached _derive_live_tenant")
    monkeypatch.setattr(parent, name, tripwire)
    _run_sim_cycle(_make_sim_broker())              # must not raise


# --- Layer 4: adversarial env — live env must NOT open a live path -------- #
@pytest.mark.parametrize("env,label",
                         [(INCIDENT_ENV, "incident"), (FULLY_LIVE_ENV, "fully-live")])
def test_sim_ignores_live_env(monkeypatch, env, label):
    """Even with the live env vars set (the 2026-07-14 incident configuration),
    running the sim broker's normal cycle must not reach any live path."""
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    # Arm all three tripwires simultaneously.
    lp, ln = _mod_attr(LIVE_CLIENT_MODULE, LIVE_ORDER_FUNC)
    monkeypatch.setattr(lp, ln, lambda *a, **k:
                        (_ for _ in ()).throw(LivePathReached(f"live order from sim [{label}]")))
    dp, dn = _mod_attr(DERIVE_LIVE_MODULE, DERIVE_LIVE_FUNC)
    monkeypatch.setattr(dp, dn, lambda *a, **k:
                        (_ for _ in ()).throw(LivePathReached(f"derive_live from sim [{label}]")))
    # Adversarial run: full sim cycle under live env vars.
    _run_sim_cycle(_make_sim_broker())              # must not raise


# --- Layer 5: structural — the sim module can't even reference the live client - #
def test_sim_module_does_not_reference_live_client_module():
    """Static check: sim_broker.py source must not import `broker` at all.
    The auditor's Layer-5 tripwire: 'if your SimBroker legitimately needs
    shared types from that module, split them into a neutral module.'"""
    src = inspect.getsource(importlib.import_module(SIM_BROKER_MODULE))
    # Strip comment lines to avoid false positives on docstring mentions.
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert f"import {LIVE_CLIENT_MODULE}" not in code, (
        f"{SIM_BROKER_MODULE}.py imports {LIVE_CLIENT_MODULE} — a live-client "
        f"import path exists. The sim broker must have NO way to reach the "
        f"live order client.")
    assert f"from {LIVE_CLIENT_MODULE}" not in code, (
        f"{SIM_BROKER_MODULE}.py imports FROM {LIVE_CLIENT_MODULE} — same problem.")
    # Also assert no state_store import (would let sim write tenant scopes).
    assert f"import {STORE_MODULE}" not in code, (
        f"{SIM_BROKER_MODULE}.py imports {STORE_MODULE} — could reach live scopes.")
    assert f"from {STORE_MODULE}" not in code, (
        f"{SIM_BROKER_MODULE}.py imports FROM {STORE_MODULE} — same problem.")
