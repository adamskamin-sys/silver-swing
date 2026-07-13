"""Tick recording — write every ticker/trade/L2 event to disk for later replay.

Foundation for order-book replay backtesting and future ML training data.
Gated by SWING_TICK_RECORDING=1 env var (default OFF — no I/O overhead
unless explicitly enabled). Records to append-only JSONL files rotated
daily to keep individual files < ~50MB / day.

Path convention:
    data/ticks/YYYY-MM-DD/<symbol>.jsonl

Each line is one JSON event:
    {"kind": "ticker", "ts": 1234567890.123, "bid": 100.5, "ask": 100.6, "price": 100.55}
    {"kind": "trade",  "ts": ..., "price": 100.55, "size": 5, "side": "buy"}
    {"kind": "l2_snap","ts": ..., "bids": [[100.5, 10], ...], "asks": [...]}
    {"kind": "l2_upd", "ts": ..., "side": "b", "price": 100.5, "size": 8}

Why JSONL not Parquet: append-only writes are trivial with JSONL and
survive process crashes without corrupting existing data. A separate
consolidation script can convert to Parquet nightly if scale demands it.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def enabled() -> bool:
    """Check the env gate. Cheap enough to call per tick — os.getenv reads
    from the process's env dict, no syscall."""
    v = os.getenv("SWING_TICK_RECORDING", "").strip().lower()
    return v in ("1", "true", "yes", "on")


class TickRecorder:
    """Per-symbol daily-rotating JSONL writer. Callers own the instance;
    on_ticker/on_trade/on_l2_snapshot/on_l2_update forward to disk when
    the gate is enabled, no-op otherwise.

    Rotation is by UTC date (not local). Fresh file at 00:00 UTC.
    """

    def __init__(self, symbol: str, base_dir: str = "data/ticks"):
        self.symbol = symbol
        self.base_dir = Path(base_dir)
        self._current_date: Optional[str] = None
        self._fh = None

    def _path_for(self, when: float) -> Path:
        d = datetime.fromtimestamp(when, tz=timezone.utc).strftime("%Y-%m-%d")
        return self.base_dir / d / f"{self.symbol}.jsonl"

    def _ensure_open(self, when: float) -> None:
        d = datetime.fromtimestamp(when, tz=timezone.utc).strftime("%Y-%m-%d")
        if d == self._current_date and self._fh is not None:
            return
        # Rotate: close prior file, open today's (create dir tree if needed).
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
        p = self._path_for(when)
        p.parent.mkdir(parents=True, exist_ok=True)
        self._fh = p.open("a", buffering=1)  # line-buffered
        self._current_date = d

    def _write(self, event: dict) -> None:
        if not enabled():
            return
        try:
            when = float(event.get("ts") or time.time())
            self._ensure_open(when)
            if self._fh is None:
                return
            self._fh.write(json.dumps(event, separators=(",", ":")) + "\n")
        except Exception:
            # Never let a recording failure kill the trade path.
            pass

    def on_ticker(self, best_bid: float, best_ask: float, price: float,
                  ts: Optional[float] = None) -> None:
        self._write({
            "kind": "ticker",
            "ts": ts if ts is not None else time.time(),
            "bid": best_bid, "ask": best_ask, "price": price,
        })

    def on_trade(self, price: float, size: float, side: Optional[str],
                 ts: Optional[float] = None) -> None:
        self._write({
            "kind": "trade",
            "ts": ts if ts is not None else time.time(),
            "price": price, "size": size, "side": side,
        })

    def on_l2_snapshot(self, bids: list, asks: list,
                       ts: Optional[float] = None) -> None:
        self._write({
            "kind": "l2_snap",
            "ts": ts if ts is not None else time.time(),
            "bids": [[float(p), float(s)] for p, s in bids][:50],
            "asks": [[float(p), float(s)] for p, s in asks][:50],
        })

    def on_l2_update(self, side: str, price: float, new_size: float,
                     ts: Optional[float] = None) -> None:
        self._write({
            "kind": "l2_upd",
            "ts": ts if ts is not None else time.time(),
            "side": side, "price": price, "size": new_size,
        })

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
