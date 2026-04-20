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
from src.execution.trailing import TrailingStopManager
from src.exchanges.binance import BinanceExchange
from src.exchanges.hyperliquid import HyperliquidExchange
from src.utils.logger import logger
from src.utils.telegram import telegram
from src.utils.trade_journal import journal
from src.strategy.regime import fetch_macro_data, detect_ai_regime, AiRegimeResult, Bias
from src.strategy.funding import evaluate_funding
from src.strategy.alpha import AlphaEngine
from src.data.market_state import get_store as get_market_state_store


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

def run_screening(exchange, ai_agent=None):
    """Run a single screening cycle across all pairs. Returns formatted string for Telegram."""
    from src.utils.trade_journal import journal
    lines = ["🔍 <b>Manual Screening</b>\n"]
    history_summary = journal.get_performance_summary()

    for symbol in settings.TRADING_PAIRS:
        symbol = symbol.strip()
        try:
            df = fetch_ohlcv_df(exchange, symbol, timeframe="1h", limit=100)
            if df.empty or len(df) < 60:
                lines.append(f"<b>{symbol}</b>: ⏭ Skipped (insufficient data)")
                continue

            signal = generate_signal(df, symbol)
            price = df["close"].iloc[-1]

            # Quick summary without full AI call
            tech = signal.technical
            action_icon = {"buy": "🟢", "sell": "🔴", "hold": "⬜"}.get(tech.signal.value, "⬜")
            lines.append(
                f"<b>{symbol}</b> ${price:.2f}\n"
                f"  {action_icon} Signal: {tech.signal.value.upper()} ({tech.strength:.0%})\n"
                f"  RSI: {tech.indicators.get('rsi', '?')} | MACD: {tech.indicators.get('macd_hist', '?')}\n"
                f"  Sentiment: {signal.sentiment.sentiment.value} ({signal.sentiment.confidence:.0%})\n"
            )
        except Exception as e:
            lines.append(f"<b>{symbol}</b>: ❌ Error: {e}")

    return "\n".join(lines)


