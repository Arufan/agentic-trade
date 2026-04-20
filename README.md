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
| `BINANCE_API_KEY` / `_SECRET` | Binance spot API (optional) |
| `HYPERLIQUID_API_KEY` | Wallet private key for Hyperliquid |
| `HYPERLIQUID_ACCOUNT_ADDRESS` | Wallet address |
| `ANTHROPIC_API_KEY` | Anthropic key for AI agent / macro regime / sentiment LLM |
| `LLM_API_KEY` / `LLM_BASE_URL` | Optional — point at an Anthropic-compatible proxy (Z.AI, OpenRouter...). Takes precedence over `ANTHROPIC_API_KEY`. |
| `LLM_MODEL` | Model id to use (default `claude-sonnet-4-6`) |
| `TAVILY_API_KEY` / `_BACKUP` | News retrieval for sentiment |
| `TRADING_PAIRS` | Comma-separated pairs (e.g. `BTC/USDC,ETH/USDC`) |
| `DEFAULT_EXCHANGE` | `hyperliquid` or `binance` |
| `RISK_PER_TRADE_PCT` | Risk % per trade (default `2.0`) |
| `MAX_DRAWDOWN_PCT` | Drawdown kill-switch from persisted peak (default `10.0`) |
| `MAX_TOTAL_EXPOSURE` | Notional exposure cap, fraction of balance (default `0.5`) |
| `MIN_CONFIDENCE` | Min combined-signal confidence to trade (default `0.7`) |
| `MAX_POSITIONS` | Max concurrent open positions (default `2`) |
| `MAX_SAME_DIRECTION` | Max same-side positions (default `2`) |
| `MAX_PER_CLUSTER` | Cap across correlated assets (BTC/ETH/SOL share L1_MAJOR) — default `1` |
| `MIN_TRADE_SIZE_USDT` | Floor notional per trade (default `10`) |
| `MAX_TRADE_SIZE_USDT` | Cap per trade (default `50`) |
| `SLIPPAGE_BPS` | Backtest slippage in bps, applied symmetrically (default `5`) |
| `FEE_BPS` | Taker fee per side in bps (default `5`, matches Hyperliquid) |
| `SENTIMENT_WEIGHT` | Weight of sentiment in combined signal, 0-0.5 (default `0.15`) |
| `FUNDING_ENABLED` | Use Hyperliquid funding rate as a size / skip filter (default `true`) |
| `FUNDING_EXTREME_ANNUAL` | Adverse *annualized* funding that halves size (default `0.30`) |
| `FUNDING_SKIP_ANNUAL` | Adverse annualized funding that blocks the trade (default `0.60`) |
| `VOL_TARGET_ENABLED` | Use realized-vol targeting instead of fixed-% risk sizing (default `true`) |
| `TARGET_DAILY_VOL_PCT` | Desired daily P&L volatility as % of balance (default `1.0`) |
| `TELEGRAM_*` | Optional Telegram bot notifications |

### Drawdown persistence

`MAX_DRAWDOWN_PCT` is measured against a `peak_balance` that is persisted to
`data/state.json`. Restarts don't reset the peak, so the kill-switch keeps
working across crashes and redeploys. The same file also stores live trailing
stop state (per `symbol:side`).

### Correlation clusters

To prevent "triple-long-L1" concentration, symbols are grouped into clusters
(`L1_MAJOR`, `MEME`, `L2_ROLLUP`, `COMMODITY`, ...) in `src/execution/risk.py`.
The `MAX_PER_CLUSTER` knob caps how many positions may be open inside a single
cluster at once.

### Sentiment

Sentiment is classified with an LLM (using the same `LLM_*` credentials as the
AI agent) over Tavily news headlines, with a conservative keyword counter as
fallback when the LLM is unavailable. Because headline sentiment is noisy,
`SENTIMENT_WEIGHT` defaults to `0.15` — it nudges rather than drives signals.

### Funding-rate filter

On Hyperliquid, perps pay *funding* every hour — longs pay shorts when the
rate is positive and vice-versa. Extreme positive funding usually means longs
are crowded, which is a contra-signal for a new long. Before each trade the
bot pulls the current funding rate from Hyperliquid (`metaAndAssetCtxs`),
annualizes it (`rate × 24 × 365`), and checks whether it's *adverse* to the
intended direction. If the adverse annualized rate ≥ `FUNDING_EXTREME_ANNUAL`
size is halved; if it's ≥ `FUNDING_SKIP_ANNUAL` the trade is skipped outright.
Spot exchanges (and symbols without funding data) pass through as no-ops.

### Volatility-targeting sizing

Instead of sizing with a flat 2 % risk and an ATR-derived stop, the default
path now estimates realized daily volatility from the last ~48 1-hour bars
(log returns, annualized via `sqrt(24)`) and picks a notional so that expected
daily P&L vol ≈ `TARGET_DAILY_VOL_PCT%` × balance. This keeps risk
contribution comparable across high-vol and low-vol symbols — a 1 % daily vol
target means BTC at 3 %/day gets about a third of the notional SOL at 1 %/day
would get. When the series is too short or vol can't be estimated, the code
falls back to the original ATR sizing path.

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
# Start the live trading loop
python -m src.main run --exchange hyperliquid --timeframe 1h --interval 300

# Check positions and balance
python -m src.main status --exchange hyperliquid

# In-sample backtest on real OHLCV (refuses synthetic data by default)
python -m src.main backtest --symbol BTC/USDT --timeframe 1h --limit 500

# Walk-forward (out-of-sample) evaluation — the honest one
python -m src.main walkforward --symbol BTC/USDT --timeframe 1h --limit 2000 \
    --train-window 500 --test-window 200

# Run unit tests
python -m pytest tests/ -v
```

### Backtest realism

The backtest engine applies slippage (buys pay up, sells take down) and
fees on both legs, both configurable via `SLIPPAGE_BPS` / `FEE_BPS` or per-run
flags (`--slippage-bps`, `--fee-bps`, `--min-strength`). Pass `--allow-synthetic`
only for plumbing tests; synthetic sinusoidal candles massively overstate edge.

### Walk-forward

`walkforward` splits history into rolling `(train, test)` windows, picks the
best parameter combo on train, and evaluates it on the immediately following
test window. OOS PnL is compounded across folds, giving a far more honest
signal of whether the strategy generalises.

## Disclaimer

This software is for educational purposes only. Trading cryptocurrencies involves significant risk. Use at your own risk. Always test with small amounts first.

## License

MIT
