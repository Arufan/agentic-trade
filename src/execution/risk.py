from config import settings
from src.utils.logger import logger


class RiskManager:
    def __init__(
        self,
        risk_per_trade_pct: float | None = None,
        max_drawdown_pct: float | None = None,
    ):
        self.risk_per_trade_pct = risk_per_trade_pct or settings.RISK_PER_TRADE_PCT
        self.max_drawdown_pct = max_drawdown_pct or settings.MAX_DRAWDOWN_PCT
        self._peak_balance: float = 0

    def calculate_position_size(self, balance: float, entry_price: float, stop_loss_price: float) -> float:
        """Calculate position size based on risk percentage."""
        risk_amount = balance * (self.risk_per_trade_pct / 100)
        risk_per_unit = abs(entry_price - stop_loss_price)
        if risk_per_unit == 0:
            return 0
        size = risk_amount / risk_per_unit
        logger.info(f"Position size: {size:.6f} (risk={self.risk_per_trade_pct}% of {balance:.2f})")
        return size

    def check_drawdown(self, current_balance: float) -> bool:
        """Check if max drawdown exceeded. Returns True if trading should stop."""
        if current_balance > self._peak_balance:
            self._peak_balance = current_balance

        if self._peak_balance == 0:
            return False

        drawdown = ((self._peak_balance - current_balance) / self._peak_balance) * 100
        if drawdown >= self.max_drawdown_pct:
            logger.warning(f"Max drawdown hit: {drawdown:.1f}% (limit: {self.max_drawdown_pct}%)")
            return True
        return False

    def calculate_stop_loss(self, entry_price: float, side: str, atr: float, multiplier: float = 1.5) -> float:
        """Calculate stop loss based on ATR."""
        if side == "buy":
            return entry_price - (atr * multiplier)
        return entry_price + (atr * multiplier)

    def calculate_take_profit(self, entry_price: float, side: str, risk_reward_ratio: float = 2.0, stop_distance: float = 0) -> float:
        """Calculate take profit based on risk:reward ratio."""
        if stop_distance == 0:
            return entry_price
        tp_distance = stop_distance * risk_reward_ratio
        if side == "buy":
            return entry_price + tp_distance
        return entry_price - tp_distance
