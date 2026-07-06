"""
LiveTickerFeed — Coinbase Advanced Trade WebSocket ticker adapter (spec §12, live-paper enabler).

Wraps `coinbase.websocket.WSClient`'s ticker channel and exposes a simple
`latest_ticker() -> {price, best_bid, best_ask, ts}` interface. The WS runs in
its own thread inside the SDK; the caller polls latest_ticker() from the main
loop. Non-blocking, auto-reconnecting (via WSClient's `retry=True`).

Usage:

    with LiveTickerFeed("SLR-27AUG26-CDE") as feed:
        feed.wait_for_first_tick()   # block until we have data
        while running:
            t = feed.latest_ticker()
            if t is not None:
                paper_broker.tick(t["best_bid"], t["best_ask"])
                trader.step(t["price"])
            time.sleep(loop_interval)

Message-parsing is the piece that most needs testing — WebSocket connectivity
itself is Coinbase's SDK's problem, not ours. Tests exercise `_on_message`
directly with synthetic Coinbase-shaped payloads.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Optional

from dotenv import load_dotenv


class LiveTickerFeed:
    """Latest-ticker-only view of the Coinbase ticker WebSocket channel."""

    def __init__(
        self,
        product_id: str,
        key_file: Optional[str] = None,
        ws_client=None,
    ):
        self.product_id = product_id
        self._latest: Optional[dict] = None
        self._lock = threading.Lock()
        self._started = False

        if ws_client is not None:
            # Injected client (tests, or a pre-configured shared instance)
            self._ws = ws_client
            return
        # Delayed import so tests that never touch a real WS don't need SDK connectivity
        from coinbase.websocket import WSClient
        load_dotenv()
        kf = key_file or os.getenv("COINBASE_API_KEY_JSON_PATH")
        if not kf:
            raise ValueError("no key file: pass key_file= or set COINBASE_API_KEY_JSON_PATH")
        self._ws = WSClient(
            key_file=kf,
            on_message=self._on_message,
            retry=True,
        )

    # ---- message parsing (the interesting, testable part) ----------------

    def _on_message(self, msg_str: str) -> None:
        """Parse an incoming WS message. Ticker events update `latest`; other
        channels are ignored. Malformed messages never propagate — a bad line
        must not kill the receiver."""
        try:
            msg = json.loads(msg_str)
        except (ValueError, TypeError):
            return
        if not isinstance(msg, dict):
            return
        if msg.get("channel") != "ticker":
            return
        events = msg.get("events") or []
        for event in events:
            tickers = event.get("tickers") or []
            for t in tickers:
                if t.get("product_id") != self.product_id:
                    continue
                # Coinbase key names have varied historically — accept both
                # `best_bid`/`bid` and `best_ask`/`ask`. `price` is standard.
                try:
                    price = float(t.get("price") or 0)
                    bid = float(t.get("best_bid") or t.get("bid") or 0)
                    ask = float(t.get("best_ask") or t.get("ask") or 0)
                except (ValueError, TypeError):
                    return
                if price <= 0 and bid <= 0 and ask <= 0:
                    return
                with self._lock:
                    self._latest = {
                        "product_id": self.product_id,
                        "price": price,
                        "best_bid": bid,
                        "best_ask": ask,
                        "ts": msg.get("timestamp") or t.get("timestamp"),
                    }

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self._ws.open()
        self._ws.ticker(product_ids=[self.product_id])
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        try:
            self._ws.ticker_unsubscribe(product_ids=[self.product_id])
        except Exception:
            pass
        try:
            self._ws.close()
        except Exception:
            pass
        self._started = False

    def __enter__(self) -> "LiveTickerFeed":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # ---- read -----------------------------------------------------------

    def latest_ticker(self) -> Optional[dict]:
        with self._lock:
            return dict(self._latest) if self._latest is not None else None

    def wait_for_first_tick(self, timeout: float = 10.0, poll_interval: float = 0.05) -> bool:
        """Block until latest_ticker() returns non-None, or timeout expires.
        Returns True if data arrived, False on timeout. Useful for a caller
        that wants to be certain it has real data before starting to trade."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.latest_ticker() is not None:
                return True
            time.sleep(poll_interval)
        return False