def cmd_run(args):
    """Run the trading bot."""
    exchange = get_exchange(args.exchange)
    executor = OrderExecutor(exchange)
    risk_mgr = RiskManager()
    ai_agent = AIAgent()
    trailing_mgr = TrailingStopManager(exchange)
    alpha_engine = AlphaEngine.from_settings(settings) if settings.ALPHA_ENABLED else None
    market_state = get_market_state_store()

    min_confidence = settings.MIN_CONFIDENCE

    # Wire Telegram callbacks
    telegram.set_callbacks(
        get_balance=lambda: exchange.fetch_balance(),
        get_positions=lambda: exchange.get_positions(),
        get_trades=lambda: executor.history,
        stop_bot=lambda: sys.exit(0),
        run_screening=lambda: run_screening(exchange, ai_agent),
    )
    telegram.start_command_listener()
    telegram.send_message(
        f"🤖 <b>Agentic Trade Bot started</b>\n"
        f"Exchange: <code>{args.exchange}</code>\n"
        f"Pairs: <code>{', '.join(settings.TRADING_PAIRS)}</code>\n"
        f"Timeframe: <code>{args.timeframe}</code>\n"
        f"Risk: max {settings.MAX_POSITIONS} pos, {settings.MAX_TOTAL_EXPOSURE:.0%} exposure, "
        f"min conf {settings.MIN_CONFIDENCE}, max size ${settings.MAX_TRADE_SIZE_USDT}, "
        f"scaled by confidence"
    )

    price_stream = PriceStream()
    for symbol in settings.TRADING_PAIRS:
        price_stream.add_symbol(symbol.strip(), args.exchange)

    logger.info(f"Starting bot on {args.exchange} with pairs: {settings.TRADING_PAIRS}")

    try:
        while True:
            # Early-exit: if balance is below the minimum trade size, there is
            # nothing to trade. Sleep instead of burning LLM/Tavily calls.
            try:
                pre_balance = exchange.fetch_balance()
                if pre_balance["total"] < settings.MIN_TRADE_SIZE_USDT:
                    msg = (
                        f"Balance ${pre_balance['total']:.2f} < MIN_TRADE_SIZE_USDT "
                        f"${settings.MIN_TRADE_SIZE_USDT}. Pausing cycle."
                    )
                    logger.warning(msg)
                    telegram.send_error_alert(msg)
                    time.sleep(max(args.interval, 300))
                    continue
            except Exception as e:
                logger.warning(f"Balance pre-check failed: {e}")

            # Reconcile closed positions with trade journal
            open_trades = journal.get_open_trades()
            current_positions = exchange.get_positions()
            for t in open_trades:
                matching = [p for p in current_positions if p.symbol == t["symbol"].split("/")[0]]
                if not matching:
                    # Position was closed — cancel stale trigger orders, estimate PnL
                    try:
                        exchange.cancel_trigger_orders(t["symbol"])
                        ticker = exchange.get_ticker(t["symbol"])
                        exit_price = ticker.get("last", 0)
                        entry = t["entry_price"] or 0
                        amount = t["amount"] or 0
                        if t["side"] == "buy":
                            pnl = (exit_price - entry) * amount
                        else:
                            pnl = (entry - exit_price) * amount
                        journal.close_trade(t["id"], exit_price, pnl)
                        telegram.send_message(
                            f"Position Closed\n"
                            f"{t['side'].upper()} {t['symbol']}\n"
                            f"Entry: {entry:.2f} -> Exit: {exit_price:.2f}\n"
                            f"PnL: {'+'if pnl>=0 else ''}{pnl:.4f} USDT"
                        )
                    except Exception as e:
                        logger.warning(f"Failed to close journal entry {t['id']}: {e}")
                    # Drop trailing state for any closed side
                    trailing_mgr.forget(t["symbol"], t["side"])

            # === Trailing-stop: tighten SL for any position in profit ===
            try:
                atr_by_symbol: dict[str, float] = {}
                for pos in current_positions:
                    try:
                        df_atr = fetch_ohlcv_df(exchange, pos.symbol, timeframe=args.timeframe, limit=30)
                        if not df_atr.empty and len(df_atr) >= 15:
                            from ta.volatility import AverageTrueRange
                            atr_val = AverageTrueRange(df_atr["high"], df_atr["low"], df_atr["close"], window=14).average_true_range().iloc[-1]
                            if atr_val and atr_val > 0:
                                atr_by_symbol[pos.symbol] = float(atr_val)
                    except Exception as e:
                        logger.warning(f"Trailing: ATR fetch failed {pos.symbol}: {e}")

                moves = trailing_mgr.update(current_positions, atr_by_symbol)
                for mv in moves:
                    try:
                        exchange.cancel_trigger_orders(mv["symbol"])
                        close_side = "sell" if mv["side"] == "buy" else "buy"
                        # Re-place SL+TP using existing helper
                        if hasattr(exchange, "place_sl_tp"):
                            exchange.place_sl_tp(mv["symbol"], close_side, mv["amount"], mv["new_sl"], mv["tp"])
                        telegram.send_message(
                            f"Trailing SL moved\n{mv['symbol']} {mv['side'].upper()}\n"
                            f"{mv['old_sl']:.4f} → {mv['new_sl']:.4f}"
                        )
                    except Exception as e:
                        logger.warning(f"Trailing: failed to replace SL for {mv['symbol']}: {e}")
            except Exception as e:
                logger.warning(f"Trailing stop update failed: {e}")

            # Load performance summary for AI context
            history_summary = journal.get_performance_summary()

            # === AI MACRO REGIME — called ONCE per cycle ===
            ai_regime = None
            try:
                from src.strategy.sentiment import analyze_sentiment
                btc_sentiment = analyze_sentiment("BTC/USDC")
                macro = fetch_macro_data(
                    sentiment_summary=btc_sentiment.summary,
                    sentiment_label=btc_sentiment.sentiment.value,
                )
                ai_regime = detect_ai_regime(macro)
                logger.info(
                    f"AI Macro Regime: {ai_regime.regime.value} (conf={ai_regime.confidence:.2f}) "
                    f"bias={ai_regime.bias.value} | {ai_regime.reasoning}"
                )
            except Exception as e:
                logger.warning(f"AI macro regime failed: {e}")

            for symbol in settings.TRADING_PAIRS:
                symbol = symbol.strip()

                # Fetch market data (1H for execution)
                df = fetch_ohlcv_df(exchange, symbol, timeframe=args.timeframe, limit=100)
                if df.empty or len(df) < 60:
                    logger.info(f"Skipping {symbol}: insufficient data ({len(df)} candles)")
                    continue

                # Fetch 4H data for regime detection (more stable)
                df_regime = None
                try:
                    df_regime = fetch_ohlcv_df(exchange, symbol, timeframe="4h", limit=100)
                except Exception as e:
                    logger.warning(f"Failed to fetch 4H data for {symbol}: {e}")

                # --- ALPHA LAYER --- #
                # Capture the current market snapshot into the rolling store,
                # then let the alpha engine emit a parallel signal from
                # OI/funding history. Errors here must never block the
                # primary signal path.
                alpha_result = None
                try:
                    current_price = float(df["close"].iloc[-1])
                    current_oi = exchange.get_open_interest(symbol)
                    current_funding = exchange.get_funding_rate(symbol)
                    market_state.append(symbol, current_price, current_oi, current_funding)
                    if alpha_engine is not None:
                        alpha_result = alpha_engine.evaluate(
                            symbol=symbol,
                            current_price=current_price,
                            current_oi=current_oi,
                            funding_1h=current_funding,
                            store=market_state,
                        )
                except Exception as e:
                    logger.warning(f"Alpha engine failed for {symbol}: {e}")

                # Generate combined signal (4H regime + 1H execution + AI macro + alpha)
                signal = generate_signal(
                    df, symbol, df_regime=df_regime, ai_regime=ai_regime,
                    alpha=alpha_result,
                )

                # Check balance and risk
                balance = exchange.fetch_balance()
                if risk_mgr.check_drawdown(balance["total"]):
                    logger.warning("Max drawdown reached. Stopping bot.")
                    telegram.send_error_alert(f"Max drawdown reached! Balance: {balance['total']:.2f} USDT")
                    price_stream.stop_all()
                    sys.exit(1)

                # Daily-loss kill-switch — pause new entries for 24h if
                # intraday loss exceeds DAILY_LOSS_KILL_PCT. Does NOT stop the
                # process; open positions still get managed (SL/TP/trailing).
                daily_blocked, daily_reason = risk_mgr.check_daily_loss(balance["total"])
                if daily_blocked:
                    logger.warning(f"[{symbol}] skip entry: {daily_reason}")
                    # Still allow exits + trailing to run below; skip only new-entry path.
                    telegram.send_error_alert(f"Daily-loss kill-switch: {daily_reason}")
                    continue

                # AI final decision (with trade history context)
                decision = ai_agent.decide(signal, symbol, balance, history_summary=history_summary)
                logger.info(f"AI Decision for {symbol}: {decision['action']} (confidence: {decision.get('confidence', 0)})")
                logger.info(f"  Reasoning: {decision.get('reasoning', 'N/A')}")

                # Log regime state
                regime = signal.regime
                blended = signal.blended_regime
                logger.info(f"  Regime: {regime.regime.value} (score={regime.score:.2f}) | Blended: {blended.regime.value} AI={blended.ai_regime}")

                # Execute if confidence threshold met + risk checks pass
                if decision["action"] in ("buy", "sell") and decision.get("confidence", 0) >= min_confidence:
                    entry_price = df["close"].iloc[-1]
                    atr = signal.technical.indicators.get("atr", 0)

                    # Volatility-targeting sizing (preferred) with ATR fallback.
                    # Vol-target keeps expected daily P&L roughly constant across
                    # symbols; ATR sizing is kept as a graceful fallback.
                    notional = 0.0
                    if settings.VOL_TARGET_ENABLED:
                        bars_per_day = {"1m": 1440, "5m": 288, "15m": 96,
                                        "1h": 24, "4h": 6, "1d": 1}.get(args.timeframe, 24)
                        notional = risk_mgr.vol_target_size(
                            balance["total"], df["close"], bars_per_day=bars_per_day,
                        )
                    if notional <= 0:
                        notional = risk_mgr.atr_based_size(balance["total"], entry_price, atr)

                    notional = risk_mgr.scale_by_confidence(notional, decision.get("confidence", 0.5))

                    # Apply regime size modifier (uses blended regime with AI bias)
                    regime_mod = risk_mgr.regime_size_modifier(blended, decision["action"])
                    notional *= regime_mod

                    # Funding-rate filter (perps only — spot exchanges return 0.0).
                    # Extreme adverse funding → shrink or skip the trade.
                    if settings.FUNDING_ENABLED:
                        try:
                            rate_1h = exchange.get_funding_rate(symbol)
                        except Exception as e:
                            logger.debug(f"funding lookup failed for {symbol}: {e}")
                            rate_1h = 0.0
                        fdec = evaluate_funding(
                            rate_1h,
                            decision["action"].upper(),
                            extreme_annual=settings.FUNDING_EXTREME_ANNUAL,
                            skip_annual=settings.FUNDING_SKIP_ANNUAL,
                        )
                        if fdec.action != "allow":
                            logger.info(f"  FUNDING {symbol}: {fdec.action} — {fdec.reason}")
                        notional *= fdec.size_modifier
                        if fdec.action == "skip":
                            logger.info(f"  SKIP {symbol}: funding filter (annual={fdec.annualized*100:.1f}%)")
                            continue

                    # Pre-trade risk checks
                    current_positions = exchange.get_positions()
                    allowed, reason = risk_mgr.pre_trade_check(
                        decision["action"], current_positions, balance, notional, symbol=symbol,
                    )

                    if not allowed:
                        logger.info(f"  RISK BLOCKED {symbol}: {reason}")
                    else:
                        amount = notional / entry_price
                        if amount > 0:
                            # Calculate SL and TP
                            sl = risk_mgr.calculate_stop_loss(entry_price, decision["action"], atr)
                            sl_distance = abs(entry_price - sl)
                            tp = risk_mgr.calculate_take_profit(entry_price, decision["action"], 2.0, sl_distance)
                            risk_pct = (notional / balance["total"]) * 100 if balance["total"] > 0 else 0

                            # Execute entry + SL/TP in one call
                            entry_order, _, _ = exchange.place_order_with_sl_tp(
                                symbol, decision["action"], amount, entry_price, sl, tp,
                            )

                            if entry_order.status in ("filled", "open"):
                                # Send detailed trade alert
                                telegram.send_trade_alert(
                                    side=decision["action"],
                                    symbol=symbol,
                                    price=entry_price,
                                    amount=amount,
                                    sl=sl,
                                    confidence=decision.get("confidence", 0),
                                    reasoning=decision.get("reasoning", ""),
                                    indicators=signal.technical.indicators,
                                    sentiment_summary=signal.sentiment.summary,
                                    sentiment=signal.sentiment.sentiment.value,
                                    sentiment_confidence=signal.sentiment.confidence,
                                    risk_pct=risk_pct,
                                    notional=notional,
                                    tech_signal=signal.technical.signal.value,
                                    tech_strength=signal.technical.strength,
                                )

                                journal.log_entry(
                                    symbol=symbol,
                                    side=decision["action"],
                                    price=entry_price,
                                    amount=amount,
                                    confidence=decision.get("confidence", 0),
                                    reasoning=decision.get("reasoning", ""),
                                    indicators=signal.technical.indicators,
                                    sentiment=signal.sentiment.sentiment.value,
                                )

                                # Register trailing-stop state so subsequent
                                # cycles can tighten the SL as profit grows.
                                from src.exchanges.base import Position as _Pos
                                trailing_mgr.register(
                                    _Pos(symbol=symbol, side=decision["action"],
                                         size=amount, entry_price=entry_price, unrealized_pnl=0.0),
                                    initial_sl=sl, tp=tp, atr=atr,
                                )

                # Live price display
                live_price = price_stream.get_price(symbol)
                if live_price:
                    print(f"  [{symbol}] ${live_price:.2f} | Signal: {signal.action.value} | AI: {decision['action']} | Regime: {regime.regime.value}", flush=True)

            if getattr(args, "once", False):
                logger.info("Single-cycle mode (--once) — exiting after one pass.")
                price_stream.stop_all()
                telegram.stop()
                return
            logger.info(f"Cycle complete. Sleeping {args.interval}s...")
            time.sleep(args.interval)

    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
        telegram.send_message("🛑 <b>Bot stopped by user</b>")
        price_stream.stop_all()
    finally:
        telegram.stop()


