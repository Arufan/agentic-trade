from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


@dataclass
class Order:
    id: str
    symbol: str
    side: OrderSide
    type: OrderType
    price: float
    amount: float
    status: str


@dataclass
class Position:
    symbol: str
    side: str
    size: float
    entry_price: float
    unrealized_pnl: float


class BaseExchange(ABC):
    @abstractmethod
    def fetch_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 100) -> list:
        """Fetch OHLCV candlestick data. Returns list of [timestamp, open, high, low, close, volume]."""
        ...

    @abstractmethod
    def fetch_balance(self) -> dict:
        """Fetch account balance. Returns dict with 'free', 'used', 'total' keys."""
        ...

    @abstractmethod
    def place_order(self, symbol: str, side: OrderSide, amount: float, order_type: OrderType = OrderType.MARKET, price: float | None = None) -> Order:
        """Place an order."""
        ...

    @abstractmethod
    def cancel_order(self, order_id: str, symbol: str) -> dict:
        """Cancel an existing order."""
        ...

    @abstractmethod
    def get_positions(self) -> list[Position]:
        """Get open positions."""
        ...

    @abstractmethod
    def get_ticker(self, symbol: str) -> dict:
        """Get current ticker data for a symbol."""
        ...
