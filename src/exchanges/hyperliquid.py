from hyperliquid.info import Info
from hyperliquid.exchange import Exchange as HLExchange
from hyperliquid.utils import constants
from config import settings
from src.exchanges.base import BaseExchange, Order, OrderSide, OrderType, Position
from src.utils.logger import logger


class HyperliquidExchange(BaseExchange):
    def __init__(self, testnet: bool = False):
        base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
        self.info = Info(base_url)
        self.exchange = HLExchange(
            settings.HYPERLIQUID_ACCOUNT_ADDRESS,
            settings.HYPERLIQUID_API_KEY,
            base_url,
        )
        logger.info(f"Hyperliquid exchange initialized ({'testnet' if testnet else 'mainnet'})")

    def fetch_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 100) -> list:
        # Hyperliquid uses "1h" style timeframes
        candles = self.info.candles_snapshot(symbol, interval=timeframe, count=limit)
        return [
            [c["t"], float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"]), float(c["v"])]
            for c in candles
        ]

    def fetch_balance(self) -> dict:
        user_state = self.info.user_state(settings.HYPERLIQUID_ACCOUNT_ADDRESS)
        margin = user_state.get("marginSummary", {})
        return {
            "free": float(margin.get("withdrawable", 0)),
            "used": float(margin.get("totalMarginUsed", 0)),
            "total": float(margin.get("accountValue", 0)),
        }

    def place_order(self, symbol: str, side: OrderSide, amount: float, order_type: OrderType = OrderType.MARKET, price: float | None = None) -> Order:
        is_buy = side == OrderSide.BUY
        if order_type == OrderType.MARKET:
            result = self.exchange.market_open(symbol, is_buy, size=amount)
        else:
            result = self.exchange.limit_order(symbol, is_buy, size=amount, limit_price=price)

        logger.info(f"Hyperliquid order: {side.value} {amount} {symbol}")
        status_data = result.get("status", {})
        return Order(
            id=str(status_data.get("resting", {}).get("oid", "")),
            symbol=symbol,
            side=side,
            type=order_type,
            price=price or 0,
            amount=amount,
            status="open" if "resting" in status_data else "filled",
        )

    def cancel_order(self, order_id: str, symbol: str) -> dict:
        result = self.exchange.cancel(symbol, int(order_id))
        logger.info(f"Hyperliquid order cancelled: {order_id}")
        return result

    def get_positions(self) -> list[Position]:
        user_state = self.info.user_state(settings.HYPERLIQUID_ACCOUNT_ADDRESS)
        positions = []
        for pos in user_state.get("assetPositions", []):
            if float(pos["position"]["szi"]) != 0:
                positions.append(Position(
                    symbol=pos["position"]["coin"],
                    side="long" if float(pos["position"]["szi"]) > 0 else "short",
                    size=abs(float(pos["position"]["szi"])),
                    entry_price=float(pos["position"]["entryPx"]),
                    unrealized_pnl=float(pos["position"]["unrealizedPnl"]),
                ))
        return positions

    def get_ticker(self, symbol: str) -> dict:
        mid = self.info.mid_price(symbol)
        all_mids = self.info.all_mids()
        return {
            "symbol": symbol,
            "last": float(mid) if mid else 0,
            "bid": float(all_mids.get(symbol, 0)),
        }
