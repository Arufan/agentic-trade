import ccxt
from config import settings
from src.exchanges.base import BaseExchange, Order, OrderSide, OrderType, Position
from src.utils.logger import logger


class BinanceExchange(BaseExchange):
    def __init__(self):
        self.exchange = ccxt.binance({
            "apiKey": settings.BINANCE_API_KEY,
            "secret": settings.BINANCE_API_SECRET,
            "options": {"defaultType": "future"},
        })
        logger.info("Binance exchange initialized (futures mode)")

    def fetch_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 100) -> list:
        return self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)

    def fetch_balance(self) -> dict:
        balance = self.exchange.fetch_balance()
        usdt = balance.get("USDT", {})
        return {
            "free": usdt.get("free", 0),
            "used": usdt.get("used", 0),
            "total": usdt.get("total", 0),
        }

    def place_order(self, symbol: str, side: OrderSide, amount: float, order_type: OrderType = OrderType.MARKET, price: float | None = None) -> Order:
        params = {}
        if order_type == OrderType.LIMIT and price:
            params["price"] = price

        result = self.exchange.create_order(
            symbol=symbol,
            type=order_type.value,
            side=side.value,
            amount=amount,
            price=price,
            params=params,
        )
        logger.info(f"Order placed: {side.value} {amount} {symbol} @ {order_type.value}")
        return Order(
            id=result["id"],
            symbol=result["symbol"],
            side=OrderSide(result["side"]),
            type=OrderType(result["type"]),
            price=result.get("price", 0),
            amount=result["amount"],
            status=result["status"],
        )

    def cancel_order(self, order_id: str, symbol: str) -> dict:
        result = self.exchange.cancel_order(order_id, symbol)
        logger.info(f"Order cancelled: {order_id}")
        return result

    def get_positions(self) -> list[Position]:
        positions = self.exchange.fetch_positions()
        return [
            Position(
                symbol=p["symbol"],
                side=p["side"],
                size=float(p["contracts"]),
                entry_price=float(p["entryPrice"]),
                unrealized_pnl=float(p["unrealizedPnl"]),
            )
            for p in positions
            if float(p["contracts"]) > 0
        ]

    def get_ticker(self, symbol: str) -> dict:
        return self.exchange.fetch_ticker(symbol)
