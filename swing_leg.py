"""
swing_leg.py — single-leg-live swing controller with a PROTECTED CORE position.

Two buckets:
  core_qty  : never sold. HARD FLOOR. The swing can never take you below this.
  swing_qty : the contracts you actively swing (start 2). Grows over time as
              realized profit banks up, capped at max_swing_qty.

Invariant enforced before every sell:  position - swing_qty >= core_qty
If that would break, the bot HALTS instead of selling into the core.

Cycle:
  ARMED_SELL --(sell swing_qty @ sell_px fills)--> ARMED_BUY
             --(buy swing_qty @ buy_px fills)--> realize profit, maybe grow --> ARMED_SELL

Growth happens at the BUY leg: when enough net profit is banked, buy ONE MORE
than you last sold, funded by that profit. The swing high-water mark rises
(10<->12 becomes 10<->13) while the floor stays put at core_qty. Growing on the
sell side instead would momentarily dip below the floor — so we never do that.

Only ONE order is ever live on the exchange, so the buy can't fill before the
sell. Fills are confirmed by order status, never by price.
"""

from __future__ import annotations
import json
from dataclasses import dataclass, asdict
from enum import Enum
from pathlib import Path
from typing import Protocol, Optional


class State(str, Enum):
    ARMED_SELL = "ARMED_SELL"
    ARMED_BUY = "ARMED_BUY"
    HALTED = "HALTED"


class Broker(Protocol):
    """Implement against Advanced Trade SDK or your FCM adapter."""
    def place_limit(self, side: str, qty: int, price: float) -> str: ...  # -> order_id
    def order_status(self, order_id: str) -> dict: ...                    # {status, filled_qty}
    def cancel(self, order_id: str) -> None: ...
    def position_qty(self) -> int: ...                                    # signed net contracts


@dataclass
class SwingConfig:
    core_qty: int = 10            # HARD FLOOR — never sold
    swing_qty: int = 2           # starting playable size (12 held - 10 core)
    max_swing_qty: int = 5       # cap on how big the swing may grow
    sell_px: float = 65.0
    buy_px: float = 63.0
    contract_size: int = 50      # oz per SLVR contract

    # --- scale-up gate ---
    # Bank this much NET profit (beyond already-reserved margin) before adding
    # one contract to the swing. margin_per_contract MUST come from your FCM.
    margin_per_contract: float = 1000.0      # <-- REPLACE with real FCM margin
    scale_up_buffer_mult: float = 1.5        # need 1.5x a contract's margin banked
    fee_per_contract_roundtrip: float = 0.0  # <-- set real round-trip fee per contract

    # --- risk governor (Jim Paul) ---
    abort_below: float = 60.0    # holding swing & market craters -> stop looping
    abort_above: float = 70.0    # flat on swing & market ran away -> stop looping


@dataclass
class SwingState:
    state: State = State.ARMED_SELL
    live_order_id: Optional[str] = None
    filled_qty: int = 0
    swing_qty: int = 2           # current playable size (persisted; can grow)
    last_sell_qty: int = 0       # how many the last sell leg sold (round-trip base)
    realized_pnl: float = 0.0    # net banked profit
    reserved_margin: float = 0.0 # profit committed to contracts already added
    cycles: int = 0


