import argparse
import sys
import time

from config import settings
from src.data.market import fetch_ohlcv_df
from src.data.websocket import PriceStream
from src.strategy.combined import generate_signal
from src.ai.agent import AIAgent
from src.execution.order import OrderExecutor
from src.execution.risk import RiskManager
from src.exchanges.binance import BinanceExchange
from src.exchanges.hyperliquid import HyperliquidExchange
from src.utils.logger import logger
from src.utils.telegram import telegram


def get_exchange(name: str):
    if name == "binance":
        return BinanceExchange()
    elif name == "hyperliquid":
        return HyperliquidExchange()
    else:
        raise ValueError(f"Unknown exchange: {name}")


def cmd_status(args):
    """Show current positions and balance."""
    exchange = get_exchange(args.exchange)
    balance = exchange.fetch_balance()
    positions = exchange.get_positions()

    print(f"\n{'='*50}")
    print(f"  Exchange: {args.exchange.upper()}")
    print(f"  Balance: {balance['total']:.2f} USDT (free: {balance['free']:.2f})")
    print(f"{'='*50}")

    if positions:
        print(f"\n  Open Positions:")
        for pos in positions:
            pnl_sign = "+" if pos.unrealized_pnl >= 0 else ""
            print(f"    {pos.symbol} | {pos.side.upper()} | size: {pos.size} | entry: {pos.entry_price} | PnL: {pnl_sign}{pos.unrealized_pnl:.2f}")
    else:
        print("\n  No open positions")
    print()


def cmd_run(args):
    """Run the trading bot."""
    exchange = get_exchange(args.exchange)
    executor = OrderExecutor(exchange)
    risk_mgr = RiskManager()
    ai_agent = AIAgent()

    # Wire Telegram callbacks
    telegram.set_callbacks(
        get_balance=lambda: exchange.fetch_balance(),
        get_positions=lambda: exchange.get_positions(),
        get_trades=lambda: executor.history,
        stop_bot=lambda: sys.exit(0),
    )
    telegram.start_command_listener()
    telegram.send_message(
        f"🤖 <b>Agentic Trade Bot started</b>\n"
        f"Exchange: <code>{args.exchange}</code>\n"
        f"Pairs: <code>{', '.join(settings.TRADING_PAIRS)}</code>\n"
        f"Timeframe: <code>{args.timeframe}</code>"
    )

    price_stream = PriceStream()
    for symbol in settings.TRADING_PAIRS:
        price_stream.add_symbol(symbol.strip(), args.exchange)

    logger.info(f"Starting bot on {args.exchange} with pairs: {settings.TRADING_PAIRS}")

    try:
        while True:
            for symbol in settings.TRADING_PAIRS:
                symbol = symbol.strip()

                # Fetch market data
                df = fetch_ohlcv_df(exchange, symbol, timeframe=args.timeframe, limit=100)

                # Generate combined signal
                signal = generate_signal(df, symbol)

                # Check balance and risk
                balance = exchange.fetch_balance()
                if risk_mgr.check_drawdown(balance["total"]):
                    logger.warning("Max drawdown reached. Stopping bot.")
                    telegram.send_error_alert(f"Max drawdown reached! Balance: {balance['total']:.2f} USDT")
                    price_stream.stop_all()
                    sys.exit(1)

                # AI final decision
                decision = ai_agent.decide(signal, symbol, balance)
                logger.info(f"AI Decision for {symbol}: {decision['action']} (confidence: {decision.get('confidence', 0)})")
                logger.info(f"  Reasoning: {decision.get('reasoning', 'N/A')}")

                # Execute if confidence threshold met
                if decision["action"] in ("buy", "sell") and decision.get("confidence", 0) >= args.min_confidence:
                    amount_pct = decision.get("amount_pct", 10) / 100
                    amount = (balance["free"] * amount_pct) / df["close"].iloc[-1]
                    if amount > 0:
                        executor.execute(symbol, decision["action"], amount)

                # Live price display
                live_price = price_stream.get_price(symbol)
                if live_price:
                    print(f"  [{symbol}] ${live_price:.2f} | Signal: {signal.action.value} | AI: {decision['action']}", flush=True)

            logger.info(f"Cycle complete. Sleeping {args.interval}s...")
            time.sleep(args.interval)

    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
        telegram.send_message("🛑 <b>Bot stopped by user</b>")
        price_stream.stop_all()
    finally:
        telegram.stop()


