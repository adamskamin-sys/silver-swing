"""Component health tracker for background tick jobs (crew).

Purpose
-------
Silent failures in periodic background jobs (reconcile, expert_guard,
portfolio_risk_tick, spec_refresh, portfolio_refresh, front_month,
live_portfolio_sync, discover_symbols, scanner_refresh) used to log
to stderr and be forgotten. Downstream code kept reading stale values
without any surface signal — the 2026-07-13 "portfolio position=0
despite 19 contracts" bug was one instance of this class.

This module gives every background job a durable health record + a
trade-log event on failure, so the daily audit can see when a component
has been silently degrading.

Contract
--------
Two calls only, both **never-raise** (per auditor 2026-07-14 review:
"wrap instrumentation defensively so a health-emit failure can't break
the wrapped site"):

    record_ok(store, "reconcile", tenant)
    record_error(store, "reconcile", tenant, exc, trade_log=log)

State lives in the __health__ scope of the tenant's store:
    {
      "components": {
        "reconcile": {"last_ok_ts": <epoch>, "last_error_ts": <epoch>,
                      "last_error_type": "TimeoutError",
                      "last_error_message": "<truncated>"},
        ...
      }
    }

Trade-log events are named `<component>_error` (e.g. `reconcile_error`)
for compact greppability. Payload: tenant, error_type, error_message.
"""
from __future__ import annotations

import time
from typing import Optional


SCOPE = "__health__"


def record_ok(store, component: str, tenant: str) -> None:
    """Called after a successful tick of `component`. Updates __health__
    scope with the last_ok_ts. Never raises — health writes must not
    take down the wrapped site."""
    try:
        state = store.get_state(tenant, SCOPE) or {}
        comps = state.get("components") or {}
        comp = comps.get(component) or {}
        comp["last_ok_ts"] = time.time()
        comps[component] = comp
        state["components"] = comps
        store.put_state(tenant, SCOPE, state)
    except Exception:
        pass


def record_error(store, component: str, tenant: str, exc: Exception,
                 trade_log: Optional[object] = None) -> None:
    """Called on a failed tick of `component`. Updates __health__ scope
    AND records a `<component>_error` event to the trade log for the
    daily audit. Never raises."""
    now = time.time()
    err_type = type(exc).__name__
    err_msg = str(exc)[:500]
    try:
        state = store.get_state(tenant, SCOPE) or {}
        comps = state.get("components") or {}
        comp = comps.get(component) or {}
        comp["last_error_ts"] = now
        comp["last_error_type"] = err_type
        comp["last_error_message"] = err_msg
        comps[component] = comp
        state["components"] = comps
        store.put_state(tenant, SCOPE, state)
    except Exception:
        pass
    if trade_log is not None:
        try:
            trade_log.record(
                f"{component}_error",
                tenant=tenant,
                error_type=err_type,
                error_message=err_msg,
            )
        except Exception:
            pass


def get_health(store, tenant: str) -> dict:
    """Read helper for the daily audit + cockpit chip. Returns the
    __health__.components dict, or {} if unavailable."""
    try:
        state = store.get_state(tenant, SCOPE) or {}
        return dict(state.get("components") or {})
    except Exception:
        return {}
