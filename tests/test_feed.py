"""Tests for LiveTickerFeed — exercises _on_message directly with synthetic
Coinbase-shaped payloads. No real WebSocket connections are opened."""

import json
import threading
from unittest.mock import MagicMock

import pytest

from feed import LiveTickerFeed


PRODUCT = "SLR-27AUG26-CDE"


def make_feed(product=PRODUCT):
    """Feed with an injected mock WSClient — no real connection."""
    return LiveTickerFeed(product, ws_client=MagicMock())


def ticker_msg(product_id=PRODUCT, price="62.80", best_bid="62.755", best_ask="62.765"):
    return json.dumps({
        "channel": "ticker",
        "timestamp": "2026-07-06T03:00:00Z",
        "sequence_num": 42,
        "events": [{
            "type": "update",
            "tickers": [{
                "type": "ticker",
                "product_id": product_id,
                "price": price,
                "best_bid": best_bid,
                "best_ask": best_ask,
            }],
        }],
    })


# ---- message parsing --------------------------------------------------------


def test_parses_valid_ticker_message():
    f = make_feed()
    f._on_message(ticker_msg())
    t = f.latest_ticker()
    assert t is not None
    assert t["product_id"] == PRODUCT
    assert t["price"] == 62.80
    assert t["best_bid"] == 62.755
    assert t["best_ask"] == 62.765
    assert "ts" in t


def test_ignores_non_ticker_channels():
    f = make_feed()
    other = json.dumps({
        "channel": "heartbeats",
        "events": [{"heartbeat_counter": 1}],
    })
    f._on_message(other)
    assert f.latest_ticker() is None


def test_ignores_ticker_for_other_products():
    f = make_feed(PRODUCT)
    f._on_message(ticker_msg(product_id="BTC-USD"))
    assert f.latest_ticker() is None


def test_malformed_json_does_not_crash():
    f = make_feed()
    f._on_message("this is not json at all")
    f._on_message('{"channel": "ticker", "events":')  # truncated
    f._on_message("")
    assert f.latest_ticker() is None


def test_missing_events_does_not_crash():
    f = make_feed()
    f._on_message(json.dumps({"channel": "ticker"}))  # no events
    f._on_message(json.dumps({"channel": "ticker", "events": []}))
    assert f.latest_ticker() is None


def test_non_dict_payload_ignored():
    f = make_feed()
    f._on_message("[]")
    f._on_message("42")
    f._on_message("null")
    assert f.latest_ticker() is None


def test_accepts_bid_ask_alias_names():
    """Coinbase's key names have shifted historically — support 'bid'/'ask' too."""
    f = make_feed()
    msg = json.dumps({
        "channel": "ticker",
        "events": [{
            "tickers": [{
                "product_id": PRODUCT,
                "price": "62.80",
                "bid": "62.755",     # not best_bid
                "ask": "62.765",     # not best_ask
            }],
        }],
    })
    f._on_message(msg)
    t = f.latest_ticker()
    assert t is not None
    assert t["best_bid"] == 62.755
    assert t["best_ask"] == 62.765


def test_all_zero_ticker_ignored():
    """A ticker with no useful price info shouldn't clobber previous state."""
    f = make_feed()
    f._on_message(ticker_msg())
    good = f.latest_ticker()
    f._on_message(json.dumps({
        "channel": "ticker",
        "events": [{"tickers": [{"product_id": PRODUCT, "price": "0", "best_bid": "0", "best_ask": "0"}]}],
    }))
    # Latest should still be the good one
    assert f.latest_ticker() == good


# ---- lifecycle --------------------------------------------------------------


def test_start_opens_and_subscribes():
    ws = MagicMock()
    f = LiveTickerFeed(PRODUCT, ws_client=ws)
    f.start()
    assert ws.open.called
    ws.ticker.assert_called_once_with(product_ids=[PRODUCT])


def test_start_is_idempotent():
    ws = MagicMock()
    f = LiveTickerFeed(PRODUCT, ws_client=ws)
    f.start(); f.start()
    assert ws.open.call_count == 1
    assert ws.ticker.call_count == 1


def test_stop_unsubscribes_and_closes():
    ws = MagicMock()
    f = LiveTickerFeed(PRODUCT, ws_client=ws)
    f.start(); f.stop()
    ws.ticker_unsubscribe.assert_called_once_with(product_ids=[PRODUCT])
    assert ws.close.called


def test_stop_without_start_is_safe():
    ws = MagicMock()
    f = LiveTickerFeed(PRODUCT, ws_client=ws)
    f.stop()
    assert not ws.ticker_unsubscribe.called
    assert not ws.close.called


def test_context_manager_starts_and_stops():
    ws = MagicMock()
    with LiveTickerFeed(PRODUCT, ws_client=ws):
        assert ws.open.called
    assert ws.close.called


# ---- wait_for_first_tick ----------------------------------------------------


def test_wait_for_first_tick_returns_true_when_available():
    f = make_feed()
    f._on_message(ticker_msg())
    assert f.wait_for_first_tick(timeout=1.0) is True


def test_wait_for_first_tick_times_out_when_no_data():
    f = make_feed()
    assert f.wait_for_first_tick(timeout=0.1) is False


def test_wait_for_first_tick_returns_when_message_arrives_late():
    f = make_feed()

    def deliver_message():
        import time as t
        t.sleep(0.1)
        f._on_message(ticker_msg())

    thread = threading.Thread(target=deliver_message)
    thread.start()
    result = f.wait_for_first_tick(timeout=1.0)
    thread.join()
    assert result is True


# ---- read isolation --------------------------------------------------------


def test_latest_ticker_returns_copy_not_reference():
    """A caller mutating the returned dict shouldn't affect the stored latest."""
    f = make_feed()
    f._on_message(ticker_msg())
    t1 = f.latest_ticker()
    t1["price"] = -999
    t2 = f.latest_ticker()
    assert t2["price"] == 62.80


# ---- construction -----------------------------------------------------------


def test_construction_requires_key_when_no_env(monkeypatch):
    """If no ws_client is injected, we need a key file or COINBASE_API_KEY_JSON_PATH."""
    import feed as feed_mod
    monkeypatch.setattr(feed_mod, "load_dotenv", lambda: None)
    monkeypatch.delenv("COINBASE_API_KEY_JSON_PATH", raising=False)
    with pytest.raises(ValueError, match="no key file"):
        LiveTickerFeed(PRODUCT)
