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

    def cancel_trigger_orders(self, symbol: str) -> int:
        """Cancel all trigger (SL/TP) orders for `symbol`. Default: no-op."""
        return 0

    def get_funding_rate(self, symbol: str) -> float:
        """Return the current per-hour funding rate for a perp symbol (e.g.
        0.0001 means 0.01 % / hour ≈ 87.6 % annualized). Spot exchanges have
        no funding, so the default returns 0.0 and the funding filter becomes
        a no-op."""
        return 0.0

    def get_open_interest(self, symbol: str) -> float:
        """Return the current open interest for a perp symbol, expressed in
        BASE-asset units (e.g. 1234.5 BTC). Spot exchanges have no OI, so
        the default returns 0.0 and any OI-based alpha becomes a no-op.

        Callers should compare deltas against the per-symbol time-series they
        persist (see src.data.market_state) rather than reading absolute
        values — OI magnitude has no universal interpretation across
        symbols."""
        return 0.0

    def place_sl_tp(self, symbol: str, close_side: str, amount: float,
                    sl_price: float, tp_price: float) -> dict:
        """Replace SL/TP triggers (e.g. after a trailing update).
        Default: not supported. Subclasses should override."""
        return {"status": "err", "response": "place_sl_tp not implemented"}

    def place_order_with_sl_tp(self, symbol: str, side, amount: float,
                               entry_price: float, sl_price: float, tp_price: float):
        """Place entry + SL/TP pair atomically. Default implementation chains
        `place_order` + `place_sl_tp`. Subclasses may override for OCO support."""
        entry = self.place_order(symbol, side, amount)
        close_side = "sell" if (getattr(side, "value", side) == "buy") else "buy"
        self.place_sl_tp(symbol, close_side, amount, sl_price, tp_price)
        return entry, sl_price, tp_price
