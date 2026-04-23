import argparse
import sys
import time
from collections import defaultdict

from config import settings
from src.data.market import fetch_ohlcv_df
from src.data.websocket import PriceStream
from src.strategy.combined import generate_signal
from src.ai.agent import AIAgent
from src.execution.order import OrderExecutor
from src.execution.risk import RiskManager
from src.execution.trailing import TrailingStopManager
from src.execution.gate import evaluate_entry_gate, evaluate_event_gate
from src.exchanges.base import OrderSide
from src.exchanges.binance import BinanceExchange
from src.exchanges.hyperliquid import HyperliquidExchange
from src.utils.logger import logger
from src.utils.telegram import telegram
from src.utils.trade_journal import journal
from src.strategy.regime import fetch_macro_data, detect_ai_regime, AiRegimeResult, Bias
from src.strategy.funding import evaluate_funding
from src.strategy.alpha import AlphaEngine
from src.strategy.levels import compute_key_levels
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

    # Cycle counter drives the Telegram digest cadence
    # (TELEGRAM_DIGEST_INTERVAL_CYCLES). Starts at 0 so cycle #1 is the
    # first complete loop.
    cycle_counter = 0

    # === Phase 9: Economic calendar bootstrap ===
    # Load once at boot from cache OR refresh from faireconomy JSON /
    # Firecrawl fallback. Missing/stale cache is fine — loader handles
    # that by fetching. Non-fatal: any error here leaves calendar_snapshot
    # empty and the gate falls through to "allowed".
    calendar_snapshot = None
    warned_event_ids: set[str] = set()  # dedup cross-cycle event warnings
    if getattr(settings, "ECON_CALENDAR_ENABLED", False):
        try:
            from src.strategy.econ_calendar import load_or_refresh as _cal_load
            calendar_snapshot = _cal_load(
                firecrawl_key=getattr(settings, "FIRECRAWL_API_KEY", ""),
                refresh_hours=int(settings.ECON_CALENDAR_REFRESH_HOURS),
                prefer_firecrawl=bool(settings.ECON_PREFER_FIRECRAWL),
            )
            n = len(calendar_snapshot.events) if calendar_snapshot else 0
            src = calendar_snapshot.source if calendar_snapshot else "none"
            logger.info(f"Econ calendar loaded: {n} events (source={src})")
        except Exception as e:
            logger.warning(f"Econ calendar bootstrap failed: {e}")
            calendar_snapshot = None

    try:
        while True:
            cycle_counter += 1
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

                        # Compute trade duration from journal timestamp
                        # (ISO8601 with tz). Non-fatal if parsing fails.
                        duration_s = None
                        try:
                            from datetime import datetime, timezone
                            opened_at = t.get("opened_at") or t.get("timestamp")
                            if opened_at:
                                ts = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
                                if ts.tzinfo is None:
                                    ts = ts.replace(tzinfo=timezone.utc)
                                duration_s = (datetime.now(timezone.utc) - ts).total_seconds()
                        except Exception:
                            pass

                        # Infer close reason: if price crossed the stored SL
                        # we call it "stop_loss"; likewise for TP. Otherwise
                        # it was a manual/external close.
                        close_reason = "closed"
                        try:
                            sl_stored = t.get("sl") or 0
                            tp_stored = t.get("tp") or 0
                            if t["side"] == "buy":
                                if sl_stored and exit_price <= sl_stored:
                                    close_reason = "stop_loss"
                                elif tp_stored and exit_price >= tp_stored:
                                    close_reason = "take_profit"
                            else:
                                if sl_stored and exit_price >= sl_stored:
                                    close_reason = "stop_loss"
                                elif tp_stored and exit_price <= tp_stored:
                                    close_reason = "take_profit"
                        except Exception:
                            pass

                        telegram.send_position_close(
                            symbol=t["symbol"],
                            side=t["side"],
                            entry_price=float(entry),
                            exit_price=float(exit_price),
                            pnl=float(pnl),
                            amount=float(amount),
                            duration_s=duration_s,
                            reason=close_reason,
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

            # Per-cycle observability: count every rejection reason so we can
            # see WHY trades are not happening. Previously the 294-line gate
            # was silent on rejection → 8k+ "hold" / 0 "order" with no way to
            # tell which gate was eating signals. Dumped at cycle end.
            rejection_stats: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
            executed_count = 0

            # === Econ calendar per-cycle refresh + T-2h warning ===
            # load_or_refresh is cheap when cache is fresh (< refresh_hours
            # old): just reads the JSON file. When stale, it hits network
            # and either returns the new snapshot or — on failure — the
            # stale cache. Wrapped defensively: calendar is advisory, not
            # load-bearing, so any exception here must not stop trading.
            if getattr(settings, "ECON_CALENDAR_ENABLED", False):
                try:
                    from src.strategy.econ_calendar import (
                        load_or_refresh as _cal_load,
                        next_event as _cal_next,
                        format_event_for_log as _cal_fmt,
                    )
                    calendar_snapshot = _cal_load(
                        firecrawl_key=getattr(settings, "FIRECRAWL_API_KEY", ""),
                        refresh_hours=int(settings.ECON_CALENDAR_REFRESH_HOURS),
                        prefer_firecrawl=bool(settings.ECON_PREFER_FIRECRAWL),
                    )
                    if calendar_snapshot and calendar_snapshot.events:
                        from datetime import datetime as _dt, timezone as _tz
                        _now = _dt.now(_tz.utc)
                        warn_ahead = float(
                            getattr(settings, "ECON_EVENT_WARN_AHEAD_H", 2.0)
                        )
                        ev = _cal_next(
                            _now, calendar_snapshot.events,
                            currencies=settings.ECON_TRACK_CURRENCIES,
                            impacts=settings.ECON_TRACK_IMPACT,
                            within_hours=warn_ahead,
                        )
                        if ev is not None:
                            ev_id = f"{ev.currency}|{ev.title}|{ev.timestamp_utc.isoformat()}"
                            if ev_id not in warned_event_ids:
                                warned_event_ids.add(ev_id)
                                mins_ahead = (ev.timestamp_utc - _now).total_seconds() / 60.0
                                logger.info(f"Econ event ahead: {_cal_fmt(ev, _now)}")
                                try:
                                    telegram.send_event_warning(
                                        currency=ev.currency, title=ev.title,
                                        impact=ev.impact, minutes_ahead=mins_ahead,
                                        blackout_min=int(settings.ECON_BLACKOUT_MIN),
                                        size_mult=float(settings.ECON_EVENT_SIZE_MULT),
                                        window_h=float(settings.ECON_EVENT_WINDOW_H),
                                    )
                                except Exception as e:
                                    logger.debug(f"event warning push failed: {e}")
                except Exception as e:
                    logger.warning(f"Calendar refresh failed (non-fatal): {e}")

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

                # Fetch daily history for the key-levels engine — only once
                # per symbol per cycle, since pivots only shift on daily bar
                # close. Failure is non-fatal: the blend degrades gracefully.
                levels_result = None
                if getattr(settings, "LEVELS_ENABLED", True):
                    try:
                        daily_limit = int(getattr(settings, "LEVELS_DAILY_HISTORY", 200))
                        df_daily = fetch_ohlcv_df(exchange, symbol, timeframe="1d", limit=daily_limit)
                        if df_daily is not None and not df_daily.empty:
                            current_px = float(df["close"].iloc[-1])
                            levels_result = compute_key_levels(
                                df_daily, current_price=current_px, symbol=symbol,
                            )
                            logger.info(f"  Levels {symbol}: {levels_result.reasoning}")
                    except Exception as e:
                        logger.warning(f"Key-levels failed for {symbol}: {e}")

                # Generate combined signal (4H regime + 1H execution + AI macro + alpha + levels)
                signal = generate_signal(
                    df, symbol, df_regime=df_regime, ai_regime=ai_regime,
                    alpha=alpha_result, levels=levels_result,
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

                # === EXECUTION GATE ===
                # Combined signal (tech × sentiment × alpha × regime) is the
                # PRIMARY direction/confidence source. AI is advisory — it can
                # veto only when highly confident (≥ AI_VETO_MIN_CONFIDENCE,
                # default 0.80). Rationale: live-test showed 358 buy/sell
                # signals vs 0 orders over 24h because AI HOLD was silently
                # overriding strong tech blends. That was wrong by design.
                signal_action = signal.action.value          # "buy"/"sell"/"hold"
                signal_conf = float(signal.confidence)

                decision = ai_agent.decide(signal, symbol, balance, history_summary=history_summary)
                ai_action = decision["action"]
                ai_conf = float(decision.get("confidence", 0))
                logger.info(
                    f"[{symbol}] Signal={signal_action}(conf={signal_conf:.2f}) "
                    f"AI={ai_action}(conf={ai_conf:.2f})"
                )
                logger.info(f"  AI reasoning: {decision.get('reasoning', 'N/A')}")

                # Log regime state
                regime = signal.regime
                blended = signal.blended_regime
                logger.info(
                    f"  Regime: {regime.regime.value} (score={regime.score:.2f}) | "
                    f"Blended: {blended.regime.value} AI={blended.ai_regime}"
                )

                # Live price display — shown every cycle regardless of gate outcome
                live_price = price_stream.get_price(symbol)
                if live_price:
                    print(
                        f"  [{symbol}] ${live_price:.2f} | Signal: {signal_action} | "
                        f"AI: {ai_action} | Regime: {regime.regime.value}",
                        flush=True,
                    )

                ai_veto_threshold = float(getattr(settings, "AI_VETO_MIN_CONFIDENCE", 0.80))

                # --- Entry gate (pure decision function, see src/execution/gate.py) ---
                gate = evaluate_entry_gate(
                    signal_action=signal_action,
                    signal_conf=signal_conf,
                    ai_action=ai_action,
                    ai_conf=ai_conf,
                    min_confidence=min_confidence,
                    ai_veto_threshold=ai_veto_threshold,
                )
                if not gate.allowed:
                    rejection_stats[symbol][gate.reason] += 1
                    if gate.reason == "signal_hold":
                        logger.info(
                            f"  [{symbol}] REJECT=signal_hold — combined={signal_action} "
                            f"tech={signal.technical.signal.value}(str={signal.technical.strength:.2f}) "
                            f"sent={signal.sentiment.sentiment.value} "
                            f"regime={blended.regime.value}"
                        )
                    elif gate.reason == "low_confidence":
                        logger.info(
                            f"  [{symbol}] REJECT=low_confidence — "
                            f"signal_conf={signal_conf:.2f} < min={min_confidence}"
                        )
                    elif gate.reason == "ai_veto_hold":
                        logger.info(
                            f"  [{symbol}] REJECT=ai_veto_hold — AI HOLD at conf={ai_conf:.2f} "
                            f">= veto_threshold={ai_veto_threshold:.2f}"
                        )
                    elif gate.reason == "ai_veto_opposite":
                        logger.info(
                            f"  [{symbol}] REJECT=ai_veto_opposite — AI={ai_action} "
                            f"vs signal={signal_action} (AI conf={ai_conf:.2f} >= {ai_veto_threshold:.2f})"
                        )
                    else:
                        logger.info(f"  [{symbol}] REJECT={gate.reason}")
                    continue

                # All direction/confidence gates passed — signal wins.
                trade_action = signal_action
                trade_confidence = signal_conf

                # --- Gate 3c: macro-event blackout ---
                # High-impact USD macro events (FOMC, CPI, NFP, …) dry up
                # liquidity and blow spreads 5–10× in the minutes before
                # print. Reject new entries inside the blackout window;
                # existing positions keep their SL/TP and trailing
                # management so the bot can still *exit* around the event.
                if (
                    getattr(settings, "ECON_CALENDAR_ENABLED", False)
                    and calendar_snapshot is not None
                ):
                    try:
                        from datetime import datetime as _dt, timezone as _tz
                        ev_gate = evaluate_event_gate(
                            now=_dt.now(_tz.utc),
                            events=calendar_snapshot.events,
                            blackout_min=int(settings.ECON_BLACKOUT_MIN),
                            currencies=settings.ECON_TRACK_CURRENCIES,
                            impacts=settings.ECON_TRACK_IMPACT,
                        )
                        if not ev_gate.allowed:
                            rejection_stats[symbol]["event_blackout"] += 1
                            logger.info(
                                f"  [{symbol}] REJECT=event_blackout — {ev_gate.detail}"
                            )
                            continue
                    except Exception as e:
                        logger.debug(f"event gate failed (non-fatal): {e}")

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

                # Sizing uses signal confidence (primary). Blend AI conf if AI
                # agrees with direction — gives slight boost when tech+AI align.
                sizing_conf = trade_confidence
                if ai_action == trade_action and ai_conf > 0:
                    sizing_conf = 0.6 * trade_confidence + 0.4 * ai_conf
                notional = risk_mgr.scale_by_confidence(notional, sizing_conf)

                # Apply regime size modifier (uses blended regime with AI bias)
                regime_mod = risk_mgr.regime_size_modifier(blended, trade_action)
                notional *= regime_mod

                # Chop trades are mean-reverting fades — shrink the size
                # because win rate is lower and we want risk parity with
                # the higher-RR trend trades.
                if signal.strategy_mode == "chop":
                    chop_mult = float(getattr(settings, "CHOP_SIZE_MULT", 0.5))
                    notional *= chop_mult
                    logger.info(f"  [{symbol}] chop sizing x{chop_mult}")

                # Macro-event size modifier — shrink notional ±ECON_EVENT_WINDOW_H
                # hours around any matching high-impact event. Applies OUTSIDE
                # the blackout window (blackout already rejected entries inside
                # T-ECON_BLACKOUT_MIN). Rationale: even 90 min after an event
                # the tape can be whippy, so keep size small but don't refuse
                # otherwise-valid setups.
                if (
                    getattr(settings, "ECON_CALENDAR_ENABLED", False)
                    and calendar_snapshot is not None
                ):
                    try:
                        from datetime import datetime as _dt, timezone as _tz
                        from src.strategy.econ_calendar import get_size_modifier as _cal_size
                        ev_mult, ev_match = _cal_size(
                            now=_dt.now(_tz.utc),
                            events=calendar_snapshot.events,
                            window_h=float(settings.ECON_EVENT_WINDOW_H),
                            size_mult=float(settings.ECON_EVENT_SIZE_MULT),
                            currencies=settings.ECON_TRACK_CURRENCIES,
                            impacts=settings.ECON_TRACK_IMPACT,
                        )
                        if ev_mult != 1.0:
                            notional *= ev_mult
                            logger.info(
                                f"  [{symbol}] event sizing x{ev_mult:.2f} "
                                f"({ev_match.currency} {ev_match.title})"
                            )
                    except Exception as e:
                        logger.debug(f"event size modifier failed (non-fatal): {e}")

                # Funding-rate filter (perps only — spot exchanges return 0.0).
                if settings.FUNDING_ENABLED:
                    try:
                        rate_1h = exchange.get_funding_rate(symbol)
                    except Exception as e:
                        logger.debug(f"funding lookup failed for {symbol}: {e}")
                        rate_1h = 0.0
                    fdec = evaluate_funding(
                        rate_1h,
                        trade_action.upper(),
                        extreme_annual=settings.FUNDING_EXTREME_ANNUAL,
                        skip_annual=settings.FUNDING_SKIP_ANNUAL,
                    )
                    if fdec.action != "allow":
                        logger.info(f"  FUNDING {symbol}: {fdec.action} — {fdec.reason}")
                    notional *= fdec.size_modifier
                    if fdec.action == "skip":
                        rejection_stats[symbol]["funding_skip"] += 1
                        logger.info(
                            f"  [{symbol}] REJECT=funding_skip — "
                            f"annual={fdec.annualized*100:.1f}% ({fdec.reason})"
                        )
                        continue

                # --- Gate 4: pre-trade risk checks (exposure, cluster, etc.) ---
                current_positions = exchange.get_positions()
                allowed, reason = risk_mgr.pre_trade_check(
                    trade_action, current_positions, balance, notional, symbol=symbol,
                )
                if not allowed:
                    rejection_stats[symbol]["risk_blocked"] += 1
                    logger.info(f"  [{symbol}] REJECT=risk_blocked — {reason}")
                    continue

                # --- Gate 5: sizing sanity (after all modifiers) ---
                amount = notional / entry_price if entry_price > 0 else 0.0
                if amount <= 0:
                    rejection_stats[symbol]["sizing_zero"] += 1
                    logger.info(
                        f"  [{symbol}] REJECT=sizing_zero — notional={notional:.2f} "
                        f"entry={entry_price:.2f} (all modifiers collapsed size)"
                    )
                    continue

                # Calculate SL and TP — chop trades prefer the engine's
                # channel-edge hints (tighter stop, midline target); trend
                # trades use the ATR-based defaults.
                if signal.strategy_mode == "chop" and signal.chop is not None \
                        and signal.chop.sl_hint is not None \
                        and signal.chop.tp_hint is not None:
                    sl = float(signal.chop.sl_hint)
                    tp = float(signal.chop.tp_hint)
                    sl_distance = abs(entry_price - sl)
                else:
                    sl = risk_mgr.calculate_stop_loss(entry_price, trade_action, atr)
                    sl_distance = abs(entry_price - sl)
                    tp = risk_mgr.calculate_take_profit(entry_price, trade_action, 2.0, sl_distance)
                risk_pct = (notional / balance["total"]) * 100 if balance["total"] > 0 else 0

                # Execute entry + SL/TP in one call.
                # Pass typed OrderSide — the exchange layer also defensively
                # normalizes strings, but pinning the enum at the boundary is
                # the correct contract. See test_hyperliquid_side_norm.py.
                side_enum = OrderSide.BUY if trade_action == "buy" else OrderSide.SELL

                # --dry-run short-circuit: log the would-be order and skip.
                # This is the smoke-test escape hatch documented in LIVE_TEST.md.
                # NEVER drop this branch without also removing the flag.
                if getattr(args, "dry_run", False):
                    rejection_stats[symbol]["dry_run_skipped"] += 1
                    logger.info(
                        f"  [{symbol}] DRY-RUN — would {trade_action.upper()} "
                        f"amount={amount:.6f} @ {entry_price:.2f} "
                        f"SL={sl:.2f} TP={tp:.2f} notional={notional:.2f} "
                        f"risk={risk_pct:.2f}%"
                    )
                    continue

                try:
                    entry_order, _, _ = exchange.place_order_with_sl_tp(
                        symbol, side_enum, amount, entry_price, sl, tp,
                    )
                except Exception as e:
                    rejection_stats[symbol]["order_exception"] += 1
                    logger.error(f"  [{symbol}] REJECT=order_exception — {e}")
                    telegram.send_error_alert(f"Order failed {symbol}: {e}")
                    continue

                if entry_order.status not in ("filled", "open"):
                    rejection_stats[symbol]["order_failed"] += 1
                    logger.warning(
                        f"  [{symbol}] REJECT=order_failed — "
                        f"status={entry_order.status} id={entry_order.id}"
                    )
                    continue

                executed_count += 1

                # Build extra reasoning tags: strategy mode (trend/chop) +
                # condensed levels summary so Telegram recipients know
                # whether a trade was a trend follow-through or a chop fade,
                # and which pivot gave the bias.
                extra_reasoning_parts = [
                    f"[{signal.strategy_mode}]",
                    decision.get("reasoning", ""),
                ]
                if signal.levels is not None:
                    extra_reasoning_parts.append(f"Levels: {signal.levels.reasoning}")
                trade_reasoning = " | ".join(p for p in extra_reasoning_parts if p)

                # Send detailed trade alert
                telegram.send_trade_alert(
                    side=trade_action,
                    symbol=symbol,
                    price=entry_price,
                    amount=amount,
                    sl=sl,
                    confidence=trade_confidence,
                    reasoning=trade_reasoning,
                    indicators=signal.technical.indicators,
                    sentiment_summary=signal.sentiment.summary,
                    sentiment=signal.sentiment.sentiment.value,
                    sentiment_confidence=signal.sentiment.confidence,
                    risk_pct=risk_pct,
                    notional=notional,
                    tech_signal=signal.technical.signal.value,
                    tech_strength=signal.technical.strength,
                    strategy_mode=signal.strategy_mode,
                )

                journal.log_entry(
                    symbol=symbol,
                    side=trade_action,
                    price=entry_price,
                    amount=amount,
                    confidence=trade_confidence,
                    reasoning=decision.get("reasoning", ""),
                    indicators=signal.technical.indicators,
                    sentiment=signal.sentiment.sentiment.value,
                )

                # Register trailing-stop state so subsequent cycles
                # can tighten the SL as profit grows.
                from src.exchanges.base import Position as _Pos
                trailing_mgr.register(
                    _Pos(symbol=symbol, side=trade_action,
                         size=amount, entry_price=entry_price, unrealized_pnl=0.0),
                    initial_sl=sl, tp=tp, atr=atr,
                )

                logger.info(f"  [{symbol}] EXECUTED {trade_action.upper()} size={amount:.6f} @ {entry_price:.4f}")

            # === Per-cycle summary ===
            # Dumps aggregate rejection stats so ops can see at a glance
            # WHY trades aren't happening across the pair universe.
            total_rejected = sum(sum(r.values()) for r in rejection_stats.values())
            logger.info(
                f"=== Cycle summary: executed={executed_count} rejected={total_rejected} "
                f"pairs={len(settings.TRADING_PAIRS)} ==="
            )
            reason_totals: dict[str, int] = defaultdict(int)
            if total_rejected > 0:
                # Flatten to {reason: total_count} for the headline
                for sym_stats in rejection_stats.values():
                    for reason, cnt in sym_stats.items():
                        reason_totals[reason] += cnt
                summary = ", ".join(f"{k}={v}" for k, v in sorted(reason_totals.items()))
                logger.info(f"  Rejection breakdown: {summary}")
                # Per-symbol detail (quiet for symbols with no rejections)
                for sym in sorted(rejection_stats.keys()):
                    per_sym = rejection_stats[sym]
                    if not per_sym:
                        continue
                    detail = ", ".join(f"{k}={v}" for k, v in sorted(per_sym.items()))
                    logger.info(f"    [{sym}] {detail}")

            # === Periodic Telegram digest ===
            # Every TELEGRAM_DIGEST_INTERVAL_CYCLES cycles, push a compact
            # rollup so Telegram-only operators can see at a glance what
            # the loop has been doing even if no trades fired. Pulls
            # Tavily budget state too since that silently gates sentiment.
            try:
                digest_every = int(getattr(settings, "TELEGRAM_DIGEST_INTERVAL_CYCLES", 12))
            except Exception:
                digest_every = 12
            if digest_every > 0 and (cycle_counter % digest_every == 0):
                try:
                    budget_snapshot = None
                    try:
                        from src.strategy.sentiment_cache import get_budget_snapshot
                        budget_snapshot = get_budget_snapshot()
                    except Exception as e:
                        logger.debug(f"budget snapshot failed: {e}")
                    telegram.send_cycle_digest(
                        cycle_num=cycle_counter,
                        executed=executed_count,
                        rejected=total_rejected,
                        rejection_breakdown=dict(reason_totals),
                        pairs=[p.strip() for p in settings.TRADING_PAIRS],
                        budget_snapshot=budget_snapshot,
                    )
                except Exception as e:
                    logger.warning(f"Telegram cycle digest failed: {e}")

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
        print(f"  Per-fold OOS PnL:")
        for fold in report.folds:
            tr = fold.test_result
            pnl_sign = "+" if tr.total_pnl >= 0 else ""
            print(
                f"    Fold {fold.idx:>2}: "
                f"{fold.test_start[:10]} → {fold.test_end[:10]}  "
                f"{pnl_sign}{tr.total_pnl:>7.2f} USDT  "
                f"({pnl_sign}{tr.total_pnl_pct:>5.2f}%)  "
                f"trades={len(tr.trades):>3}  "
                f"params={fold.best_params}"
            )
        print(f"{'='*60}")
    print()


def main():
    """CLI entry point — dispatches to cmd_* handlers via argparse."""
    parser = argparse.ArgumentParser(description="Agentic Trade — AI crypto trading bot")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # run command
    run_parser = subparsers.add_parser("run", help="Start the trading bot (live loop)")
    run_parser.add_argument(
        "--exchange",
        default=settings.DEFAULT_EXCHANGE,
        choices=["binance", "hyperliquid"],
        help="Exchange to trade on (default from settings.DEFAULT_EXCHANGE)",
    )
    run_parser.add_argument(
        "--timeframe", default="1h",
        help="Candle timeframe for analysis (1m, 5m, 15m, 1h, 4h, 1d)",
    )
    run_parser.add_argument(
        "--interval", type=int, default=300,
        help="Seconds between analysis cycles (default 300 = 5 min)",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip order placement — log decisions only (safe smoke test)",
    )
    run_parser.set_defaults(func=cmd_run)

    # status command
    status_parser = subparsers.add_parser("status", help="Show positions and balance")
    status_parser.add_argument(
        "--exchange",
        default=settings.DEFAULT_EXCHANGE,
        choices=["binance", "hyperliquid"],
    )
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
    wf_parser.add_argument(
        "--limit", type=int, default=2000,
        help="Total candles to fetch (needs >= train+test)",
    )
    wf_parser.add_argument("--balance", type=float, default=50.0)
    wf_parser.add_argument("--leverage", type=float, default=20.0)
    wf_parser.add_argument("--risk", type=float, default=2.0)
    wf_parser.add_argument("--train-window", type=int, default=500)
    wf_parser.add_argument("--test-window", type=int, default=200)
    wf_parser.add_argument(
        "--step", type=int, default=None,
        help="Defaults to test window (non-overlapping)",
    )
    wf_parser.add_argument("--sentiment", action="store_true")
    wf_parser.add_argument("--allow-synthetic", action="store_true")
    wf_parser.set_defaults(func=cmd_walkforward)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    args.func(args)


if __name__ == "__main__":
    main()