def _fetch_history_df(symbol: str, timeframe: str, limit: int):
    """Fetch OHLCV history from Hyperliquid first, then CCXT exchanges.
    Returns DataFrame or None."""
    df = None
    try:
        import requests as _req
        import time as _time
        import pandas as pd

        coin = symbol.replace("/USDC", "").replace("/USDT", "").replace("-USDC", "").replace("-USDT", "")
        # Rough ms per bar lookup — Hyperliquid wants startTime/endTime
        tf_ms = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}.get(timeframe, 3_600_000)
        now_ms = int(_time.time() * 1000)
        start_ms = now_ms - (limit * tf_ms)
        resp = _req.post('https://api.hyperliquid.xyz/info', json={
            'type': 'candleSnapshot',
            'req': {'coin': coin, 'interval': timeframe, 'startTime': start_ms, 'endTime': now_ms}
        }, timeout=15)
        candles = resp.json()
        if candles:
            df = pd.DataFrame([{
                'timestamp': c['t'], 'open': float(c['o']), 'high': float(c['h']),
                'low': float(c['l']), 'close': float(c['c']), 'volume': float(c['v']),
            } for c in candles])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            logger.info(f"Fetched {len(df)} {timeframe} candles for {coin} from Hyperliquid")
    except Exception:
        pass

    if df is None:
        try:
            import ccxt
            for exchange_name in ["gate", "kucoin", "binance", "bybit", "okx"]:
                try:
                    ex = getattr(ccxt, exchange_name)()
                    logger.info(f"Fetching {limit} {timeframe} candles for {symbol} via {exchange_name}...")
                    ohlcv = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
                    df = fetch_ohlcv_df_from_raw(ohlcv)
                    logger.info(f"Successfully fetched {len(df)} candles from {exchange_name}")
                    break
                except Exception:
                    continue
        except Exception:
            pass
    return df


