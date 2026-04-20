import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # Exchange
    BINANCE_API_KEY: str = os.getenv("BINANCE_API_KEY", "")
    BINANCE_API_SECRET: str = os.getenv("BINANCE_API_SECRET", "")

    HYPERLIQUID_API_KEY: str = os.getenv("HYPERLIQUID_API_KEY", "")
    HYPERLIQUID_ACCOUNT_ADDRESS: str = os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS", "")
    HYPERLIQUID_WALLET_ADDRESS: str = os.getenv("WALLET_ADDRESS", "")

    # AI — allow Anthropic-compatible proxies (Z.AI, OpenRouter, etc.)
    # LLM_API_KEY takes precedence over ANTHROPIC_API_KEY when both are set.
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", "") or os.getenv("ANTHROPIC_API_KEY", "")
    LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "") or os.getenv("ANTHROPIC_BASE_URL", "")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "claude-sonnet-4-6")
    TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "")
    TAVILY_API_KEY_BACKUP: str = os.getenv("TAVILY_API_KEY_BACKUP", "")

    # Trading
    TRADING_PAIRS: list[str] = os.getenv("TRADING_PAIRS", "BTC/USDT,ETH/USDT").split(",")
    DEFAULT_EXCHANGE: str = os.getenv("DEFAULT_EXCHANGE", "binance")
    RISK_PER_TRADE_PCT: float = float(os.getenv("RISK_PER_TRADE_PCT", "2.0"))
    MAX_DRAWDOWN_PCT: float = float(os.getenv("MAX_DRAWDOWN_PCT", "10.0"))
    MAX_TOTAL_EXPOSURE: float = float(os.getenv("MAX_TOTAL_EXPOSURE", "0.5"))
    MIN_CONFIDENCE: float = float(os.getenv("MIN_CONFIDENCE", "0.7"))
    MAX_POSITIONS: int = int(os.getenv("MAX_POSITIONS", "2"))
    MAX_SAME_DIRECTION: int = int(os.getenv("MAX_SAME_DIRECTION", "2"))
    MAX_TRADE_SIZE_USDT: float = float(os.getenv("MAX_TRADE_SIZE_USDT", "50.0"))
    MIN_TRADE_SIZE_USDT: float = float(os.getenv("MIN_TRADE_SIZE_USDT", "10.0"))

    # Sentiment weight in combined signal (0.0–1.0). Keep low; keyword/LLM
    # sentiment is noisy so we use it as a light tiebreaker, not a main driver.
    SENTIMENT_WEIGHT: float = float(os.getenv("SENTIMENT_WEIGHT", "0.15"))

    # Correlated-asset cluster cap: max positions across BTC/ETH/SOL, stables, etc.
    MAX_PER_CLUSTER: int = int(os.getenv("MAX_PER_CLUSTER", "1"))

    # Slippage model (basis points) — applied symmetrically in backtest
    SLIPPAGE_BPS: float = float(os.getenv("SLIPPAGE_BPS", "5.0"))
    FEE_BPS: float = float(os.getenv("FEE_BPS", "5.0"))

    # Funding-rate filter (Hyperliquid perps). Extreme positive funding means
    # longs are crowded and paying shorts → fade bullish signals. The rate is
    # quoted per-hour; we annualize (× 24 × 365) before comparing.
    FUNDING_ENABLED: bool = os.getenv("FUNDING_ENABLED", "true").lower() == "true"
    FUNDING_EXTREME_ANNUAL: float = float(os.getenv("FUNDING_EXTREME_ANNUAL", "0.30"))  # 30%  → halve size
    FUNDING_SKIP_ANNUAL: float = float(os.getenv("FUNDING_SKIP_ANNUAL", "0.60"))        # 60%  → skip

    # Volatility-targeting position sizing. When enabled, notional is chosen
    # such that realized daily vol × notional ≈ TARGET_DAILY_VOL_PCT × balance.
    # This keeps risk contribution roughly constant across high- and low-vol
    # symbols rather than sizing off ATR (which scales with price).
    VOL_TARGET_ENABLED: bool = os.getenv("VOL_TARGET_ENABLED", "true").lower() == "true"
    TARGET_DAILY_VOL_PCT: float = float(os.getenv("TARGET_DAILY_VOL_PCT", "1.0"))       # 1 % of balance / day

    # Alpha engine (OI anomaly + funding contrarian). When enabled, the alpha
    # layer runs in parallel to tech+sentiment and its score is blended into
    # the combined signal with weight ALPHA_WEIGHT. Thresholds below control
    # individual module sensitivity.
    ALPHA_ENABLED: bool = os.getenv("ALPHA_ENABLED", "true").lower() == "true"
    ALPHA_WEIGHT: float = float(os.getenv("ALPHA_WEIGHT", "0.25"))                     # 0.25 of combined score

    # OI anomaly module
    ALPHA_OI_ENABLED: bool = os.getenv("ALPHA_OI_ENABLED", "true").lower() == "true"
    ALPHA_OI_LOOKBACK_SEC: int = int(os.getenv("ALPHA_OI_LOOKBACK_SEC", str(4 * 3600)))  # 4 hours
    ALPHA_OI_THRESHOLD: float = float(os.getenv("ALPHA_OI_THRESHOLD", "0.10"))           # 10 % OI change
    ALPHA_PRICE_THRESHOLD: float = float(os.getenv("ALPHA_PRICE_THRESHOLD", "0.02"))     # 2 % price change

    # Funding contrarian module (DIFFERENT from funding filter)
    ALPHA_FUNDING_CONTRARIAN_ENABLED: bool = os.getenv("ALPHA_FUNDING_CONTRARIAN_ENABLED", "true").lower() == "true"
    ALPHA_FUNDING_LOOKBACK_SEC: int = int(os.getenv("ALPHA_FUNDING_LOOKBACK_SEC", str(12 * 3600)))  # 12 hours
    ALPHA_FUNDING_CONTRARIAN_ANNUAL: float = float(os.getenv("ALPHA_FUNDING_CONTRARIAN_ANNUAL", "0.50"))  # 50 % annual
    ALPHA_FUNDING_MIN_PRICE_MOVE: float = float(os.getenv("ALPHA_FUNDING_MIN_PRICE_MOVE", "0.02"))       # 2 % move

    # Daily-loss kill-switch. Pauses new entries for DAILY_LOCK_HOURS after a
    # single-day loss >= DAILY_LOSS_KILL_PCT (fraction of the UTC-day anchor
    # balance). Existing positions + SL/TP stay managed under the normal path.
    # Disabled when DAILY_LOSS_KILL_PCT <= 0.
    DAILY_LOSS_KILL_PCT: float = float(os.getenv("DAILY_LOSS_KILL_PCT", "0.05"))  # 5 % of day-start
    DAILY_LOCK_HOURS: float = float(os.getenv("DAILY_LOCK_HOURS", "24"))

    # Telegram
    TELEGRAM_ENABLED: bool = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")


settings = Settings()
