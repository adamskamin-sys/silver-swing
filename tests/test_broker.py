"""Unit tests for CoinbaseBroker using a mock RESTClient.

Live-API behavior is covered by scratch/verify_access.py, which is what actually
proves the SDK bindings work. These tests cover the wrapping logic — status
normalization, side routing, idempotency-key generation, error handling — which
is the part that can silently rot as SDK versions shift.
"""

from unittest.mock import MagicMock

import pytest

from broker import CoinbaseBroker, BrokerConfig


class FakeResponse:
    """Mimics a Coinbase SDK typed response with .to_dict()."""

    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return self._data


def make_broker(client=None):
    return CoinbaseBroker(
        BrokerConfig(product_id="SLR-27AUG26-CDE"),
        client=client or MagicMock(),
    )


# ---- place_limit ------------------------------------------------------------


def test_place_limit_sell_returns_order_id():
    c = MagicMock()
    c.limit_order_gtc_sell.return_value = FakeResponse({
        "success": True,
        "success_response": {"order_id": "abc-123", "product_id": "SLR-27AUG26-CDE"},
    })
    oid = make_broker(c).place_limit("SELL", 2, 65.0)
    assert oid == "abc-123"
    kwargs = c.limit_order_gtc_sell.call_args.kwargs
    assert kwargs["product_id"] == "SLR-27AUG26-CDE"
    assert kwargs["base_size"] == "2"
    assert kwargs["limit_price"] == "65.000"
    assert "client_order_id" in kwargs


def test_place_limit_buy_uses_buy_endpoint():
    c = MagicMock()
    c.limit_order_gtc_buy.return_value = FakeResponse({
        "success": True, "success_response": {"order_id": "xyz-9"},
    })
    make_broker(c).place_limit("BUY", 2, 63.0)
    assert c.limit_order_gtc_buy.called
    assert not c.limit_order_gtc_sell.called


def test_place_limit_lowercase_side_normalizes():
    c = MagicMock()
    c.limit_order_gtc_sell.return_value = FakeResponse({
        "success": True, "success_response": {"order_id": "x"},
    })
    make_broker(c).place_limit("sell", 2, 65.0)


def test_place_limit_bad_side_raises():
    with pytest.raises(ValueError):
        make_broker().place_limit("SHORT", 2, 65.0)


def test_place_limit_failure_raises_with_error_body():
    c = MagicMock()
    c.limit_order_gtc_sell.return_value = FakeResponse({
        "success": False,
        "error_response": {"error": "insufficient funds"},
    })
    with pytest.raises(RuntimeError, match="insufficient funds"):
        make_broker(c).place_limit("SELL", 2, 65.0)


def test_place_limit_generates_distinct_client_order_ids():
    c = MagicMock()
    c.limit_order_gtc_sell.return_value = FakeResponse({
        "success": True, "success_response": {"order_id": "x"},
    })
    b = make_broker(c)
    b.place_limit("SELL", 2, 65.0)
    b.place_limit("SELL", 2, 65.0)
    ids = [call.kwargs["client_order_id"] for call in c.limit_order_gtc_sell.call_args_list]
    assert ids[0] != ids[1]


def test_place_limit_price_decimals_configurable():
    c = MagicMock()
    c.limit_order_gtc_sell.return_value = FakeResponse({
        "success": True, "success_response": {"order_id": "x"},
    })
    b = CoinbaseBroker(BrokerConfig(product_id="X", price_decimals=5), client=c)
    b.place_limit("SELL", 1, 62.80)
    assert c.limit_order_gtc_sell.call_args.kwargs["limit_price"] == "62.80000"


# ---- order_status -----------------------------------------------------------


def test_order_status_maps_filled():
    c = MagicMock()
    c.get_order.return_value = FakeResponse({
        "order": {"status": "FILLED", "filled_size": "2", "average_filled_price": "65.00"},
    })
    r = make_broker(c).order_status("abc-123")
    assert r["status"] == "FILLED"
    assert r["filled_qty"] == 2
    assert r["average_filled_price"] == "65.00"


def test_order_status_maps_open_synonyms():
    c = MagicMock()
    for raw in ("OPEN", "PENDING", "QUEUED"):
        c.get_order.return_value = FakeResponse({"order": {"status": raw, "filled_size": "0"}})
        assert make_broker(c).order_status("x")["status"] == "OPEN"


def test_order_status_unmapped_status_passes_through():
    """Unmapped statuses aren't masked to UNKNOWN — surface the truth."""
    c = MagicMock()
    c.get_order.return_value = FakeResponse({"order": {"status": "WEIRD", "filled_size": "0"}})
    r = make_broker(c).order_status("x")
    assert r["status"] == "WEIRD"
    assert r["raw_status"] == "WEIRD"


def test_order_status_missing_fields_default_safely():
    c = MagicMock()
    c.get_order.return_value = FakeResponse({"order": {}})
    r = make_broker(c).order_status("x")
    assert r["filled_qty"] == 0
    assert r["status"] == "UNKNOWN"


def test_order_status_filled_size_is_float_string():
    """Coinbase sometimes returns filled_size as a decimal string; convert cleanly."""
    c = MagicMock()
    c.get_order.return_value = FakeResponse({"order": {"status": "FILLED", "filled_size": "2.0"}})
    assert make_broker(c).order_status("x")["filled_qty"] == 2


# ---- cancel -----------------------------------------------------------------