def cmd_backtest(args):
    """Backtest the strategy on historical data."""
    from src.backtest.engine import run_backtest

    symbol = args.symbol
    df = _fetch_history_df(symbol, args.timeframe, args.limit)

    if df is None:
        if not args.allow_synthetic:
            logger.error(
                "Could not fetch live OHLCV data from Hyperliquid or any CCXT exchange. "
                "Backtest refuses to run on synthetic data by default. "
                "Re-run with --allow-synthetic if you understand the results will not reflect live performance, "
                "or retry later when the network is reachable."
            )
            return
        logger.warning(
            "!!! FALLING BACK TO SYNTHETIC DATA !!! "
            "Results are NOT representative of live performance. "
            "Use --allow-synthetic only for plumbing tests, never for strategy validation."
        )
        df = generate_synthetic_data(symbol, args.limit)
        setattr(args, "_synthetic", True)

    logger.info(f"Running backtest with {args.balance} USDT starting balance...")

    result = run_backtest(
        df=df,
        symbol=symbol,
        initial_balance=args.balance,
        risk_per_trade_pct=args.risk,
        leverage=args.leverage,
        use_sentiment=args.sentiment,
        fee_bps=args.fee_bps,
        slippage_bps=args.slippage_bps,
        min_signal_strength_live=args.min_strength,
    )

    # Print results
    print(f"\n{'='*55}")
    print(f"  BACKTEST RESULTS — {symbol} ({args.timeframe})")
    if getattr(args, "_synthetic", False):
        print(f"  [!] SYNTHETIC DATA — numbers below are NOT reliable")
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


