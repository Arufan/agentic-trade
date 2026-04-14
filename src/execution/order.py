from src.exchanges.base import BaseExchange, Order, OrderSide, OrderType
from src.utils.logger import logger
from src.utils.telegram import telegram


class OrderExecutor:
    def __init__(self, exchange: BaseExchange):
        self.exchange = exchange
        self._order_history: list[Order] = []

    def execute(self, symbol: str, action: str, amount: float, order_type: OrderType = OrderType.MARKET, price: float | None = None, sl: float = 0, tp: float = 0) -> Order | None:
        """Execute a trade order."""
        if action not in ("buy", "sell"):
            logger.info(f"No execution needed: action={action}")
            return None

        side = OrderSide.BUY if action == "buy" else OrderSide.SELL
        try:
            order = self.exchange.place_order(
                symbol=symbol,
                side=side,
                amount=amount,
                order_type=order_type,
                price=price,
            )
            self._order_history.append(order)
            fill_price = order.price or price or 0
            telegram.send_trade_alert(action, symbol, fill_price, amount, sl=sl, tp=tp)
            return order
        except Exception as e:
            logger.error(f"Order execution failed: {e}")
            telegram.send_error_alert(f"Order failed: {action} {symbol} — {e}")
            return None

    @property
    def history(self) -> list[Order]:
        return self._order_history
