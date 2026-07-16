"""Fully remove a retired symbol's block from the live tenant.

Adam 2026-07-16: after diag_retire_sleeves and diag_audit_stale_scopes,
the retired symbols (AVE, CHN, HYF, SLR post-retirement) still have
their whole `state[adam-live][SYMBOL]` block in Redis — state, config,
snapshot, etc. The reconciliation monitor keeps scanning them and
generating ghost findings.

This diag removes the entire symbol block from adam-live. Multi-guard
to prevent accidental live-money deletion:

  1. Refuses if broker holds a position on the symbol (__portfolio__)
  2. Refuses if any sleeve is ARMED_BUY / ARMED_SELL / BOUGHT
  3. Refuses if state.last_heartbeat_ts fresher than 15min (active
     trader would clobber the write anyway)
  4. Supports multiple symbols in one invocation

Read-only by default. --apply required to delete. Each deletion is
recorded to trades.jsonl as `retired_symbol_purged`.

Usage:
    python3 diag_clear_retired_symbol.py AVE-20DEC30-CDE           # preview
    python3 diag_clear_retired_symbol.py AVE-20DEC30-CDE --apply   # delete
    python3 diag_clear_retired_symbol.py AVE-20DEC30-CDE HYF-31JUL26-CDE --apply
    python3 diag_clear_retired_symbol.py --all-ghost --apply       # all retired
"""
from __future__ import annotations
import argparse
import os
import sys
import time


TENANT = "adam-live"
FRESH_HEARTBEAT_SECS = 15 * 60.0   # 15 min


def _refuse_reasons(store, sym: str) -> list[str]:
    """Return list of reasons to refuse deletion. Empty list = safe to delete."""
    reasons = []
    # Held position check via __portfolio__ snapshot
    try:
        pf = store.get_state(TENANT, "__portfolio__") or {}
        snap = pf.get(sym) or {}
        if isinstance(snap, dict):
            pq = float(snap.get("position_qty") or 0)
            if pq != 0:
                reasons.append(f"__portfolio__ shows position_qty={pq}")
    except Exception:
        pass
    # State-level guards
    try:
        st = store.get_state(TENANT, sym) or {}
        # Armed sleeves
        sleeves = st.get("sleeves") or {}
        armed = [sid for sid, ss in (sleeves.items() if isinstance(sleeves, dict) else [])
                  if str(ss.get("state") or "") in ("ARMED_BUY", "ARMED_SELL", "BOUGHT")]
        if armed:
            reasons.append(f"active sleeves: {armed}")
        # swing_qty non-zero on state suggests active primary
        try:
            sq = int(st.get("swing_qty") or 0)
            if sq != 0:
                reasons.append(f"state.swing_qty={sq}")
        except (TypeError, ValueError):
            pass
        # Fresh heartbeat means a trader is currently managing this
        hb = st.get("last_heartbeat_ts")
        if hb is not None:
            try:
                age = time.time() - float(hb)
                if age < FRESH_HEARTBEAT_SECS:
                    reasons.append(f"heartbeat age {age:.0f}s < {int(FRESH_HEARTBEAT_SECS)}s "
                                    f"(trader alive; write would be clobbered)")
            except (TypeError, ValueError):
                pass
    except Exception:
        pass
    return reasons


def _find_all_ghost_symbols(store) -> list[str]:
    """Return every adam-live symbol that's fully retired (no config qty,
    no state sleeves, no position). Excludes special __scope__ keys."""
    ghosts = []
    try:
        pf = store.get_state(TENANT, "__portfolio__") or {}
        for sym in store.list_symbols(TENANT):
            if sym.startswith("__"):
                continue
            cfg = store.get_config(TENANT, sym) or {}
            st = store.get_state(TENANT, sym) or {}
            sleeves = st.get("sleeves") or {}
            snap = pf.get(sym) or {}
            pq = float(snap.get("position_qty") or 0) if isinstance(snap, dict) else 0
            if (not sleeves and int(cfg.get("swing_qty") or 0) == 0
                    and int(cfg.get("core_qty") or 0) == 0 and pq == 0):
                # Additionally check no active sleeve state
                is_armed = any(
                    str(ss.get("state") or "") in ("ARMED_BUY", "ARMED_SELL", "BOUGHT")
                    for ss in (sleeves.values() if isinstance(sleeves, dict) else []))
                if not is_armed:
                    ghosts.append(sym)
    except Exception:
        pass
    return ghosts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--all-ghost", action="store_true",
                    help="delete every retired symbol (no config qty, no "
                          "sleeves, no position)")
    ap.add_argument("--data-dir", default=os.getenv("SWING_DATA_DIR", "data"))
    args = ap.parse_args()

    from state_store import make_store
    store = make_store(args.data_dir)

    if args.all_ghost:
        args.symbols = _find_all_ghost_symbols(store)
        if not args.symbols:
            print(f"✓ No ghost symbols on {TENANT}. Nothing to do.")
            return
        print(f"[--all-ghost] Discovered {len(args.symbols)} ghost symbols: {args.symbols}")

    if not args.symbols:
        print("USAGE: python3 diag_clear_retired_symbol.py SYMBOL [SYMBOL ...] [--apply]")
        print("   or: python3 diag_clear_retired_symbol.py --all-ghost [--apply]")
        sys.exit(2)

    print("=" * 90)
    print(f"CLEAR RETIRED SYMBOL {'(APPLY)' if args.apply else '(dry-run)'} "
          f"— tenant={TENANT}")
    print("=" * 90)

    to_delete: list[str] = []
    for sym in args.symbols:
        reasons = _refuse_reasons(store, sym)
        st = store.get_state(TENANT, sym) or {}
        cfg = store.get_config(TENANT, sym) or {}
        has_data = bool(st or cfg)
        print(f"\n· {sym}")
        print(f"    has_state={bool(st)}, has_config={bool(cfg)}")
        if reasons:
            print(f"    ✗ REFUSED — reasons:")
            for r in reasons:
                print(f"        · {r}")
        elif not has_data:
            print(f"    ⚠ SKIP — nothing to delete (no state / no config)")
        else:
            print(f"    ✓ safe to delete")
            to_delete.append(sym)

    if not to_delete:
        print(f"\nNothing to delete.")
        return

    if not args.apply:
        print(f"\n(dry-run — pass --apply to delete {len(to_delete)} symbol(s))")
        return

    # Delete: drop the entire tenant[symbol] block from the store
    def _mutate(fn):
        if hasattr(store, "_mutate"):
            store._mutate(fn)
        else:
            data = store._load()
            fn(data)
            store._save(data)

    try:
        from safety import make_trade_log
        log = make_trade_log(args.data_dir)
    except Exception:
        log = None

    for sym in to_delete:
        _mutate(lambda d, sym=sym: d.get(TENANT, {}).pop(sym, None))
        if log:
            try:
                log.record("retired_symbol_purged",
                            tenant=TENANT, symbol=sym, severity="info",
                            reason="manual purge via diag_clear_retired_symbol.py")
            except Exception:
                pass
        print(f"  ✓ purged {TENANT}/{sym}")

    print(f"\n✓ Purged {len(to_delete)} symbol block(s). Blob is smaller now.")
    print(f"  The reconciliation monitor will stop generating ghost findings")
    print(f"  for these symbols on next tick.")


if __name__ == "__main__":
    main()
