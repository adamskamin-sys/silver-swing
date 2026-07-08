"""
CoinbaseBroker — Broker adapter for Coinbase Advanced Trade / CFM (spec §12 step 2).

Wraps `coinbase.rest.RESTClient` to implement swing_leg.py's `Broker` Protocol
so the strategy code stays exchange-agnostic. Adds `preview_order` for the §2A
pre-trade fee gate and `contract_spec` / `futures_balance` for the empirical
inputs the strategy math needs.

READ-side methods (order_status, position_qty, preview_order, contract_spec,
futures_balance) are safe to call today — they only fetch data.

WRITE-side methods (place_limit, cancel) execute real trades against the live
account and must NOT be called until the full main loop is wired up against
either a PaperBroker or a deliberate live run. Nothing in this file wires them
to a caller.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv
from coinbase.rest import RESTClient


# Map Coinbase Advanced Trade order statuses to the vocabulary swing_leg.py checks against
# (FILLED / CANCELLED / EXPIRED / UNKNOWN). OPEN is added so the strategy can distinguish
# "resting on the book" from "unknown," which the original prototype conflated.
STATUS_MAP = {
    "OPEN": "OPEN",
    "PENDING": "OPEN",
    "QUEUED": "OPEN",
    "FILLED": "FILLED",
    "CANCELLED": "CANCELLED",
    "CANCEL_QUEUED": "CANCELLED",
    "EXPIRED": "EXPIRED",
    "FAILED": "UNKNOWN",
}


def _dump(obj):
    """Normalize a typed SDK response (or a dict, or None) to a plain dict."""
    if obj is None:
        return {}
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, dict):
        return obj
    return {}


# Local-only order id prefixes. State can carry these across broker swaps
# (dry-run session → live session, paper session → live restart). Forwarding
# them to Coinbase is guaranteed 400 INVALID_ARGUMENT; treat them as stale and
# skip the round-trip.
_STALE_LOCAL_PREFIXES = ("dry-run-", "paper-")


def _is_stale_local_order_id(order_id) -> bool:
    if not order_id:
        return False
    return any(str(order_id).startswith(p) for p in _STALE_LOCAL_PREFIXES)


@dataclass
class BrokerConfig:
    product_id: str                    # e.g., "SLR-27AUG26-CDE"
    key_file: Optional[str] = None     # path to Coinbase JSON key; falls back to $COINBASE_API_KEY_JSON_PATH
    price_decimals: int = 3            # SLR tick is 0.005 → 3 decimals is enough; override per instrument


class CoinbaseBroker:
    """Implements the Broker Protocol against Coinbase Advanced Trade / CFM."""

    def __init__(self, cfg: BrokerConfig, client: Optional[RESTClient] = None):
        self.cfg = cfg
        if client is not None:
            # Injected client (tests, or an already-authenticated instance)
            self.client = client
            return
        load_dotenv()
        key_path = cfg.key_file or os.getenv("COINBASE_API_KEY_JSON_PATH")
        if not key_path:
            raise ValueError(
                "no key file: pass BrokerConfig(key_file=...) or set COINBASE_API_KEY_JSON_PATH"
            )
        self.client = RESTClient(key_file=key_path)

    # ---- Broker Protocol -------------------------------------------------

    def place_limit(self, side: str, qty: int, price: float) -> str:
        """Place a GTC limit order. Returns the exchange order_id.

        Idempotency: generates a fresh client_order_id (UUIDv4) per call. The
        caller must NOT blindly retry on network error — a retry with a new UUID
        creates a second order. Coordinate retries at the SwingTrader layer.
        """
        s = side.upper()
        if s not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, got {side!r}")
        method = self.client.limit_order_gtc_buy if s == "BUY" else self.client.limit_order_gtc_sell
        resp = _dump(method(
            client_order_id=str(uuid.uuid4()),
            product_id=self.cfg.product_id,
            base_size=str(int(qty)),
            limit_price=f"{price:.{self.cfg.price_decimals}f}",
        ))
        if resp.get("success"):
            oid = (resp.get("success_response") or {}).get("order_id")
            if oid:
                return oid
        err = resp.get("error_response") or resp.get("failure_reason") or resp
        raise RuntimeError(f"place_limit failed: {err}")

    def place_market(self, side: str, qty: int) -> str:
        """Submit a market order. Fills at whatever the book has right now.

        For futures/CFM, base_size is contract count. Same idempotency contract
        as place_limit — fresh client_order_id per call, don't blindly retry.
        """
        s = side.upper()
        if s not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, got {side!r}")
        method = self.client.market_order_buy if s == "BUY" else self.client.market_order_sell
        kwargs = {
            "client_order_id": str(uuid.uuid4()),
            "product_id": self.cfg.product_id,
            "base_size": str(int(qty)),
        }
        resp = _dump(method(**kwargs))
        if resp.get("success"):
            oid = (resp.get("success_response") or {}).get("order_id")
            if oid:
                return oid
        err = resp.get("error_response") or resp.get("failure_reason") or resp
        raise RuntimeError(f"place_market failed: {err}")

    def order_status(self, order_id: str) -> dict:
        """Return {'status': mapped, 'filled_qty': int, 'raw_status': ..., 'average_filled_price': ...}."""
        # Stale ids left over from paper / dry-run sessions can end up in
        # state.live_order_id (Redis persists across broker swaps). Forwarding
        # them to Coinbase throws INVALID_ARGUMENT and crashes the worker at
        # reconcile. Treat them as CANCELLED so reconcile clears the state and
        # the strategy re-arms with a real order.
        if _is_stale_local_order_id(order_id):
            return {
                "status": "CANCELLED",
                "filled_qty": 0,
                "raw_status": "STALE_LOCAL_ID",
                "average_filled_price": None,
            }
        order = _dump(self.client.get_order(order_id)).get("order") or {}
        raw = order.get("status") or "UNKNOWN"
        try:
            filled = int(float(order.get("filled_size") or 0))
        except (TypeError, ValueError):
            filled = 0
        return {
            "status": STATUS_MAP.get(raw, raw),
            "filled_qty": filled,
            "raw_status": raw,
            "average_filled_price": order.get("average_filled_price"),
        }

    def cancel(self, order_id: str) -> None:
        """Cancel one live order by its exchange order_id."""
        # Same stale-id guard as order_status — cancelling a fake dry-run id
        # against Coinbase would 400 too. No-op it and let the caller move on.
        if _is_stale_local_order_id(order_id):
            return
        resp = _dump(self.client.cancel_orders(order_ids=[order_id]))
        results = resp.get("results") or []
        if not results:
            raise RuntimeError(f"cancel returned no results: {resp}")
        r = results[0]
        if not r.get("success"):
            raise RuntimeError(f"cancel failed: {r}")

    def position_qty(self) -> int:
        """Signed net contract count for this product. LONG > 0, SHORT < 0, flat = 0."""
        for p in _dump(self.client.list_futures_positions()).get("positions") or []:
            if p.get("product_id") == self.cfg.product_id:
                n = int(float(p.get("number_of_contracts") or 0))
                return n if (p.get("side") or "").upper() == "LONG" else -n
        return 0

    # ---- Paper-compat surface so _Track can drive us identically ---------
    # PaperBroker maintains an internal simulated position that the tick loop
    # feeds bid/ask into, then reads a snapshot from. On the live exchange the
    # book is the source of truth — we don't need to be told where the market
    # is. These stubs let the same _Track code path work for both.

    def tick(self, bid: float, ask: float) -> None:
        """No-op on the live broker — Coinbase's order book already knows
        where the market is. Kept for interface parity with PaperBroker."""
        return

    def set_external_day_range(self, high, low) -> None:
        """No-op — live snapshot() reads 24h range straight from Coinbase."""
        return

    def to_state_dict(self) -> dict:
        """Position + realized live on Coinbase, not in local JSON. Return
        an empty dict so store.put_paper_state() is a no-op equivalent."""
        return {}

    def restore_from_state_dict(self, _state: dict) -> None:
        """No-op — position is read fresh from Coinbase on every reconcile()."""
        return

    @property
    def position(self):
        """Compatibility with PaperBroker's .position.qty / .avg_entry access.
        Returns a tiny namespace populated from the live snapshot() call so
        callers that read broker.position.qty (logs, _mirror helpers) work."""
        snap = self.snapshot()
        class _Pos:
            pass
        p = _Pos()
        p.qty = int(snap.get("position_qty") or 0)
        p.avg_entry = float(snap.get("position_avg_entry") or 0.0)
        return p

    @property
    def balance(self) -> float:
        """USD cash across CFM + CBI + USDC. Read from Coinbase."""
        try:
            return float(self.snapshot().get("balance") or 0.0)
        except Exception:
            return 0.0

    @property
    def realized_pnl(self) -> float:
        try:
            return float(self.snapshot().get("realized_pnl") or 0.0)
        except Exception:
            return 0.0

    @property
    def lots(self) -> list:
        """No lot-level tracking on the live broker — Coinbase reports one
        blended avg entry per position. Return empty list for API parity."""
        return []

    # ---- §2A fee gate ----------------------------------------------------

    def preview_order(self, side: str, qty: int, price: float) -> dict:
        """Preview a limit order. Read-only — does NOT create the order.

        Returns the fee, projected margin, and preview_id needed by the §2A gate.
        `raw` is the full SDK response for anything else the caller wants.
        """
        s = side.upper()
        if s not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, got {side!r}")
        method = (
            self.client.preview_limit_order_gtc_buy if s == "BUY"
            else self.client.preview_limit_order_gtc_sell
        )
        resp = _dump(method(
            product_id=self.cfg.product_id,
            base_size=str(int(qty)),
            limit_price=f"{price:.{self.cfg.price_decimals}f}",
        ))
        commission = resp.get("commission_total")
        detail = resp.get("commission_detail_total") or {}
        return {
            "commission_total": float(commission) if commission is not None else None,
            "client_commission": float(detail.get("client_commission") or 0),
            "projected_margin_ratio": (resp.get("margin_ratio_data") or {}).get("projected_margin_ratio"),
            "projected_liquidation_buffer": resp.get("projected_liquidation_buffer"),
            "preview_id": resp.get("preview_id"),
            "errs": resp.get("errs") or [],
            "raw": resp,
        }

    # ---- Empirical spec inputs (spec §3A refresh on startup) --------------

    def contract_spec(self) -> dict:
        """Per-instrument spec block, pulled live so it can't drift from reality."""
        resp = _dump(self.client.get_product(self.cfg.product_id))
        details = resp.get("future_product_details") or {}
        tick = float(resp.get("price_increment") or 0)
        size = float(details.get("contract_size") or 0)
        return {
            "product_id": resp.get("product_id"),
            "contract_size": size,
            "tick_size": tick,
            "tick_value": tick * size,
            "contract_expiry": details.get("contract_expiry"),
            "intraday_margin_rate": details.get("intraday_margin_rate"),
            "overnight_margin_rate": details.get("overnight_margin_rate"),
            "current_price": resp.get("price"),
            "best_bid": resp.get("best_bid_price"),
            "best_ask": resp.get("best_ask_price"),
            "session_open": (resp.get("fcm_trading_session_details") or {}).get("is_session_open"),
        }

    def futures_balance(self) -> dict:
        """Real-time futures account balance summary — empirical inputs for §4 gates."""
        return _dump(self.client.get_futures_balance_summary()).get("balance_summary") or {}

    def stablecoin_balance(self) -> float:
        """USDC held in spot accounts. Coinbase's Total balance display in the
        app includes this; get_futures_balance_summary does not — that's why
        the dashboard's TOTAL VALUE reads lower than Coinbase's total when
        the user is holding stables.

        Coinbase's get_accounts is paginated (default 49 per page). Users
        with multiple wallets (SOL, dust crypto, USD, USDC, etc.) often have
        USDC beyond the first page, so we iterate the cursor until has_next
        is False. Also sums 'hold' so pending USDC isn't dropped.

        Returns 0.0 on any failure so the caller (snapshot) can proceed
        without crashing the whole snapshot.
        """
        try:
            total = 0.0
            cursor = None
            # Hard page cap so a broken cursor loop can't wedge the snapshot
            # forever. 20 pages × 250 = 5000 accounts, well beyond any user.
            for _ in range(20):
                kwargs = {"limit": 250}
                if cursor:
                    kwargs["cursor"] = cursor
                resp = _dump(self.client.get_accounts(**kwargs))
                accts = resp.get("accounts") or []
                for a in accts:
                    cur = (a.get("currency") or "").upper()
                    if cur != "USDC":
                        continue
                    avail = a.get("available_balance") or {}
                    hold = a.get("hold") or {}
                    try:
                        total += float(avail.get("value") or 0)
                    except (TypeError, ValueError):
                        pass
                    try:
                        total += float(hold.get("value") or 0)
                    except (TypeError, ValueError):
                        pass
                if not resp.get("has_next"):
                    break
                cursor = resp.get("cursor")
                if not cursor:
                    break
            return total
        except Exception:
            return 0.0

    def list_all_holdings(self) -> list[dict]:
        """Every non-zero position + spot balance on the connected Coinbase
        account. Powers the Live tab's "actual portfolio" view — everything the
        user owns is a candidate for either manual trading or Model attachment.

        Returns list of dicts:
          {kind: 'futures'|'spot', product_id, qty, avg_entry, mark, unrealized,
           display: 'BTC-USD' style label}

        Best-effort — swallows per-page failures so a broken cursor can't stall
        the caller. Empty list on total failure.
        """
        out: list[dict] = []

        # Futures positions (all products, not just self.cfg.product_id)
        try:
            for p in _dump(self.client.list_futures_positions()).get("positions") or []:
                pid = p.get("product_id")
                if not pid:
                    continue
                n = int(float(p.get("number_of_contracts") or 0))
                if n == 0:
                    continue
                signed = n if (p.get("side") or "").upper() == "LONG" else -n
                try: avg = float(p.get("avg_entry_price") or 0)
                except (TypeError, ValueError): avg = 0.0
                try: mark = float(p.get("current_price") or 0)
                except (TypeError, ValueError): mark = 0.0
                try: unreal = float(p.get("unrealized_pnl") or 0)
                except (TypeError, ValueError): unreal = 0.0
                out.append({
                    "kind": "futures",
                    "product_id": pid,
                    "qty": signed,
                    "avg_entry": avg,
                    "mark": mark,
                    "unrealized": unreal,
                    "display": pid,
                })
        except Exception:
            pass

        # Spot balances (all currencies with a non-zero available or hold).
        # Coinbase pages accounts; iterate until has_next is False. USD/USDC
        # excluded here because they're cash, not tradeable assets — surfaced
        # separately via futures_balance/stablecoin_balance if callers want them.
        try:
            cursor = None
            for _ in range(20):
                kwargs = {"limit": 250}
                if cursor:
                    kwargs["cursor"] = cursor
                resp = _dump(self.client.get_accounts(**kwargs))
                for a in resp.get("accounts") or []:
                    cur = (a.get("currency") or "").upper()
                    if cur in ("USD", "USDC", ""):
                        continue
                    avail = 0.0
                    hold = 0.0
                    try: avail = float((a.get("available_balance") or {}).get("value") or 0)
                    except (TypeError, ValueError): pass
                    try: hold = float((a.get("hold") or {}).get("value") or 0)
                    except (TypeError, ValueError): pass
                    total = avail + hold
                    if total <= 0:
                        continue
                    product_id = f"{cur}-USD"
                    out.append({
                        "kind": "spot",
                        "product_id": product_id,
                        "currency": cur,
                        "qty": total,
                        "avg_entry": 0.0,
                        "mark": 0.0,
                        "unrealized": 0.0,
                        "display": product_id,
                    })
                if not resp.get("has_next"):
                    break
                cursor = resp.get("cursor")
                if not cursor:
                    break
        except Exception:
            pass

        return out

    def portfolio_snapshot(self) -> dict:
        """Structured full-account snapshot for the Live tab's Coinbase-style
        portfolio view. Sections: cash (USD accounts + USDC), derivatives
        (futures positions), crypto (non-USD spot balances). Also computes
        allocation percentages and totals.

        Best-effort: any subcall that fails still returns a partial snapshot
        with zeros for the missing section so the dashboard can render.
        """
        # ---- cash breakdown ---------------------------------------------
        try:
            balance = self.futures_balance()
        except Exception:
            balance = {}
        def _bnum(node):
            try: return float(((balance.get(node) or {}).get("value")) or 0)
            except (TypeError, ValueError): return 0.0
        cbi = _bnum("cbi_usd_balance")       # Primary USD (spot)
        cfm = _bnum("cfm_usd_balance")       # Derivatives USD (futures collateral)
        try:
            usdc = self.stablecoin_balance()
        except Exception:
            usdc = 0.0
        cash_total = cbi + cfm + usdc

        # ---- futures positions ------------------------------------------
        derivatives: list[dict] = []
        derivatives_unrealized = 0.0
        try:
            for p in _dump(self.client.list_futures_positions()).get("positions") or []:
                pid = p.get("product_id")
                if not pid:
                    continue
                n = int(float(p.get("number_of_contracts") or 0))
                if n == 0:
                    continue
                side = (p.get("side") or "").upper()
                try: avg = float(p.get("avg_entry_price") or 0)
                except (TypeError, ValueError): avg = 0.0
                try: mark = float(p.get("current_price") or 0)
                except (TypeError, ValueError): mark = 0.0
                # Refresh mark from get_product so we get the freshest price
                # rather than whatever list_futures_positions cached. Also
                # recompute unrealized from that fresh mark so it's honest.
                contract_size = 0.0
                try:
                    pd = _dump(self.client.get_product(pid))
                    fresh_mark = float(pd.get("price") or 0)
                    if fresh_mark > 0:
                        mark = fresh_mark
                    contract_size = float((pd.get("future_product_details") or {}).get("contract_size") or 0)
                except Exception:
                    pass
                # Prefer live-recomputed unrealized when we have contract_size;
                # otherwise fall back to whatever the position snapshot returned.
                if mark > 0 and avg > 0 and contract_size > 0:
                    signed = n if side == "LONG" else -n
                    unreal = (mark - avg) * signed * contract_size
                else:
                    try: unreal = float(p.get("unrealized_pnl") or 0)
                    except (TypeError, ValueError): unreal = 0.0
                try: liq = float(p.get("liquidation_price") or 0)
                except (TypeError, ValueError): liq = 0.0
                derivatives.append({
                    "product_id": pid,
                    "side": side,
                    "qty": n,
                    "avg_entry": avg,
                    "mark": mark,
                    "unrealized": unreal,
                    "liquidation_price": liq,
                })
                derivatives_unrealized += unreal
        except Exception:
            pass

        # ---- spot crypto ------------------------------------------------
        crypto: list[dict] = []
        crypto_total = 0.0
        try:
            cursor = None
            for _ in range(20):
                kwargs = {"limit": 250}
                if cursor:
                    kwargs["cursor"] = cursor
                resp = _dump(self.client.get_accounts(**kwargs))
                for a in resp.get("accounts") or []:
                    cur = (a.get("currency") or "").upper()
                    # Skip fiat & stablecoins — they're in the cash section.
                    if cur in ("USD", "USDC", ""):
                        continue
                    try: avail = float((a.get("available_balance") or {}).get("value") or 0)
                    except (TypeError, ValueError): avail = 0.0
                    try: hold = float((a.get("hold") or {}).get("value") or 0)
                    except (TypeError, ValueError): hold = 0.0
                    total = avail + hold
                    if total <= 0:
                        continue
                    # Try to price it: spot product is CURRENCY-USD
                    product_id = f"{cur}-USD"
                    mark = 0.0
                    try:
                        pd = _dump(self.client.get_product(product_id))
                        mark = float(pd.get("price") or 0)
                    except Exception:
                        pass
                    value_usd = total * mark if mark > 0 else 0.0
                    crypto.append({
                        "currency": cur,
                        "product_id": product_id,
                        "balance": total,
                        "available": avail,
                        "mark": mark,
                        "value_usd": value_usd,
                    })
                    crypto_total += value_usd
                if not resp.get("has_next"):
                    break
                cursor = resp.get("cursor")
                if not cursor:
                    break
        except Exception:
            pass

        # ---- allocation percentages -------------------------------------
        grand_total = cash_total + derivatives_unrealized + crypto_total
        gt_pos = grand_total if grand_total > 0 else 1.0
        for c in crypto:
            c["allocation_pct"] = (c["value_usd"] / gt_pos) * 100 if gt_pos else 0.0

        return {
            "cash": {
                "primary_usd": cbi,
                "derivatives_usd": cfm,
                "predictions_usd": 0.0,  # not exposed via public API; placeholder
                "usdc": usdc,
                "total": cash_total,
                "usd_total": cbi + cfm,
            },
            "derivatives": derivatives,
            "derivatives_unrealized": derivatives_unrealized,
            "crypto": crypto,
            "crypto_total": crypto_total,
            "grand_total": grand_total,
            "generated_at": __import__("time").time(),
        }

    def snapshot(self) -> dict:
        """Unified snapshot in the same shape as PaperBroker.snapshot() so the
        dashboard can render either without branching. Best-effort — any subcall
        that fails returns {} rather than propagating."""
        try:
            balance = self.futures_balance()
        except Exception:
            balance = {}
        try:
            positions = _dump(self.client.list_futures_positions()).get("positions") or []
        except Exception:
            positions = []

        pos_qty = 0
        avg_entry = 0.0
        mark = 0.0
        for p in positions:
            if p.get("product_id") == self.cfg.product_id:
                n = int(float(p.get("number_of_contracts") or 0))
                pos_qty = n if (p.get("side") or "").upper() == "LONG" else -n
                try: avg_entry = float(p.get("avg_entry_price") or 0)
                except (TypeError, ValueError): pass
                try: mark = float(p.get("current_price") or 0)
                except (TypeError, ValueError): pass
                break

        def _num(node, key):
            try: return float(((balance.get(node) or {}).get("value")) or 0)
            except (TypeError, ValueError): return 0.0

        cfm_balance = _num("cfm_usd_balance", "value")
        cbi_balance = _num("cbi_usd_balance", "value")
        # USDC in spot accounts. Coinbase's "Total balance" in the app rolls
        # this in; the futures balance summary doesn't. Pulling it separately
        # closes the mismatch the dashboard used to show.
        usdc_balance = self.stablecoin_balance()
        unrealized = _num("unrealized_pnl", "value")
        return {
            "mode": "live",
            "product_id": self.cfg.product_id,
            "position_qty": pos_qty,
            "position_avg_entry": avg_entry,
            "last_mark": mark,
            "balance": cbi_balance + cfm_balance + usdc_balance,
            "cfm_usd_balance": cfm_balance,
            "cbi_usd_balance": cbi_balance,
            "usdc_balance": usdc_balance,
            "unrealized_pnl": unrealized,
            "realized_pnl": _num("daily_realized_pnl", "value"),
            "initial_margin": _num("initial_margin", "value"),
            "maintenance_margin": _num("liquidation_threshold", "value"),
            "available_margin": _num("available_margin", "value"),
            "futures_buying_power": _num("futures_buying_power", "value"),
            "liquidation_buffer": _num("liquidation_buffer_amount", "value"),
            "equity": cbi_balance + cfm_balance + usdc_balance + unrealized,
        }