def cmd_backtest(args):
    """Backtest the strategy on historical data."""
    from src.backtest.engine import run_backtest

    symbol = args.symbol
    df = None

    # Try fetching real data — Hyperliquid first, then CCXT exchanges
    try:
        import requests as _req
        import time as _time
        import pandas as pd

        coin = symbol.replace("/USDC", "").replace("/USDT", "").replace("-USDC", "").replace("-USDT", "")
        now_ms = int(_time.time() * 1000)
        start_ms = now_ms - (args.limit * 3600 * 1000)
        resp = _req.post('https://api.hyperliquid.xyz/info', json={
            'type': 'candleSnapshot',
            'req': {'coin': coin, 'interval': args.timeframe, 'startTime': start_ms, 'endTime': now_ms}
        }, timeout=15)
        candles = resp.json()
        if candles:
            df = pd.DataFrame([{
                'timestamp': c['t'], 'open': float(c['o']), 'high': float(c['h']),
                'low': float(c['l']), 'close': float(c['c']), 'volume': float(c['v']),
            } for c in candles])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            logger.info(f"Fetched {len(df)} {args.timeframe} candles for {coin} from Hyperliquid")
    except Exception:
        pass

    if df is None:
        try:
            import ccxt
            for exchange_name in ["gate", "kucoin", "binance", "bybit", "okx"]:
                try:
                    ex = getattr(ccxt, exchange_name)()
                    logger.info(f"Fetching {args.limit} {args.timeframe} candles for {symbol} via {exchange_name}...")
                    ohlcv = ex.fetch_ohlcv(symbol, args.timeframe, limit=args.limit)
                    df = fetch_ohlcv_df_from_raw(ohlcv)
                    logger.info(f"Successfully fetched {len(df)} candles from {exchange_name}")
                    break
                except Exception:
                    continue
        except Exception:
            pass

    if df is None:
        logger.warning("Could not fetch live data from any source")
        logger.info("Generating synthetic market data for backtest...")
        df = generate_synthetic_data(symbol, args.limit)

    logger.info(f"Running backtest with {args.balance} USDT starting balance...")

    result = run_backtest(
        df=df,
        symbol=symbol,
        initial_balance=args.balance,
        risk_per_trade_pct=args.risk,
        leverage=args.leverage,
        use_sentiment=args.sentiment,
    )

    # Print results
    print(f"\n{'='*55}")
    print(f"  BACKTEST RESULTS — {symbol} ({args.timeframe})")
    print(f"{'='*55}")
    print(f"  Starting Balance : {result.initial_balance:.2f} USDT")
    print(f"  Final Balance    : {result.final_balance:.2f} USDT")
    pnl_sign = "+" if result.total_pnl >= 0 else ""
    print(f"  Total PnL        : {pnl_sign}{result.total_pnl:.2f} USDT ({pnl_sign}{result.total_pnl_pct:.2f}%)")
    print(f"  Max Drawdown     : -{result.max_drawdown_pct:.2f}%")
    print(f"  Sharpe Ratio     : {result.sharpe_ratio:.2f}")
    print(f"{'='*55}")
    print(f"  Total Trades     : {result.total_trades}")
    print(f"  Wins / Losses    : {result.wins} / {result.losses}")
    print(f"  Win Rate         : {result.win_rate:.1f}%")
    print(f"{'='*55}")

    if result.trades:
        print(f"\n  {'No.':<4} {'Side':<6} {'Entry':<12} {'Exit':<12} {'Size':<10} {'PnL':<12}")
        print(f"  {'-'*56}")
        for i, t in enumerate(result.trades[:20], 1):
            pnl_s = f"{'+'if t.pnl > 0 else ''}{t.pnl:.2f}"
            print(f"  {i:<4} {t.side.upper():<6} {t.entry_price:<12.2f} {t.exit_price:<12.2f} {t.size:<10.6f} {pnl_s:<12}")
        if len(result.trades) > 20:
            print(f"  ... and {len(result.trades) - 20} more trades")
    print()

    # Generate TradingView chart
    try:
        from src.backtest.chart import generate_chart
        report_path = generate_chart(result, df, symbol, args.timeframe)
        print(f"  Chart report: {report_path}")
    except Exception as e:
        logger.warning(f"Chart generation failed: {e}")
    print()


