import json
import threading
import time
import websocket
from src.utils.logger import logger


class BinanceWebSocket:
    def __init__(self, symbol: str, on_price_update):
        self.symbol = symbol.lower().replace("/", "")
        self.on_price_update = on_price_update
        self._ws = None
        self._thread = None
        self._running = False

    def _on_message(self, ws, message):
        data = json.loads(message)
        price = float(data["c"])  # current/last price
        self.on_price_update(self.symbol, price)

    def _on_error(self, ws, error):
        logger.error(f"Binance WS error: {error}")

    def _on_close(self, ws, close_status, close_msg):
        logger.info(f"Binance WS closed for {self.symbol}")
        if self._running:
            time.sleep(2)
            self.start()

    def _on_open(self, ws):
        logger.info(f"Binance WS connected for {self.symbol}")

    def start(self):
        self._running = True
        url = f"wss://stream.binance.com:9443/ws/{self.symbol}@ticker"
        self._ws = websocket.WebSocketApp(
            url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._thread = threading.Thread(target=self._ws.run_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._ws:
            self._ws.close()


class PriceStream:
    """Manages multiple WebSocket price streams."""

    def __init__(self):
        self._streams: dict[str, BinanceWebSocket] = {}
        self._latest_prices: dict[str, float] = {}

    def add_symbol(self, symbol: str, exchange: str = "binance"):
        if exchange == "binance":
            ws = BinanceWebSocket(symbol, self._on_price)
            self._streams[symbol] = ws
            ws.start()

    def _on_price(self, symbol: str, price: float):
        self._latest_prices[symbol] = price

    def get_price(self, symbol: str) -> float | None:
        return self._latest_prices.get(symbol)

    def stop_all(self):
        for ws in self._streams.values():
            ws.stop()
        self._streams.clear()
