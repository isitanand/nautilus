from decimal import Decimal
from typing import Optional

try:
    from nautilus_trader.config import StrategyConfig
    _BASE_CONFIG = StrategyConfig
except ImportError:
    class _FallbackConfig:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)
    _BASE_CONFIG = _FallbackConfig


class BtcAtmStraddleConfig(_BASE_CONFIG):
    def __init__(
        self,
        underlying_instrument_id: str = "BTCUSDT-LINEAR.BYBIT",
        target_expiry: Optional[str] = None,
        trade_size: Decimal = Decimal("0.01"),
        level: int = 2,
        stop_loss_pct: Decimal = Decimal("0.01"),
        take_profit_pct: Decimal = Decimal("0.02"),
        max_slippage_bps: int = 50,
        leg_fill_timeout_secs: float = 5.0,
        min_dte_hours: float = 4.0,
        **kwargs,
    ):
        try:
            super().__init__(**kwargs)
        except Exception:
            pass
        self.underlying_instrument_id = underlying_instrument_id
        self.target_expiry = target_expiry
        self.trade_size = trade_size
        self.level = level
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.max_slippage_bps = max_slippage_bps
        self.leg_fill_timeout_secs = leg_fill_timeout_secs
        self.min_dte_hours = min_dte_hours