class SwingTrader:
    def __init__(self, broker: Broker, cfg: SwingConfig, state_path: str = "swing_state.json"):
        self.b = broker
        self.cfg = cfg
        self.path = Path(state_path)
        self.s = self._load()

    # ---- persistence / crash recovery -----------------------------------
    def _load(self) -> SwingState:
        if self.path.exists():
            r = json.loads(self.path.read_text())
            return SwingState(state=State(r["state"]), live_order_id=r["live_order_id"],
                              filled_qty=r["filled_qty"], swing_qty=r["swing_qty"],
                              last_sell_qty=r["last_sell_qty"], realized_pnl=r["realized_pnl"],
                              reserved_margin=r["reserved_margin"], cycles=r["cycles"])
        s = SwingState()
        s.swing_qty = self.cfg.swing_qty
        return s

    def _save(self) -> None:
        self.path.write_text(json.dumps({**asdict(self.s), "state": self.s.state.value}))

    # ---- floor guard -----------------------------------------------------
    def _floor_ok(self, position: int, sell_qty: int) -> bool:
        return position - sell_qty >= self.cfg.core_qty

    def reconcile(self) -> None:
        """Call ONCE on startup. Trust the book, not memory."""
        pos = self.b.position_qty()
        if pos < self.cfg.core_qty:
            return self._halt(f"position {pos} already below core {self.cfg.core_qty}")
        if self.s.live_order_id:
            st = self.b.order_status(self.s.live_order_id)
            if st["status"] in ("FILLED", "CANCELLED", "EXPIRED", "UNKNOWN"):
                self.s.live_order_id = None
                self.s.filled_qty = st.get("filled_qty", 0)
        self._save()

    # ---- arming ----------------------------------------------------------
    def _arm(self, side: str, qty: int, price: float) -> None:
        if self.s.live_order_id:
            try: self.b.cancel(self.s.live_order_id)
            except Exception: pass
        self.s.live_order_id = self.b.place_limit(side, qty, price)
        self.s.filled_qty = 0
        self._save()

    def _ensure_armed(self) -> None:
        if self.s.live_order_id or self.s.state == State.HALTED:
            return
        pos = self.b.position_qty()
        if self.s.state == State.ARMED_SELL:
            if not self._floor_ok(pos, self.s.swing_qty):   # protect the core
                return self._halt(f"sell {self.s.swing_qty} would breach floor at pos {pos}")
            self._arm("SELL", self.s.swing_qty, self.cfg.sell_px)
        elif self.s.state == State.ARMED_BUY:
            self._maybe_scale_up()                          # grow at the BUY leg only
            self._arm("BUY", self.s.swing_qty, self.cfg.buy_px)

    def _maybe_scale_up(self) -> None:
        if self.s.swing_qty >= self.cfg.max_swing_qty:
            return
        free = self.s.realized_pnl - self.s.reserved_margin
        need = self.cfg.margin_per_contract * self.cfg.scale_up_buffer_mult
        if free >= need:
            self.s.swing_qty += 1        # this buy leg will acquire the extra contract
            self._save()

    # ---- main loop -------------------------------------------------------
    def step(self, last_price: float) -> None:
        if self.s.state == State.HALTED:
            return
        if self.s.state == State.ARMED_SELL and last_price >= self.cfg.abort_above:
            return self._halt("price ran above abort_above while flat on swing")
        if self.s.state == State.ARMED_BUY and last_price <= self.cfg.abort_below:
            return self._halt("price fell below abort_below while holding swing")

        self._ensure_armed()
        if not self.s.live_order_id:
            return

        st = self.b.order_status(self.s.live_order_id)
        self.s.filled_qty = st.get("filled_qty", 0)
        if st["status"] == "FILLED" and self.s.filled_qty >= self.s.swing_qty:  # full fill only
            self._on_fill()
        self._save()

    def _on_fill(self) -> None:
        self.s.live_order_id = None
        self.s.filled_qty = 0
        if self.s.state == State.ARMED_SELL:
            self.s.last_sell_qty = self.s.swing_qty
            self.s.state = State.ARMED_BUY              # sell done -> buy now allowed
        else:
            # buy filled: realize profit on the round-tripped portion (what we last sold)
            gross = (self.cfg.sell_px - self.cfg.buy_px) * self.cfg.contract_size * self.s.last_sell_qty
            fees = self.cfg.fee_per_contract_roundtrip * self.s.last_sell_qty
            self.s.realized_pnl += gross - fees
            # any contracts bought beyond what we sold are newly added swing inventory
            added = self.s.swing_qty - self.s.last_sell_qty
            if added > 0:
                self.s.reserved_margin += added * self.cfg.margin_per_contract
            self.s.cycles += 1
            self.s.state = State.ARMED_SELL
        self._save()

    def _halt(self, reason: str = "") -> None:
        if self.s.live_order_id:
            try: self.b.cancel(self.s.live_order_id)
            except Exception: pass
        self.s.live_order_id = None
        self.s.state = State.HALTED
        self._save()
        # HOOK: alert yourself here (SMS/email). HALT means eyeball it now.
        print(f"[HALT] {reason}")

    def run(self, price_feed) -> None:
        self.reconcile()
        for last_price in price_feed:
            self.step(last_price)
