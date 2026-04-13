# Agentic Trade

AI-powered automated crypto trading bot with combined technical analysis and sentiment-driven strategies.

## Features

- **Multi-Exchange Support** — Trade on Binance and Hyperliquid through a unified interface
- **Technical Analysis** — RSI, MACD, Bollinger Bands, and EMA indicators with scoring system
- **AI Sentiment Analysis** — Real-time crypto news sentiment via Tavily API
- **AI Trade Decisions** — Claude-powered final trade decisions with reasoning
- **Risk Management** — Position sizing, stop-loss, take-profit, and max drawdown limits
- **Live Price Streaming** — WebSocket-based real-time price feeds
- **CLI Interface** — Simple command-line control

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Exchange Connectivity | [CCXT](https://github.com/ccxt/ccxt) — 100+ exchanges, one API |
| On-Chain Perps | [Hyperliquid Python SDK](https://github.com/hyperliquid-dex/hyperliquid-python-sdk) |
| Charting | [TradingView Lightweight Charts](https://github.com/tradingview/lightweight-charts) *(planned)* |
| Sentiment Data | [Tavily API](https://tavily.com) — AI search engine for news |
| Technical Indicators | [ta](https://github.com/bukosabino/ta) library |
| AI Agent | [Anthropic Claude](https://anthropic.com) |
| Cost Optimization | [RTK](https://github.com/rtk-ai/rtk) *(planned)* |

## Quick Start

```bash
# Clone the repo
git clone https://github.com/Arufan/agentic-trade.git
cd agentic-trade

# Install dependencies
pip install -r requirements.txt

# Configure API keys
cp .env.example .env
# Edit .env with your API keys

# Check account status
python -m src.main status --exchange binance

# Start the bot
python -m src.main run --exchange binance --timeframe 1h
```

## Configuration

Copy `.env.example` to `.env` and fill in your keys:

| Variable | Description |
|----------|------------|
| `BINANCE_API_KEY` | Binance API key |
| `BINANCE_API_SECRET` | Binance API secret |
| `HYPERLIQUID_API_KEY` | Wallet private key for Hyperliquid |
| `HYPERLIQUID_ACCOUNT_ADDRESS` | Wallet address |
| `ANTHROPIC_API_KEY` | Anthropic API key for AI agent |
| `TAVILY_API_KEY` | Tavily API key for sentiment analysis |
| `TRADING_PAIRS` | Comma-separated pairs (e.g. `BTC/USDT,ETH/USDT`) |
| `RISK_PER_TRADE_PCT` | Risk % per trade (default: 2%) |
| `MAX_DRAWDOWN_PCT` | Max drawdown before bot stops (default: 10%) |

## Project Structure

```
agentic-trade/
├── config/          # Settings and env config
├── src/
│   ├── exchanges/   # Binance & Hyperliquid adapters
│   ├── data/        # Market data fetcher & WebSocket streams
│   ├── strategy/    # Technical indicators, sentiment, combined signals
│   ├── ai/          # AI agent for trade decisions
│   ├── execution/   # Order placement & risk management
│   └── utils/       # Logging and helpers
└── tests/           # Unit tests
```

## CLI Commands

```bash
# Start trading bot
python -m src.main run --exchange binance --timeframe 1h --interval 300

# Check positions and balance
python -m src.main status --exchange binance
```

## Disclaimer

This software is for educational purposes only. Trading cryptocurrencies involves significant risk. Use at your own risk. Always test with small amounts first.

## License

MIT
