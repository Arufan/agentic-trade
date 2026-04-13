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
        price_stream.stop_all()


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

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    args.func(args)


if __name__ == "__main__":
    main()