def test_cancel_success():
    c = MagicMock()
    c.cancel_orders.return_value = FakeResponse({"results": [{"success": True}]})
    make_broker(c).cancel("abc-123")
    assert c.cancel_orders.call_args.kwargs["order_ids"] == ["abc-123"]


def test_cancel_failure_raises():
    c = MagicMock()
    c.cancel_orders.return_value = FakeResponse({
        "results": [{"success": False, "failure_reason": "not found"}],
    })
    with pytest.raises(RuntimeError, match="not found"):
        make_broker(c).cancel("bad-id")


def test_cancel_no_results_raises():
    c = MagicMock()
    c.cancel_orders.return_value = FakeResponse({"results": []})
    with pytest.raises(RuntimeError):
        make_broker(c).cancel("x")


# ---- position_qty ----------------------------------------------------------


def test_position_qty_long_positive():
    c = MagicMock()
    c.list_futures_positions.return_value = FakeResponse({
        "positions": [
            {"product_id": "SLR-27AUG26-CDE", "side": "LONG", "number_of_contracts": "12"},
        ],
    })
    assert make_broker(c).position_qty() == 12


def test_position_qty_short_negative():
    c = MagicMock()
    c.list_futures_positions.return_value = FakeResponse({
        "positions": [
            {"product_id": "SLR-27AUG26-CDE", "side": "SHORT", "number_of_contracts": "3"},
        ],
    })
    assert make_broker(c).position_qty() == -3


def test_position_qty_filters_by_product():
    c = MagicMock()
    c.list_futures_positions.return_value = FakeResponse({
        "positions": [
            {"product_id": "GOLD-XX", "side": "LONG", "number_of_contracts": "999"},
            {"product_id": "SLR-27AUG26-CDE", "side": "LONG", "number_of_contracts": "12"},
        ],
    })
    assert make_broker(c).position_qty() == 12


def test_position_qty_flat_when_no_match():
    c = MagicMock()
    c.list_futures_positions.return_value = FakeResponse({
        "positions": [{"product_id": "OTHER", "side": "LONG", "number_of_contracts": "5"}],
    })
    assert make_broker(c).position_qty() == 0


def test_position_qty_empty_returns_zero():
    c = MagicMock()
    c.list_futures_positions.return_value = FakeResponse({"positions": []})
    assert make_broker(c).position_qty() == 0


# ---- preview_order ----------------------------------------------------------


def test_preview_order_extracts_fee_and_margin_fields():
    c = MagicMock()
    c.preview_limit_order_gtc_sell.return_value = FakeResponse({
        "commission_total": "2.34",
        "commission_detail_total": {"client_commission": "2.1966"},
        "margin_ratio_data": {"projected_margin_ratio": "0.2054850922403356"},
        "projected_liquidation_buffer": "7.7090553",
        "preview_id": "preview-uuid",
        "errs": [],
    })
    p = make_broker(c).preview_order("SELL", 1, 999.99)
    assert p["commission_total"] == 2.34
    assert p["client_commission"] == 2.1966
    assert p["projected_margin_ratio"] == "0.2054850922403356"
    assert p["preview_id"] == "preview-uuid"
    assert p["errs"] == []


def test_preview_order_buy_uses_buy_endpoint():
    c = MagicMock()
    c.preview_limit_order_gtc_buy.return_value = FakeResponse({"commission_total": "2.34"})
    make_broker(c).preview_order("BUY", 1, 63.0)
    assert c.preview_limit_order_gtc_buy.called
    assert not c.preview_limit_order_gtc_sell.called


def test_preview_order_missing_commission_returns_none():
    c = MagicMock()
    c.preview_limit_order_gtc_sell.return_value = FakeResponse({})
    p = make_broker(c).preview_order("SELL", 1, 65.0)
    assert p["commission_total"] is None


# ---- contract_spec ---------------------------------------------------------


def test_contract_spec_computes_tick_value():
    c = MagicMock()
    c.get_product.return_value = FakeResponse({
        "product_id": "SLR-27AUG26-CDE",
        "price_increment": "0.005",
        "price": "62.80",
        "best_bid_price": "62.755",
        "best_ask_price": "62.765",
        "future_product_details": {
            "contract_size": "50",
            "contract_expiry": "2026-08-27T17:25:00Z",
            "intraday_margin_rate": {"long_margin_rate": "0.09", "short_margin_rate": "0.09"},
            "overnight_margin_rate": {"long_margin_rate": "0.12", "short_margin_rate": "0.13"},
        },
        "fcm_trading_session_details": {"is_session_open": True},
    })
    spec = make_broker(c).contract_spec()
    assert spec["contract_size"] == 50.0
    assert spec["tick_size"] == 0.005
    assert spec["tick_value"] == 0.25
    assert spec["contract_expiry"] == "2026-08-27T17:25:00Z"
    assert spec["session_open"] is True


# ---- init ------------------------------------------------------------------


def test_init_requires_key_when_no_env(monkeypatch):
    # broker.load_dotenv would otherwise re-populate the env var from the
    # project's real .env (find_dotenv walks up from the module file).
    import broker as broker_mod
    monkeypatch.setattr(broker_mod, "load_dotenv", lambda: None)
    monkeypatch.delenv("COINBASE_API_KEY_JSON_PATH", raising=False)
    with pytest.raises(ValueError, match="no key file"):
        CoinbaseBroker(BrokerConfig(product_id="X"))