def cmd_walkforward(args):
    """Walk-forward (out-of-sample) backtest."""
    from src.backtest.walk_forward import walk_forward

    symbol = args.symbol
    df = _fetch_history_df(symbol, args.timeframe, args.limit)
    synthetic = False
    if df is None:
        if not args.allow_synthetic:
            logger.error(
                "Walk-forward requires real OHLCV data and could not reach any exchange. "
                "Retry later, or pass --allow-synthetic for a plumbing smoke-test."
            )
            return
        logger.warning(
            "!!! Walk-forward running on SYNTHETIC DATA — do NOT interpret results as strategy quality."
        )
        df = generate_synthetic_data(symbol, args.limit)
        synthetic = True

    if len(df) < args.train_window + args.test_window:
        logger.error(
            f"Fetched only {len(df)} candles; need at least "
            f"{args.train_window + args.test_window} for train={args.train_window}, "
            f"test={args.test_window}. Increase --limit or shrink the windows."
        )
        return

    logger.info(
        f"Walk-forward: {len(df)} candles, train={args.train_window}, "
        f"test={args.test_window}, step={args.step or args.test_window}"
    )

    report = walk_forward(
        df=df,
        symbol=symbol,
        initial_balance=args.balance,
        train_window=args.train_window,
        test_window=args.test_window,
        step=args.step,
        leverage=args.leverage,
        risk_per_trade_pct=args.risk,
        use_sentiment=args.sentiment,
    )

    print(f"\n{'='*60}")
    print(f"  WALK-FORWARD REPORT — {symbol} ({args.timeframe})")
    if synthetic:
        print(f"  [!] SYNTHETIC DATA — numbers below are NOT reliable")
    print(f"{'='*60}")
    print(f"  Folds            : {len(report.folds)}")
    print(f"  Starting Balance : {report.initial_balance:.2f} USDT")
    print(f"  Final Balance    : {report.final_balance:.2f} USDT")
    sign = "+" if report.oos_total_pnl >= 0 else ""
    print(f"  OOS PnL          : {sign}{report.oos_total_pnl:.2f} USDT ({sign}{report.oos_total_pnl_pct:.2f}%)")
    print(f"  OOS Win Rate     : {report.oos_win_rate:.1f}%")
    print(f"  OOS Sharpe       : {report.oos_sharpe:.2f}")
    print(f"  OOS Max DD       : -{report.oos_max_drawdown_pct:.2f}%")
    print(f"  OOS Trades       : {len(report.combined_trades)}")
    print(f"{'='*60}")
    if report.folds:
        print(f"\n  {'#':<3} {'Test Window':<36} {'Trades':<7} {'WR%':<6} {'PnL%':<8} {'Best Params'}")
        print(f"  {'-'*100}")
        for f in report.folds:
            tr = f.test_result
            print(
                f"  {f.idx:<3} {f.test_start[:16]} -> {f.test_end[:16]}  "
                f"{tr.total_trades:<7} {tr.win_rate:<6.1f} {tr.total_pnl_pct:<+8.2f} {f.best_params}"
            )
    print()


