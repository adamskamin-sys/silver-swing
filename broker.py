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
import threading
import uuid
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv
from coinbase.rest import RESTClient

# [crew 2026-07-15] Central budget allocator. On approve = call proceeds;
# on defer, LOW/MEDIUM callers back off so CRITICAL order-placement
# retains headroom. Fail-open — controller errors never block a trade.
from rate_limit_controller import EndpointKind, Priority, get_controller


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
        # Adam 2026-07-15 fleet-wide rule: derive price precision from live
        # tick_size per-product, not the hardcoded cfg.price_decimals fallback.
        # HYPE ratchet-stop was rejected for HOURS by Coinbase with
        # INVALID_PRICE_PRECISION because cfg.price_decimals=3 sent "68.200"
        # while HYP tick=0.01 requires 2 decimals. Cached lazily on first
        # _price_str call to avoid an extra API round-trip in __init__.
        self._tick_size_cache: Optional[float] = None
        # Adam 2026-07-15 CRITICAL: per-broker (= per-product) SELL lock.
        # Prevents TOCTOU race where two concurrent SELL submissions both
        # pass _no_short_check because neither has filled yet when the other
        # reads position_qty. CU 2026-07-15 12:34:34 double-fire race —
        # resting stop + hybrid timeout both submitted a SELL 1 in the same
        # second; both accepted; position went +1 LONG → -1 SHORT. This
        # lock serializes the entire {read position + read open sells +
        # submit} critical section per product so the second submission
        # sees the first as already-pending.
        self._sell_lock = threading.Lock()
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

    def _tick_size(self) -> float:
        """Cached tick_size from contract_spec. Falls back to 0 if fetch fails
        (callers then fall through to cfg.price_decimals)."""
        if self._tick_size_cache is None:
            try:
                spec = self.contract_spec()
                self._tick_size_cache = float(spec.get("tick_size") or 0)
            except Exception:
                self._tick_size_cache = 0.0
        return self._tick_size_cache

    def _price_str(self, price: float) -> str:
        """Format a price string at the product's real tick precision.

        Snap-to-tick first (belt-and-suspenders — callers should already snap,
        but if a stale saved value slipped through we still get a valid string).
        Decimals derived from tick_size representation (e.g., 0.01 → 2 decimals,
        0.005 → 3, 0.0001 → 4). Fleet-wide fix — every product's tick_size is
        the source of truth, no per-instrument overrides needed.
        """
        tick = self._tick_size()
        if tick and tick > 0:
            snapped = round(price / tick) * tick
            # Decimals = length of fractional part of tick_size, e.g.:
            # "0.01"  → 2, "0.005" → 3, "0.0001" → 4, "1.0" → 0
            s = f"{tick:.10f}".rstrip("0")
            decimals = len(s.split(".", 1)[1]) if "." in s else 0
            return f"{snapped:.{decimals}f}"
        # Fallback: no tick_size available, use the config default
        return f"{price:.{self.cfg.price_decimals}f}"

    # ---- Rate-limit helper -----------------------------------------------
    # Every REST call passes through _rl_call — records the call in the
    # sliding-window tracker (so utilization is accurate for later gating
    # decisions) and hands back any 429 to the controller for backoff. For
    # LOW/MEDIUM callers we ALSO check acquire() first and abort/defer if
    # we're near the budget cap. CRITICAL always proceeds — a breakout
    # order-placement is worth an occasional 429.
    def _is_spot_product(self, pid: str | None = None) -> bool:
        """Adam 2026-07-20: Coinbase spot vs futures dispatcher.
        Futures product_ids end in '-CDE' (dated CFM) or contain '-PERP-'
        (perpetuals). Everything else with a fiat/stable quote suffix is spot.
        Used to route market-order shape, position lookup, and no-short check
        so a scanner-armed crypto sleeve actually executes."""
        p = pid or self.cfg.product_id
        if not p:
            return False
        if p.endswith("-CDE"):
            return False
        if "-PERP-" in p:
            return False
        return any(p.endswith(s) for s in
                   ("-USD", "-USDC", "-EUR", "-GBP", "-BTC", "-ETH"))

    def _spot_base_currency(self, pid: str | None = None) -> str:
        """META-USDC → 'META', BTC-USD → 'BTC'. Empty if not spot-shaped."""
        p = pid or self.cfg.product_id
        if not self._is_spot_product(p):
            return ""
        return p.split("-", 1)[0]

    def _spot_position_qty(self) -> float:
        """Signed spot balance in base-currency units. LONG only on spot
        (Coinbase doesn't margin-short outside of INTX perps), so this is
        always >= 0. Returns 0.0 on any lookup failure so no-short checks
        fail-safe (refuse the sell rather than allow an over-sell).

        Adam 2026-07-20 CANCEL-CASCADE ROOT FIX: use available + hold, NOT
        just available. Coinbase moves spot tokens from `available_balance`
        to `hold` when a SELL stop-limit is placed. If we read only
        `available`, tick sees 0 the moment we place a stop → thinks
        position is closed → cancels the stop → released back to available
        → next tick sees position again → places new stop → moves to hold
        → cycles. Every ~60s a new stop-place-then-cancel. Adam's Coinbase
        fill history showed 7 META cancels in 13 min for exactly this
        reason. TOTAL balance is the true position — Coinbase's own
        portfolio page shows total (available+hold), not just available."""
        cur = self._spot_base_currency()
        if not cur:
            return 0.0
        try:
            cursor = None
            for _ in range(20):
                kwargs = {"limit": 250}
                if cursor:
                    kwargs["cursor"] = cursor
                resp = _dump(self.client.get_accounts(**kwargs))
                for a in resp.get("accounts") or []:
                    if (a.get("currency") or "").upper() == cur:
                        try:
                            avail = float((a.get("available_balance") or {}).get("value") or 0)
                        except (TypeError, ValueError):
                            avail = 0.0
                        try:
                            hold = float((a.get("hold") or {}).get("value") or 0)
                        except (TypeError, ValueError):
                            hold = 0.0
                        return avail + hold
                if not resp.get("has_next"):
                    break
                cursor = resp.get("cursor")
                if not cursor:
                    break
        except Exception:
            return 0.0
        return 0.0

    def _rl_call(self, priority: Priority, kind: str, fn, *args, **kwargs):
        ctrl = get_controller()
        # For non-critical, give the controller a chance to defer. If deferred,
        # we still make the call (fail-open behavior — never block a legit
        # trade because our controller thinks we're busy), but the deny is
        # recorded so telemetry shows how much throttling would have happened.
        if priority != Priority.CRITICAL:
            ctrl.acquire(priority, kind)  # advisory record, we proceed regardless
        else:
            ctrl.acquire(priority, kind)  # always True for CRITICAL
        try:
            resp = fn(*args, **kwargs)
            # Record successful call — controller sees status_code=200
            ctrl.record_response(kind, status_code=200)
            return resp
        except Exception as e:
            # If the SDK raises with an HTTP status attached, extract for
            # backoff — else just note as a non-2xx.
            code = getattr(e, "status_code", None) or getattr(e, "code", None)
            try:
                code = int(code) if code is not None else None
            except (TypeError, ValueError):
                code = None
            ctrl.record_response(kind, status_code=code)
            raise

    # ---- Broker Protocol -------------------------------------------------

    def place_limit(self, side: str, qty: int, price: float,
                    post_only: bool = False, client_order_id=None) -> str:
        """Place a GTC limit order. Returns the exchange order_id.

        Idempotency: generates a fresh client_order_id (UUIDv4) per call. The
        caller must NOT blindly retry on network error — a retry with a new UUID
        creates a second order. Coordinate retries at the SwingTrader layer.

        post_only: when True, Coinbase REJECTS the order if it would take
        liquidity (cross the spread). Guarantees maker fees, which on CFM are
        ~40-60% cheaper than taker. Cost: orders resting BELOW best_ask on a
        buy leg (or above best_bid on a sell leg) fill normally; only orders
        that would immediately cross get rejected. Caller re-arms next tick
        with a slightly different price.
        """
        s = side.upper()
        if s not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, got {side!r}")
        method = self.client.limit_order_gtc_buy if s == "BUY" else self.client.limit_order_gtc_sell
        # [crew:#7] Accept a caller-supplied client_order_id. Coinbase dedupes on
        # this id, so a caller that persists it can safely retry an ambiguous
        # ack (network timeout after the order was accepted) and RECOVER the
        # same order instead of orphaning a live position. Defaults to a fresh
        # UUID (unchanged behavior) when the caller doesn't pass one.
        kwargs = {
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "product_id": self.cfg.product_id,
            "base_size": str(int(qty)),
            "limit_price": self._price_str(price),
        }
        if post_only:
            kwargs["post_only"] = True
        # Adam 2026-07-15: no shorts allowed. Refuse SELL that would net short.
        # _sell_lock serializes _no_short_check + submit so concurrent SELLs
        # can't both pass the check before either shows up as pending.
        if s == "SELL":
            with self._sell_lock:
                self._no_short_check(qty, kind="place_limit")
                # CRITICAL — order placement always proceeds even under budget pressure.
                resp = _dump(self._rl_call(Priority.CRITICAL, EndpointKind.PRIVATE, method, **kwargs))
        else:
            resp = _dump(self._rl_call(Priority.CRITICAL, EndpointKind.PRIVATE, method, **kwargs))
        if resp.get("success"):
            oid = (resp.get("success_response") or {}).get("order_id")
            if oid:
                return oid
        err = resp.get("error_response") or resp.get("failure_reason") or resp
        raise RuntimeError(f"place_limit failed: {err}")

    def place_market(self, side: str, qty: int, client_order_id=None) -> str:
        """Submit a market order. Fills at whatever the book has right now.

        For futures/CFM, base_size is contract count. Same idempotency contract
        as place_limit — fresh client_order_id per call, don't blindly retry.
        """
        s = side.upper()
        if s not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, got {side!r}")
        method = self.client.market_order_buy if s == "BUY" else self.client.market_order_sell
        kwargs = {
            "client_order_id": client_order_id or str(uuid.uuid4()),  # [crew:#7] caller can supply for idempotent retry
            "product_id": self.cfg.product_id,
        }
        # Adam 2026-07-20 SPOT SUPPORT: Coinbase Advanced Trade requires
        # quote_size (USD notional) for a spot market BUY — base_size on
        # spot BUY is rejected with INVALID_ORDER_CONFIGURATION. Convert
        # unit qty → USD by fetching current price. Spot SELL uses base_size
        # (base-currency units) exactly like futures. Futures orders always
        # use base_size (contract count).
        _is_spot = self._is_spot_product()
        if _is_spot and s == "BUY":
            try:
                pd = _dump(self.client.get_product(self.cfg.product_id))
                _px = float(pd.get("price") or 0)
            except Exception:
                _px = 0.0
            if _px <= 0:
                raise RuntimeError(
                    f"place_market spot BUY: unable to get current price for "
                    f"{self.cfg.product_id} — cannot compute quote_size")
            notional = float(qty) * _px
            kwargs["quote_size"] = f"{notional:.2f}"
        else:
            # Spot SELL uses base_size in units; futures use contract count.
            # int() is safe because scanner UI caps qty at integer 1-100.
            kwargs["base_size"] = str(int(qty))
        # Adam 2026-07-15: no shorts allowed. Refuse SELL that would net short.
        # _sell_lock: {check + submit} atomic per product (see __init__ docstring).
        if s == "SELL":
            with self._sell_lock:
                self._no_short_check(qty, kind="place_market")
                # CRITICAL — market orders always proceed (worth a 429 to hit the top).
                resp = _dump(self._rl_call(Priority.CRITICAL, EndpointKind.PRIVATE, method, **kwargs))
        else:
            resp = _dump(self._rl_call(Priority.CRITICAL, EndpointKind.PRIVATE, method, **kwargs))
        if resp.get("success"):
            oid = (resp.get("success_response") or {}).get("order_id")
            if oid:
                return oid
        err = resp.get("error_response") or resp.get("failure_reason") or resp
        raise RuntimeError(f"place_market failed: {err}")

    def place_stop_limit(self, side: str, qty: int, stop_price: float,
                         limit_price: float, client_order_id=None) -> str:
        """Place a STOP_LIMIT (GTC) order. Coinbase-side triggered exit.

        For a LONG protective stop, call with side='SELL' and
        stop_price=trail_or_stop_level, limit_price slightly below stop_price
        (typically stop_price - one tick) so the resulting limit fills
        immediately after trigger.

        stop_direction is inferred: SELL → STOP_DIRECTION_STOP_DOWN
        (trigger when price falls to stop_price), BUY → STOP_DIRECTION_STOP_UP.

        Adam 2026-07-15: this is the ratchet-stop primitive. The strategy
        layer maintains ONE resting order per product, cancel+replaces on
        ratchet-up, never lowers. Fires as a real Coinbase order — protects
        us even if the bot process dies.
        """
        s = side.upper()
        if s not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, got {side!r}")
        stop_dir = "STOP_DIRECTION_STOP_DOWN" if s == "SELL" else "STOP_DIRECTION_STOP_UP"
        cfg = {
            "stop_limit_stop_limit_gtc": {
                "base_size": str(int(qty)),
                "limit_price": self._price_str(limit_price),
                "stop_price": self._price_str(stop_price),
                "stop_direction": stop_dir,
            }
        }
        coid = client_order_id or str(uuid.uuid4())
        # Prefer explicit create_order (available on every SDK version). Some
        # SDKs also expose stop_limit_order_gtc_{buy,sell} convenience methods,
        # but create_order + order_configuration is the reliable path.
        # Adam 2026-07-15: _sell_lock atomic {check + submit}. include_pending=
        # False for stop-limit — see _no_short_check docstring (avoids ratchet
        # cancel-then-place false-positives; double-fire prevented at swing_leg).
        if s == "SELL":
            with self._sell_lock:
                self._no_short_check(qty, kind="place_stop_limit",
                                     include_pending=False)
                resp = _dump(self._rl_call(
                    Priority.CRITICAL, EndpointKind.PRIVATE,
                    self.client.create_order,
                    client_order_id=coid, product_id=self.cfg.product_id,
                    side=s, order_configuration=cfg,
                ))
        else:
            resp = _dump(self._rl_call(
                Priority.CRITICAL, EndpointKind.PRIVATE,
                self.client.create_order,
                client_order_id=coid, product_id=self.cfg.product_id,
                side=s, order_configuration=cfg,
            ))
        if resp.get("success"):
            oid = (resp.get("success_response") or {}).get("order_id")
            if oid:
                return oid
        err = resp.get("error_response") or resp.get("failure_reason") or resp
        raise RuntimeError(f"place_stop_limit failed: {err}")

    def _pending_sell_qty(self) -> int:
        """Sum of qty across all OPEN SELL orders on this product, covering
        every shape (limit_limit_gtc, stop_limit_stop_limit_gtc, market, …).
        Used by _no_short_check to reject a new SELL that would — together
        with orders already sitting on the book — exceed LONG position.

        Fail-CLOSED: on exception, return a large sentinel so _no_short_check
        refuses the sell (better to skip a sell than accidentally short).
        One MEDIUM-priority API call. Kept broader than list_open_orders (which
        filters to plain limits only for reconciliation purposes)."""
        try:
            resp = _dump(self._rl_call(
                Priority.MEDIUM, EndpointKind.PRIVATE,
                self.client.list_orders,
                order_status=["OPEN"],
                product_ids=[self.cfg.product_id],
            ))
        except Exception as e:
            raise RuntimeError(
                f"pending_sell_qty: list_orders failed ({e}); refusing to "
                f"assume 0 pending (would let a concurrent SELL slip through)"
            ) from e
        total = 0
        for o in (resp.get("orders") or []):
            if str(o.get("side") or "").upper() != "SELL":
                continue
            cfg = o.get("order_configuration") or {}
            qty = 0
            # Walk every known shape until we find one with a base_size.
            for shape in cfg.values():
                if not isinstance(shape, dict):
                    continue
                bs = shape.get("base_size")
                if bs is not None:
                    try:
                        qty = int(float(bs))
                    except (TypeError, ValueError):
                        qty = 0
                    if qty > 0:
                        break
            total += max(qty, 0)
        return total

    def _no_short_check(self, sell_qty: int, kind: str,
                        include_pending: bool = True) -> None:
        """Refuse a SELL that would take the net position (LONG minus already-
        pending SELLs) short. Prevents any code path (trail exit, stop-loss,
        sleeve arm, manual sell, resting-stop double-fire) from opening a
        short.

        Reads position + pending-sell qty from Coinbase — two HIGH/MEDIUM
        API calls per SELL. Fail-CLOSED: if either read fails, we refuse
        (fail-safe = don't accidentally short).

        Adam 2026-07-15 CRITICAL: caller MUST hold self._sell_lock across
        this check + the submit call. Otherwise two concurrent SELLs can
        both read the same pre-submit state and both pass. See CoinbaseBroker
        __init__ docstring for the CU 2026-07-15 12:34:34 race that
        motivated the lock.

        include_pending: set False for place_stop_limit. Rationale: stop-
        limits don't fire on placement (they wait for trigger price), so a
        transient over-count of pending sells (the ratchet cancel-then-place
        window where the old stop is still visible as OPEN) would false-
        positive-refuse a legitimate ratchet replacement. Position check
        still holds; the double-fire risk (resting-stop + market-sell
        firing on same trigger) is prevented at the swing_leg layer via
        the resting_stop_oid mutual-exclusion guards."""
        try:
            pos = int(self.position_qty() or 0)
        except Exception as e:
            raise RuntimeError(
                f"no_short_check: position read failed ({e}); refusing {kind} SELL {sell_qty} "
                "to avoid accidental short"
            ) from e
        if include_pending:
            pending = self._pending_sell_qty()
        else:
            pending = 0
        # Effective available = LONG minus what's already sitting on the book.
        # Never negative (a short position is already past the invariant).
        available = max(pos - pending, 0)
        if int(sell_qty) > available:
            raise RuntimeError(
                f"no_short_check: {kind} SELL {sell_qty} exceeds available {available} "
                f"(pos={pos}, pending_sells={pending}) on {self.cfg.product_id} "
                f"— refused (would net short)"
            )

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
        # HIGH — status check on a live order (needed to trip fills/cancels).
        order = _dump(
            self._rl_call(Priority.HIGH, EndpointKind.PRIVATE,
                          self.client.get_order, order_id)
        ).get("order") or {}
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

    def list_open_orders(self, product_ids: Optional[list[str]] = None) -> list[dict]:
        """Return currently-OPEN orders from Coinbase, normalized to the
        shape reconciliation_monitor expects: [{order_id, symbol, side,
        price, qty}, ...]. Passes product_ids= to the SDK if provided;
        otherwise all-account open orders. Safe/read-only — used by the
        reconciliation monitor to diff exchange orders vs bot sleeve state."""
        try:
            kwargs = {"order_status": ["OPEN"]}
            if product_ids:
                kwargs["product_ids"] = list(product_ids)
            resp = _dump(self.client.list_orders(**kwargs))
        except Exception:
            return []
        out: list[dict] = []
        for o in (resp.get("orders") or []):
            pid = o.get("product_id")
            side = str(o.get("side") or "").upper()
            oid = o.get("order_id")
            # Price + qty live under order_configuration for the various
            # limit_* / stop_limit_* / market_* order shapes.
            # Adam 2026-07-20: include STOP-LIMIT shapes too. Prior code
            # only surfaced pure limit orders, so reconciliation could not
            # see the resting protective stops — and had no way to flag
            # "position held but no protective stop." Now every shape that
            # has a price + qty surfaces, with `kind` set so downstream
            # consumers can distinguish. Backward-compatible: `price`,
            # `qty`, `order_id`, `symbol`, `side` remain on every entry.
            cfg = o.get("order_configuration") or {}
            price = None
            qty = None
            kind = None
            for shape_key in ("limit_limit_gtc", "limit_limit_gtd",
                              "limit_limit_fok", "limit_limit_ioc"):
                shape = cfg.get(shape_key)
                if shape:
                    try:
                        price = float(shape.get("limit_price") or 0)
                        qty = int(float(shape.get("base_size") or 0))
                        kind = "limit"
                    except (TypeError, ValueError):
                        pass
                    break
            if price is None:
                # Try stop-limit shapes. price = trigger (stop_price); the
                # limit_price is captured separately for context.
                for shape_key in ("stop_limit_stop_limit_gtc",
                                  "stop_limit_stop_limit_gtd"):
                    shape = cfg.get(shape_key)
                    if shape:
                        try:
                            price = float(shape.get("stop_price") or 0)
                            qty = int(float(shape.get("base_size") or 0))
                            kind = "stop_limit"
                        except (TypeError, ValueError):
                            pass
                        break
            if price is None or qty is None or not pid or not oid or not side:
                continue
            out.append({
                "order_id": oid, "symbol": pid,
                "side": side, "price": price, "qty": qty,
                "kind": kind or "unknown",
            })
        return out

    def position_qty(self) -> int:
        """Signed net contract count for this product. LONG > 0, SHORT < 0, flat = 0.

        Adam 2026-07-20 SPOT SUPPORT: for spot products, returns whole-unit
        balance from the base-currency account (always LONG on Coinbase spot,
        no margin-short outside INTX perps). Fractional balances truncate
        down to the nearest whole unit so downstream int()-based sizing
        doesn't try to sell a fractional slice that fails."""
        if self._is_spot_product():
            return int(self._spot_position_qty())
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
        callers that read broker.position.qty (logs, _mirror helpers) work.

        Adam 2026-07-20 SPOT SUPPORT: snapshot() only iterates
        list_futures_positions — for spot products it returns qty=0,
        avg_entry=0. That broke _maybe_reconcile_orphan_position for
        every spot sleeve (line 5488 exits early when avg <= 0), so
        HIGH-USD stayed stuck in ARMED_BUY on a 5000-token wallet.
        Coinbase spot doesn't track per-position cost basis, so use
        current mark as the adopt baseline — matches what server.js
        auto-adopt does at seed time."""
        snap = self.snapshot()
        class _Pos:
            pass
        p = _Pos()
        if self._is_spot_product():
            p.qty = int(self._spot_position_qty())
            try:
                spec = self.contract_spec() or {}
                p.avg_entry = float(spec.get("current_price") or 0.0)
            except Exception:
                p.avg_entry = 0.0
        else:
            p.qty = int(snap.get("position_qty") or 0)
            p.avg_entry = float(snap.get("position_avg_entry") or 0.0)
        return p

    @property
    def balance(self) -> float:
        """USD cash across CFM + CBI + USDC. Read from Coinbase."""
        try:
            return float(self.snapshot().get("balance") or 0.0)
        except Exception as e:
            # [crew:#7] Don't fail silently — a 0.0 here can feed sizing math as
            # if the account were empty. Returning 0 is fail-safe (blocks arms
            # rather than oversizing), but it MUST be visible, not swallowed.
            print(f"[broker] balance read FAILED ({type(e).__name__}: {e}) — returning 0.0", flush=True)
            return 0.0

    @property
    def realized_pnl(self) -> float:
        try:
            return float(self.snapshot().get("realized_pnl") or 0.0)
        except Exception as e:
            print(f"[broker] realized_pnl read FAILED ({type(e).__name__}: {e}) — returning 0.0", flush=True)  # [crew:#7]
            return 0.0

    @property
    def lots(self) -> list:
        """No lot-level tracking on the live broker — Coinbase reports one
        blended avg entry per position. Return empty list for API parity."""
        return []

    # ---- Order book depth --------------------------------------------------

    def get_orderbook(self, limit: int = 25) -> dict:
        """Fetch top-N levels of the current order book from Coinbase.
        Returns {"bids": [(price, size), ...], "asks": [(price, size), ...]}
        sorted best-first on each side. Empty lists on any error — callers
        must handle that (falls back to no-op signal, don't crash the tick).

        Used by book-imbalance / wall-detection gates in swing_leg. Cached
        upstream (5-second TTL) so this is called at most ~1×/product/5s
        even under heavy tick load.
        """
        try:
            resp = _dump(self.client.get_product_book(
                product_id=self.cfg.product_id, limit=limit,
            ))
            book = resp.get("pricebook") or resp
            bids_raw = book.get("bids") or []
            asks_raw = book.get("asks") or []
            def _rows(raw):
                out = []
                for r in raw:
                    try:
                        p = float(r.get("price"))
                        s = float(r.get("size"))
                        if p > 0 and s > 0:
                            out.append((p, s))
                    except (TypeError, ValueError, AttributeError):
                        continue
                return out
            bids = _rows(bids_raw)
            asks = _rows(asks_raw)
            bids.sort(key=lambda r: -r[0])  # highest bid first
            asks.sort(key=lambda r: r[0])   # lowest ask first
            return {"bids": bids, "asks": asks}
        except Exception:
            return {"bids": [], "asks": []}

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
            limit_price=self._price_str(price),
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
        """Per-instrument spec block, pulled live so it can't drift from reality.

        Adam 2026-07-20 SPOT SUPPORT: spot products have no future_product_details
        block — contract_size defaults to 1.0 (each unit is one base-currency
        token), no margin (spot is fully-collateralized), no expiry."""
        resp = _dump(self.client.get_product(self.cfg.product_id))
        tick = float(resp.get("price_increment") or 0)
        if self._is_spot_product():
            return {
                "product_id": resp.get("product_id"),
                "contract_size": 1.0,
                "tick_size": tick,
                "tick_value": tick,
                "contract_expiry": None,
                "intraday_margin_rate": None,
                "overnight_margin_rate": None,
                "current_price": resp.get("price"),
                "best_bid": resp.get("best_bid_price"),
                "best_ask": resp.get("best_ask_price"),
                "session_open": True,  # spot always open
                "is_spot": True,
            }
        details = resp.get("future_product_details") or {}
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
