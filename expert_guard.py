"""Expert-params drift guard (crew).

Answers the operator's question: "is the bot ACTUALLY using the expert data we
compiled when it figures buy/sell prices — or has the live config drifted off
it?" It recomputes the expected levels from the current ATR (Layer 1,
expert_params) plus the Layer-2 tuned trail multiplier cached under
__tuned_params__, then compares them to the LIVE config's actual trail / stop /
reanchor / ratchet levels. If any level has drifted beyond a tolerance, it
alerts through the existing notifier and journals it to the trade log.

Design notes:
  - READ-ONLY. It never changes config or places orders. It only observes and
    alerts. A wrong config is the human's to fix (or re-tune).
  - Fail-safe: when the data it needs is missing (no ATR, no config, unknown
    field names), it SKIPS that symbol and reports ok=True rather than
    false-alarming. Better silent than crying wolf.
  - Meant to be called periodically from live_runner's main loop (e.g. every
    few minutes, alongside the snapshot/reconcile cadence).

Field-name assumption: the live swing-config stores the derived levels under the
same keys expert_params() returns (trail_distance, reanchor_threshold, ...).
Those two are confirmed (see expert_tuner._cfg_for_grid); the rest are compared
only when present in BOTH the config and the expert output, so an unknown schema
degrades to "checked fewer fields", never to a false alert.
"""

from __future__ import annotations

from typing import Optional

import expert_params


# expert_params() output key -> the config key it should match.
_PARAM_MAP = {
    "trail_distance": "trail_distance",
    "stop_loss_distance": "stop_loss_distance",
    "reanchor_threshold": "reanchor_threshold",
    "ratchet_distance": "ratchet_distance",
    "ratchet_activation": "ratchet_activation",
    "buy_trail_distance": "buy_trail_distance",
    "trail_activation_offset": "trail_activation_offset",
}


def _tuned_for(store, tenant: str, symbol: str) -> Optional[dict]:
    """Layer-2 tuned params for this symbol, cached under __tuned_params__.
    The tuner may persist under the state or config scope — try both."""
    for getter in ("get_state", "get_config"):
        fn = getattr(store, getter, None)
        if not fn:
            continue
        try:
            blob = fn(tenant, "__tuned_params__") or {}
        except Exception:
            blob = {}
        tp = blob.get(symbol) if isinstance(blob, dict) else None
        if isinstance(tp, dict) and (tp.get("atr") or tp.get("trail_x_atr")):
            return tp
    return None


def _current_atr(store, tenant: str, symbol: str, tuned: Optional[dict]):
    """Best-available ATR the config's levels SHOULD be based on. Prefer the
    daily tuner's ATR, then a snapshot-stored ATR, then a config-stored ATR."""
    if tuned and tuned.get("atr"):
        return float(tuned["atr"]), "tuned"
    try:
        snap = (store.get_snapshot(tenant, symbol) or {}) if hasattr(store, "get_snapshot") else {}
    except Exception:
        snap = {}
    if snap.get("atr"):
        return float(snap["atr"]), "snapshot"
    cfg = store.get_config(tenant, symbol) or {}
    if cfg.get("atr"):
        return float(cfg["atr"]), "config"
    return None, None


def check(store, tenant: str, symbol: str, tolerance_pct: float = 10.0) -> dict:
    """Drift report for one symbol. Skips (ok=True) when data is missing."""
    cfg = store.get_config(tenant, symbol) or {}
    if not cfg:
        return {"symbol": symbol, "ok": True, "skipped": "no config", "drifts": []}
    tuned = _tuned_for(store, tenant, symbol)
    atr, atr_src = _current_atr(store, tenant, symbol, tuned)
    if not atr or atr <= 0:
        return {"symbol": symbol, "ok": True, "skipped": "no ATR available", "drifts": []}

    expected = dict(expert_params.expert_params(symbol, atr))
    using_tuned = bool(tuned and tuned.get("trail_x_atr"))
    if using_tuned:
        # Layer-2 overrides the trail multiplier with the per-product tuned one.
        expected["trail_distance"] = round(atr * float(tuned["trail_x_atr"]), 4)

    drifts = []
    checked = []
    for ek, ck in _PARAM_MAP.items():
        if ek not in expected or ck not in cfg:
            continue
        try:
            exp = float(expected[ek])
            act = float(cfg.get(ck) or 0)
        except (TypeError, ValueError):
            continue
        checked.append(ck)
        if exp <= 0:
            continue
        pct_off = abs(act - exp) / exp * 100.0
        if pct_off > tolerance_pct:
            drifts.append({
                "param": ck,
                "expected": round(exp, 4),
                "actual": round(act, 4),
                "pct_off": round(pct_off, 1),
            })
    return {
        "symbol": symbol,
        "ok": not drifts,
        "atr": atr,
        "atr_source": atr_src,
        "using_tuned": using_tuned,
        "checked": checked,
        "drifts": drifts,
    }


def run_guard(store, tenant: str, symbols, notifier=None, trade_log=None,
              tolerance_pct: float = 10.0) -> list[dict]:
    """Check every symbol; journal + alert on drift. Returns all reports.

    Wire into live_runner's loop, e.g.:
        if now - last_expert_guard >= EXPERT_GUARD_SECS:
            last_expert_guard = now
            try:
                import expert_guard
                expert_guard.run_guard(store, TENANT, store.list_symbols(TENANT),
                                       notifier=notifier, trade_log=log)
            except Exception as e:
                _log(f"expert_guard failed: {type(e).__name__}: {e}")
    """
    reports = []
    for sym in symbols:
        if sym.startswith("__"):
            continue
        try:
            r = check(store, tenant, sym, tolerance_pct)
        except Exception as e:
            r = {"symbol": sym, "ok": True, "error": f"{type(e).__name__}: {e}", "drifts": []}
        reports.append(r)
        if r.get("drifts"):
            msg = "; ".join(
                f"{d['param']}: config {d['actual']} vs expert {d['expected']} ({d['pct_off']}% off)"
                for d in r["drifts"]
            )
            if trade_log is not None:
                try:
                    trade_log.record("expert_params_drift", tenant=tenant, symbol=sym,
                                     atr=r.get("atr"), drifts=r["drifts"])
                except Exception:
                    pass
            if notifier is not None:
                try:
                    from alerting import Priority
                    notifier.send(
                        f"expert-params drift: {sym}",
                        (f"{sym} is NOT tracking its expert data within {tolerance_pct:.0f}%:\n"
                         f"{msg}\n(ATR {r.get('atr')} via {r.get('atr_source')}, "
                         f"tuned={r.get('using_tuned')})"),
                        Priority.HIGH,
                    )
                except Exception:
                    pass
    return reports