def fetch_ohlcv_df_from_raw(raw: list):
    """Convert raw OHLCV list to DataFrame."""
    import pandas as pd
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    return df


def cmd_export(args):
    """Export the trade journal to a formatted .xlsx workbook."""
    from src.utils.trade_export import export_trades_to_xlsx

    out = args.output
    if not out:
        ts = time.strftime("%Y%m%d-%H%M%S")
        suffix = f"-last{args.days}d" if args.days else ""
        out = f"data/trade_export-{ts}{suffix}.xlsx"

    try:
        stats = export_trades_to_xlsx(out, days=args.days)
    except Exception as e:
        logger.error(f"Export failed: {e}")
        print(f"[ERROR] Export failed: {e}")
        return

    print("\n=== Trade Journal Export ===")
    print(f"  File     : {stats['path']}")
    print(f"  Rows     : {stats['rows']} (closed: {stats['closed']}, open: {stats['open']})")
    if args.days:
        print(f"  Window   : last {args.days} day(s)")
    print(f"  Sheets   : Trades | Summary | By Symbol | Daily P&L")
    print("\nOpen the file in Excel/LibreOffice. Formulas recalculate automatically")
    print("on first open — no manual recalc needed.\n")


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
    run_parser.add_argument("--once", action="store_true",
                            help="Run one cycle then exit (for Task Scheduler / cron wrappers).")
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
    bt_parser.add_argument(
        "--allow-synthetic",
        action="store_true",
        help="Allow fallback to synthetic market data if live fetch fails (NOT recommended — results are unreliable)",
    )
    bt_parser.add_argument(
        "--fee-bps",
        type=float,
        default=None,
        help="Override taker fee in basis points (default: settings.FEE_BPS)",
    )
    bt_parser.add_argument(
        "--slippage-bps",
        type=float,
        default=None,
        help="Override slippage in basis points (default: settings.SLIPPAGE_BPS)",
    )
    bt_parser.add_argument(
        "--min-strength",
        type=float,
        default=None,
        help="Minimum signal strength to enter a trade (matches live when omitted)",
    )
    bt_parser.set_defaults(func=cmd_backtest)

    # walk-forward command
    wf_parser = subparsers.add_parser(
        "walkforward",
        help="Walk-forward (out-of-sample) backtest — more honest than --backtest",
    )
    wf_parser.add_argument("--symbol", default="BTC/USDT")
    wf_parser.add_argument("--timeframe", default="1h")
    wf_parser.add_argument("--limit", type=int, default=2000, help="Total candles to fetch (needs >= train+test)")
    wf_parser.add_argument("--balance", type=float, default=50.0)
    wf_parser.add_argument("--leverage", type=float, default=20.0)
    wf_parser.add_argument("--risk", type=float, default=2.0)
    wf_parser.add_argument("--train-window", type=int, default=500)
    wf_parser.add_argument("--test-window", type=int, default=200)
    wf_parser.add_argument("--step", type=int, default=None, help="Defaults to test window (non-overlapping)")
    wf_parser.add_argument("--sentiment", action="store_true")
    wf_parser.add_argument("--allow-synthetic", action="store_true")
    wf_parser.set_defaults(func=cmd_walkforward)

    # export command — dump data/trades.json to a formatted xlsx workbook
    export_parser = subparsers.add_parser(
        "export",
        help="Export the trade journal to a formatted .xlsx workbook",
    )
    export_parser.add_argument(
        "--output", "-o",
        default=None,
        help="Output xlsx path (default: data/trade_export-<timestamp>.xlsx)",
    )
    export_parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Only include trades opened in the last N days (default: all)",
    )
    export_parser.set_defaults(func=cmd_export)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    args.func(args)


if __name__ == "__main__":
    main()
