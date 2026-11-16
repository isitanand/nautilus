from decimal import Decimal
from enum import Enum, auto
from typing import Optional, Dict, Any

from nautilus_trader.common.enums import LogColor
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import (
    AssetClass,
    InstrumentClass,
    OptionKind,
    OrderSide,
    OrderType,
    TimeInForce,
)
from nautilus_trader.model.events import (
    OrderAccepted,
    OrderCanceled,
    OrderDenied,
    OrderEmulated,
    OrderExpired,
    OrderFilled,
    OrderInitialized,
    OrderRejected,
    OrderSubmitted,
    OrderTriggered,
    PositionChanged,
    PositionClosed,
    PositionOpened,
)
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, PositionId
from nautilus_trader.model.instruments import CryptoOption, Instrument
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.model.orders import MarketOrder
from nautilus_trader.trading.strategy import Strategy

from config import BtcAtmStraddleConfig


class StraddleState(Enum):
    INITIALIZING = auto()
    DISCOVERING_LEGS = auto()
    PENDING_ENTRY = auto()
    ACTIVE = auto()
    PENDING_EXIT = auto()
    CLOSED = auto()
    EMERGENCY_UNWIND = auto()
    FAILED = auto()


class BtcAtmShortStraddle(Strategy):
    def __init__(self, config: BtcAtmStraddleConfig) -> None:
        super().__init__(config)
        self.config: BtcAtmStraddleConfig = config

        self.underlying_id: InstrumentId = InstrumentId.from_str(config.underlying_instrument_id)
        self.call_instrument: Optional[CryptoOption] = None
        self.put_instrument: Optional[CryptoOption] = None
        self.atm_strike: Optional[Decimal] = None
        self.target_expiry: Optional[str] = config.target_expiry

        self.state: StraddleState = StraddleState.INITIALIZING
        self._underlying_price: Optional[Decimal] = None

        self.call_entry_order_id: Optional[ClientOrderId] = None
        self.put_entry_order_id: Optional[ClientOrderId] = None
        self.call_exit_order_id: Optional[ClientOrderId] = None
        self.put_exit_order_id: Optional[ClientOrderId] = None

        self.call_filled_qty: Decimal = Decimal("0")
        self.call_fill_cost: Decimal = Decimal("0")
        self.put_filled_qty: Decimal = Decimal("0")
        self.put_fill_cost: Decimal = Decimal("0")

        self.call_entry_price: Optional[Decimal] = None
        self.put_entry_price: Optional[Decimal] = None
        self.combined_entry_premium: Optional[Decimal] = None

        self.latest_call_ask: Optional[Decimal] = None
        self.latest_put_ask: Optional[Decimal] = None

        self.entry_start_time_ns: Optional[int] = None
        self._exit_triggered: bool = False

    def on_start(self) -> None:
        self.log.info(
            f"Starting BTC ATM Short Straddle Strategy [Level {self.config.level}]...",
            color=LogColor.BLUE,
        )

        underlying = self.cache.instrument(self.underlying_id)
        if underlying is None:
            self.log.warning(
                f"Underlying instrument {self.underlying_id} not yet in cache. "
                "Will subscribe to quote ticks to prime the engine."
            )

        self.subscribe_quote_ticks(self.underlying_id)
        self.state = StraddleState.DISCOVERING_LEGS
        self.log.info(
            f"Subscribed to {self.underlying_id}. Awaiting reference price for strike discovery...",
            color=LogColor.CYAN,
        )

    def on_stop(self) -> None:
        self.log.info("Stopping BTC ATM Short Straddle Strategy...", color=LogColor.BLUE)
        self.unsubscribe_quote_ticks(self.underlying_id)
        if self.call_instrument:
            self.unsubscribe_quote_ticks(self.call_instrument.id)
        if self.put_instrument:
            self.unsubscribe_quote_ticks(self.put_instrument.id)

    def on_quote_tick(self, tick: QuoteTick) -> None:
        if tick.instrument_id == self.underlying_id:
            mid_price = (Decimal(str(tick.bid_price)) + Decimal(str(tick.ask_price))) / Decimal("2")
            self._underlying_price = mid_price

            if self.state == StraddleState.DISCOVERING_LEGS:
                self._discover_and_select_straddle_legs(self._underlying_price)
            return

        if self.call_instrument and tick.instrument_id == self.call_instrument.id:
            self.latest_call_ask = Decimal(str(tick.ask_price))

        if self.put_instrument and tick.instrument_id == self.put_instrument.id:
            self.latest_put_ask = Decimal(str(tick.ask_price))

        if self.state == StraddleState.ACTIVE and self.config.level >= 2:
            self._evaluate_risk_management()

    def _discover_and_select_straddle_legs(self, current_spot: Decimal) -> None:
        all_instruments = self.cache.instruments()
        options: list[CryptoOption] = [
            inst for inst in all_instruments
            if isinstance(inst, CryptoOption) and (
                "BTC" in str(inst.id.symbol) or "BTC" in str(getattr(inst, "underlying", ""))
            )
        ]

        if not options:
            self.log.debug("No BTC options found in instrument cache yet. Waiting for provider sync...")
            return

        expiries = sorted(list({inst.id.symbol.value.split("-")[1] for inst in options if "-" in inst.id.symbol.value}))
        if not expiries:
            self.log.warning("Could not parse expiry from available option symbols.")
            return

        target_exp = self.target_expiry if (self.target_expiry and self.target_expiry in expiries) else expiries[0]
        self.log.info(f"Targeting Expiry: {target_exp} (Available expiries: {expiries[:5]})")

        calls_by_strike: Dict[Decimal, CryptoOption] = {}
        puts_by_strike: Dict[Decimal, CryptoOption] = {}

        for opt in options:
            sym_parts = opt.id.symbol.value.split("-")
            if len(sym_parts) >= 4 and sym_parts[1] == target_exp:
                strike_val = Decimal(str(opt.strike_price))
                if opt.option_kind == OptionKind.CALL or sym_parts[3] == "C":
                    calls_by_strike[strike_val] = opt
                elif opt.option_kind == OptionKind.PUT or sym_parts[3] == "P":
                    puts_by_strike[strike_val] = opt

        common_strikes = sorted(list(set(calls_by_strike.keys()) & set(puts_by_strike.keys())))
        if not common_strikes:
            self.log.warning(f"No common Call/Put strikes found for expiry {target_exp}.")
            return

        atm_strike = min(common_strikes, key=lambda strike: abs(strike - current_spot))
        self.atm_strike = atm_strike
        self.call_instrument = calls_by_strike[atm_strike]
        self.put_instrument = puts_by_strike[atm_strike]

        self.log.info(
            f"=== ATM Straddle Legs Selected ==="
            f"\nUnderlying Spot: {current_spot:.2f}"
            f"\nATM Strike:      {atm_strike:.2f}"
            f"\nCall Contract:   {self.call_instrument.id}"
            f"\nPut Contract:    {self.put_instrument.id}",
            color=LogColor.GREEN,
        )

        self.subscribe_quote_ticks(self.call_instrument.id)
        self.subscribe_quote_ticks(self.put_instrument.id)
        self._execute_short_straddle()

    def _execute_short_straddle(self) -> None:
        if self.state != StraddleState.DISCOVERING_LEGS:
            return

        self.state = StraddleState.PENDING_ENTRY
        trade_qty = Quantity.from_str(str(self.config.trade_size))

        self.log.info(
            f"Executing Short Straddle: Submitting SELL orders for {trade_qty} contracts each...",
            color=LogColor.YELLOW,
        )

        call_order: MarketOrder = self.order_factory.market(
            instrument_id=self.call_instrument.id,
            order_side=OrderSide.SELL,
            quantity=trade_qty,
            time_in_force=TimeInForce.IOC,
        )
        self.call_entry_order_id = call_order.client_order_id

        put_order: MarketOrder = self.order_factory.market(
            instrument_id=self.put_instrument.id,
            order_side=OrderSide.SELL,
            quantity=trade_qty,
            time_in_force=TimeInForce.IOC,
        )
        self.put_entry_order_id = put_order.client_order_id

        self.entry_start_time_ns = self.clock.utc_now()

        self.submit_order(call_order)
        self.submit_order(put_order)

        self.log.info(
            f"Orders Submitted -> Call OrderId: {call_order.client_order_id}, "
            f"Put OrderId: {put_order.client_order_id}"
        )

    def on_order_submitted(self, event: OrderSubmitted) -> None:
        self.log.debug(f"Order Submitted: {event.client_order_id}")

    def on_order_accepted(self, event: OrderAccepted) -> None:
        self.log.info(f"Order Accepted by Exchange: {event.client_order_id}", color=LogColor.CYAN)

    def on_order_rejected(self, event: OrderRejected) -> None:
        self.log.error(f"Order REJECTED by Exchange: {event.client_order_id} - Reason: {event.reason}")
        if self.state == StraddleState.PENDING_ENTRY:
            self._handle_entry_rejection(event.client_order_id)

    def on_order_filled(self, event: OrderFilled) -> None:
        fill_qty = Decimal(str(event.last_qty))
        fill_price = Decimal(str(event.last_px))

        self.log.info(
            f"ORDER FILLED -> {event.client_order_id} | Side: {event.order_side.name} | "
            f"Qty: {fill_qty} | Price: {fill_price:.4f}",
            color=LogColor.GREEN,
        )

        if event.client_order_id == self.call_entry_order_id:
            self.call_filled_qty += fill_qty
            self.call_fill_cost += fill_qty * fill_price
            self.call_entry_price = self.call_fill_cost / self.call_filled_qty

        elif event.client_order_id == self.put_entry_order_id:
            self.put_filled_qty += fill_qty
            self.put_fill_cost += fill_qty * fill_price
            self.put_entry_price = self.put_fill_cost / self.put_filled_qty

        target_qty = self.config.trade_size
        if (
            self.state == StraddleState.PENDING_ENTRY
            and self.call_filled_qty >= target_qty
            and self.put_filled_qty >= target_qty
        ):
            self.combined_entry_premium = self.call_entry_price + self.put_entry_price
            self.state = StraddleState.ACTIVE
            self.log.info(
                f"\n{'='*55}\n"
                f"SHORT STRADDLE POSITION ESTABLISHED (ACTIVE)\n"
                f"Call Entry Fill:     {self.call_entry_price:.4f} USD\n"
                f"Put Entry Fill:      {self.put_entry_price:.4f} USD\n"
                f"Combined Entry Prem: {self.combined_entry_premium:.4f} USD\n"
                f"Contract Size:       {target_qty} BTC each\n"
                f"{'='*55}",
                color=LogColor.GREEN,
            )

        elif self.state == StraddleState.PENDING_EXIT:
            if (
                event.client_order_id in (self.call_exit_order_id, self.put_exit_order_id)
                and self._all_positions_closed()
            ):
                self.state = StraddleState.CLOSED
                self.log.info("ALL STRADDLE POSITIONS SUCCESSFULLY CLOSED.", color=LogColor.BLUE)

    def on_order_canceled(self, event: OrderCanceled) -> None:
        self.log.warning(f"Order Canceled: {event.client_order_id}")
        if self.state == StraddleState.PENDING_ENTRY:
            self._handle_legging_risk()

    def _evaluate_risk_management(self) -> None:
        if self.latest_call_ask is None or self.latest_put_ask is None:
            return

        if self.combined_entry_premium is None or self.combined_entry_premium <= Decimal("0"):
            return

        if self._exit_triggered or self.state != StraddleState.ACTIVE:
            return

        combined_buyback_premium = self.latest_call_ask + self.latest_put_ask
        pct_change = (combined_buyback_premium - self.combined_entry_premium) / self.combined_entry_premium

        self.log.debug(
            f"Straddle Valuation -> Entry: {self.combined_entry_premium:.4f} | "
            f"Current Buyback: {combined_buyback_premium:.4f} (Call Ask: {self.latest_call_ask:.4f}, Put Ask: {self.latest_put_ask:.4f}) | "
            f"Change: {pct_change * 100:+.2f}%"
        )

        if pct_change >= self.config.stop_loss_pct:
            self._exit_triggered = True
            self.log.warning(
                f"\n{'!'*60}\n"
                f"STOP-LOSS TRIGGERED (+{pct_change*100:.2f}% >= +{self.config.stop_loss_pct*100:.1f}%)\n"
                f"Combined Entry Premium:   {self.combined_entry_premium:.4f}\n"
                f"Current Buyback Premium: {combined_buyback_premium:.4f}\n"
                f"Closing both positions immediately...\n"
                f"{'!'*60}",
                color=LogColor.RED,
            )
            self._close_straddle(reason="Stop-Loss (+1% buyback threshold reached)")

        elif pct_change <= -self.config.take_profit_pct:
            self._exit_triggered = True
            self.log.info(
                f"\n{'*'*60}\n"
                f"TAKE-PROFIT TRIGGERED ({pct_change*100:.2f}% <= -{self.config.take_profit_pct*100:.1f}%)\n"
                f"Combined Entry Premium:   {self.combined_entry_premium:.4f}\n"
                f"Current Buyback Premium: {combined_buyback_premium:.4f}\n"
                f"Capturing profit and closing both positions...\n"
                f"{'*'*60}",
                color=LogColor.GREEN,
            )
            self._close_straddle(reason="Take-Profit (-2% buyback threshold reached)")

    def _close_straddle(self, reason: str) -> None:
        self.state = StraddleState.PENDING_EXIT
        self.log.info(f"Initiating Straddle Exit. Reason: {reason}", color=LogColor.YELLOW)

        call_pos = self.cache.position(PositionId(f"{self.call_instrument.id}-POSITION")) if self.call_instrument else None
        put_pos = self.cache.position(PositionId(f"{self.put_instrument.id}-POSITION")) if self.put_instrument else None

        call_qty_to_cover = (
            call_pos.quantity if (call_pos and call_pos.is_open)
            else Quantity.from_str(str(self.call_filled_qty))
        )
        put_qty_to_cover = (
            put_pos.quantity if (put_pos and put_pos.is_open)
            else Quantity.from_str(str(self.put_filled_qty))
        )

        if call_qty_to_cover > Decimal("0"):
            call_exit_order = self.order_factory.market(
                instrument_id=self.call_instrument.id,
                order_side=OrderSide.BUY,
                quantity=call_qty_to_cover,
                time_in_force=TimeInForce.IOC,
            )
            self.call_exit_order_id = call_exit_order.client_order_id
            self.submit_order(call_exit_order)
            self.log.info(f"Submitted Call Exit Order: {call_exit_order.client_order_id} (Qty: {call_qty_to_cover})")

        if put_qty_to_cover > Decimal("0"):
            put_exit_order = self.order_factory.market(
                instrument_id=self.put_instrument.id,
                order_side=OrderSide.BUY,
                quantity=put_qty_to_cover,
                time_in_force=TimeInForce.IOC,
            )
            self.put_exit_order_id = put_exit_order.client_order_id
            self.submit_order(put_exit_order)
            self.log.info(f"Submitted Put Exit Order: {put_exit_order.client_order_id} (Qty: {put_qty_to_cover})")

    def _handle_entry_rejection(self, rejected_order_id: ClientOrderId) -> None:
        self.log.error(f"Legging Risk Alert: Entry order {rejected_order_id} was rejected.")
        self._emergency_unwind("Leg rejected at exchange entry")

    def _handle_legging_risk(self) -> None:
        if self.call_filled_qty > Decimal("0") and self.put_filled_qty == Decimal("0"):
            self.log.error("Legging Asymmetry: Call leg filled but Put leg failed/timed out! Unwinding Call.")
            self._emergency_unwind("Put leg failed to fill within timeout")
        elif self.put_filled_qty > Decimal("0") and self.call_filled_qty == Decimal("0"):
            self.log.error("Legging Asymmetry: Put leg filled but Call leg failed/timed out! Unwinding Put.")
            self._emergency_unwind("Call leg failed to fill within timeout")

    def _emergency_unwind(self, reason: str) -> None:
        self.state = StraddleState.EMERGENCY_UNWIND
        self.log.warning(f"EMERGENCY UNWIND TRIGGERED: {reason}", color=LogColor.RED)
        self._close_straddle(reason=f"Emergency Unwind: {reason}")

    def _all_positions_closed(self) -> bool:
        call_open = False
        put_open = False
        if self.call_instrument:
            call_pos = self.cache.position(PositionId(f"{self.call_instrument.id}-POSITION"))
            if call_pos and call_pos.is_open:
                call_open = True
        if self.put_instrument:
            put_pos = self.cache.position(PositionId(f"{self.put_instrument.id}-POSITION"))
            if put_pos and put_pos.is_open:
                put_open = True
        return not (call_open or put_open)

    def on_position_opened(self, event: PositionOpened) -> None:
        self.log.info(f"Position Opened: {event.position_id} | Side: {event.side.name} | Qty: {event.quantity}")

    def on_position_changed(self, event: PositionChanged) -> None:
        self.log.debug(f"Position Changed: {event.position_id} | Net Qty: {event.quantity}")

    def on_position_closed(self, event: PositionClosed) -> None:
        self.log.info(
            f"Position Closed: {event.position_id} | Realized PnL: {event.realized_pnl}",
            color=LogColor.MAGENTA,
        )
