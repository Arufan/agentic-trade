import pandas as pd
from src.exchanges.base import BaseExchange
from src.utils.logger import logger


def fetch_ohlcv_df(exchange: BaseExchange, symbol: str, timeframe: str = "1h", limit: int = 100) -> pd.DataFrame:
    """Fetch OHLCV data and return as a pandas DataFrame."""
    raw = exchange.fetch_ohlcv(symbol, timeframe, limit)
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    logger.info(f"Fetched {len(df)} candles for {symbol} ({timeframe})")
    return df
