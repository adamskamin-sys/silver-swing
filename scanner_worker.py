"""Shared scanner-tick worker used by both the paper and live loops.

Adam retired bot-paper on Render, but the scanner previously only lived
in run_paper_mode's loop — so the Add/Edit Strategy modal's BEST tiles
never populated. This module wraps the scan-cadence + force_include logic
so live_runner can call it too, keeping tiles fresh regardless of which
worker(s) are up.

State (last-run timestamps, cached Coinbase client) lives on the instance
so callers just do `worker.tick()` on every main-loop iteration.
"""

import os
import time


def _log(msg: str) -> None:
    print(f"[scanner_worker] {msg}", flush=True)


class ScannerWorker:
    def __init__(self, store, redis_url: str | None, symbol_hint: str):
        self.store = store
        self.redis_url = redis_url
        self.symbol_hint = symbol_hint  # used for lazy CoinbaseBroker init
        self.last_scanner = 0.0
        self.last_scanner_auto = 0.0
        self.scanner_interval = float(os.getenv("SWING_SCANNER_INTERVAL", "30.0"))
        self.scanner_auto_interval = float(os.getenv("SWING_SCANNER_AUTO_INTERVAL", "900.0"))
        self._coinbase_client = None
        self._logged_no_redis = False
        # Startup log so we can see in bot-live's logs whether Redis is wired.
        # Without REDIS_URL the tick returns silently; Adam would just see
        # "scanning..." spin forever with no obvious cause.
        if redis_url:
            _log(f"initialized with Redis, symbol_hint={symbol_hint}, "
                 f"scanner_interval={self.scanner_interval}s, "
                 f"scanner_auto_interval={self.scanner_auto_interval}s")
        else:
            _log("initialized WITHOUT Redis — set REDIS_URL env var to enable "
                 "scanner tile refresh. Tick() will be a no-op.")

    def tick(self) -> None:
        """One scanner check — either a full scan or a no-op depending on
        rate gates. Safe to call every main-loop iteration; internally
        capped by scanner_interval (30s floor)."""
        if not self.redis_url:
            if not self._logged_no_redis:
                _log("tick: no REDIS_URL — skipping scanner refresh forever")
                self._logged_no_redis = True
            return
        now = time.time()
        if now - self.last_scanner < self.scanner_interval:
            return
        try:
            from scanner import (
                fetch_and_rank, write_ranking_to_redis,
                check_and_clear_refresh_request,
            )
            requested = check_and_clear_refresh_request(self.redis_url)
            auto_due = (self.scanner_auto_interval > 0
                        and now - self.last_scanner_auto >= self.scanner_auto_interval)
            if not (requested or auto_due):
                return
            if self._coinbase_client is None:
                from broker import BrokerConfig, CoinbaseBroker
                self._coinbase_client = CoinbaseBroker(
                    BrokerConfig(product_id=self.symbol_hint)
                ).client
            forced = self._gather_forced()
            trigger = "user request" if requested else "auto interval"
            _log(f"running one scan ({trigger}, force_include={sorted(forced)})")
            ranking = fetch_and_rank(
                self._coinbase_client, top_n=10,
                force_include=list(forced),
            )
            write_ranking_to_redis(self.redis_url, ranking, generated_at=now)
            self.last_scanner = now
            if auto_due or requested:
                # User request counts as a fresh auto tick so the next
                # auto interval starts from the just-completed scan.
                self.last_scanner_auto = now
        except Exception as e:
            _log(f"scanner refresh failed: {type(e).__name__}: {e}")
            self.last_scanner = now  # back off on repeated failure

    def _gather_forced(self) -> set[str]:
        forced: set[str] = set()
        # Every product Adam has an active strategy on so the Edit modal's
        # "Recommended spreads" tiles always populate. Without this, low-24h-
        # range products (that Adam is still actively swinging) drop off the
        # top-N and their modal shows "no scanner data".
        try:
            for t in self.store.list_tenants():
                for sym in self.store.list_symbols(t):
                    if sym.startswith("__"):
                        continue
                    cfg = self.store.get_config(t, sym) or {}
                    if cfg.get("sleeves") or cfg.get("swing_qty"):
                        forced.add(sym)
        except Exception as e:
            _log(f"force-include gather failed: {type(e).__name__}: {e}")
        # Every product Adam actually holds a live futures position in —
        # even without a strategy attached yet. Newly-bought contracts
        # (via scanner-buy or Coinbase directly) don't have sleeves yet,
        # so without this they'd drop out of the scan and the Add
        # Strategy modal would open with no tiles.
        try:
            if self._coinbase_client is not None:
                resp = self._coinbase_client.list_futures_positions()
                positions = (resp.to_dict() if hasattr(resp, "to_dict") else resp).get("positions") or []
                for p in positions:
                    pid = p.get("product_id")
                    if pid:
                        forced.add(pid)
        except Exception as e:
            _log(f"live position gather failed: {type(e).__name__}: {e}")
        # Explicit include list(s) from the dashboard (Add Strategy modal
        # for a brand-new product without an existing sleeve). Uses a
        # Redis SET so successive Scan-Now clicks accumulate instead of
        # overwriting. Consumed once per scan (deleted after read).
        try:
            import redis as _redis
            _r = _redis.from_url(self.redis_url)
            members = _r.smembers("silver-swing:scanner:refresh_include_set") or set()
            for m in members:
                pid = m.decode() if isinstance(m, bytes) else str(m)
                pid = pid.strip()
                if pid:
                    forced.add(pid)
            if members:
                _r.delete("silver-swing:scanner:refresh_include_set")
            # Legacy string key from an earlier deploy — read + delete once
            # so any stale requests still count.
            legacy = _r.get("silver-swing:scanner:refresh_include")
            if legacy:
                legacy = legacy.decode() if isinstance(legacy, bytes) else legacy
                for pid in str(legacy).split(","):
                    pid = pid.strip()
                    if pid:
                        forced.add(pid)
                _r.delete("silver-swing:scanner:refresh_include")
        except Exception as e:
            _log(f"refresh_include read failed: {type(e).__name__}: {e}")
        return forced