def fetch_ohlcv_df_from_raw(raw: list):
    """Convert raw OHLCV list to DataFrame."""
    import pandas as pd
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    return df


def generate_synthetic_data(symbol: str, limit: int = 500):
    """Generate realistic synthetic OHLCV data for backtesting."""
    import numpy as np
    import pandas as pd

    # Set base price based on symbol
    base_prices = {"BTC/USDT": 80000, "ETH/USDT": 3000, "SOL/USDT": 150}
    base_price = base_prices.get(symbol, 100)

    np.random.seed(42)
    dates = pd.date_range(end=pd.Timestamp.now(), periods=limit, freq="1h")

    # Random walk with trend cycles
    returns = np.random.normal(0.0001, 0.012, limit)
    # Add trend cycles (sinusoidal)
    trend = np.sin(np.linspace(0, 8 * np.pi, limit)) * 0.002
    returns += trend

    close = base_price * np.exp(np.cumsum(returns))
    high = close * (1 + np.abs(np.random.normal(0, 0.005, limit)))
    low = close * (1 - np.abs(np.random.normal(0, 0.005, limit)))
    open_prices = close * (1 + np.random.normal(0, 0.003, limit))
    volume = np.random.uniform(100, 5000, limit)

    df = pd.DataFrame({
        "open": open_prices,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }, index=dates)
    return df


def main():
    parser = argparse.ArgumentParser(description="Agentic Trade - AI Trading Bot")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # run command
    run_parser = subparsers.add_parser("run", help="Start the trading bot")
    run_parser.add_argument("--exchange", default=settings.DEFAULT_EXCHANGE, choices=["binance", "hyperliquid"])
    run_parser.add_argument("--timeframe", default="1h", help="Candle timeframe (1m, 5m, 15m, 1h, 4h, 1d)")
    run_parser.add_argument("--interval", type=int, default=300, help="Seconds between analysis cycles")
    run_parser.add_argument("--min-confidence", type=float, default=0.5, help="Minimum confidence to execute trades")
    run_parser.set_defaults(func=cmd_run)

    # status command
    status_parser = subparsers.add_parser("status", help="Show positions and balance")
    status_parser.add_argument("--exchange", default=settings.DEFAULT_EXCHANGE, choices=["binance", "hyperliquid"])
    status_parser.set_defaults(func=cmd_status)

    # backtest command
    bt_parser = subparsers.add_parser("backtest", help="Backtest strategy on historical data")
    bt_parser.add_argument("--symbol", default="BTC/USDT", help="Trading pair (e.g. BTC/USDT)")
    bt_parser.add_argument("--timeframe", default="1h", help="Candle timeframe")
    bt_parser.add_argument("--limit", type=int, default=500, help="Number of candles to fetch")
    bt_parser.add_argument("--balance", type=float, default=50.0, help="Starting balance in USDT")
    bt_parser.add_argument("--risk", type=float, default=2.0, help="Risk %% per trade")
    bt_parser.add_argument("--leverage", type=float, default=20.0, help="Leverage (default: 20x)")
    bt_parser.add_argument("--sentiment", action="store_true", help="Enable Tavily sentiment filter")
    bt_parser.set_defaults(func=cmd_backtest)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    args.func(args)


if __name__ == "__main__":
    main()
