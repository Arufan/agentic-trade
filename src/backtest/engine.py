from dataclasses import dataclass, field

import numpy as np

from src.strategy.technical import Signal, analyze_technical
from src.utils.logger import logger


@dataclass
class Trade:
    symbol: str
    side: str  # "buy" or "sell"
    entry_price: float
    exit_price: float
    size: float
    pnl: float
    pnl_pct: float
    entry_time: str
    exit_time: str


@dataclass
class BacktestResult:
    initial_balance: float
    final_balance: float
    total_pnl: float
    total_pnl_pct: float
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    max_drawdown_pct: float
    sharpe_ratio: float
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list = field(default_factory=list)  # [(timestamp, balance), ...]


def run_backtest(
    df,
    symbol: str,
    initial_balance: float = 50.0,
    risk_per_trade_pct: float = 2.0,
    stop_loss_atr_mult: float = 1.5,
    take_profit_rr: float = 1.5,
    min_signal_strength: float = 0.2,
    leverage: float = 20.0,
    use_sentiment: bool = False,
) -> BacktestResult:
    """Run a backtest on historical OHLCV data using technical + optional sentiment.

    Args:
        df: DataFrame with OHLCV data (must have 'close', 'high', 'low' columns)
        symbol: Trading pair symbol
        initial_balance: Starting balance in USDT
        risk_per_trade_pct: % of balance risked per trade
        stop_loss_atr_mult: ATR multiplier for stop-loss distance
        take_profit_rr: Risk:reward ratio for take-profit
        min_signal_strength: Minimum signal strength to trigger a trade
        leverage: Trading leverage
        use_sentiment: Include Tavily sentiment analysis
    """
    from ta.volatility import AverageTrueRange

    # Sentiment: fetch once for the symbol
    sentiment_data = None
    if use_sentiment:
        try:
            from src.strategy.sentiment import analyze_sentiment
            coin = symbol.replace("/USDC", "").replace("/USDT", "").replace("-", "")
            logger.info(f"Fetching sentiment for {coin}...")
            sentiment_data = analyze_sentiment(coin)
            logger.info(f"Sentiment: {sentiment_data.sentiment.value} (conf: {sentiment_data.confidence:.2f})")
        except Exception as e:
            logger.warning(f"Sentiment unavailable, technical only: {e}")

    # Calculate ATR for stop-loss
    atr = AverageTrueRange(high=df["high"], low=df["low"], close=df["close"], window=14)
    atr_values = atr.average_true_range()

    balance = initial_balance
    peak_balance = initial_balance
    max_drawdown = 0.0
    position = None
    trades: list[Trade] = []
    returns = []
    equity_curve: list = []
    lookback = 60

    for i in range(lookback, len(df)):
        candle = df.iloc[i]
        price = candle["close"]
        high = candle["high"]
        low = candle["low"]
        timestamp = str(df.index[i])
        current_atr = atr_values.iloc[i]

        # Check existing position for SL/TP hit + trailing stop
        if position:
            # Trailing stop: move SL to breakeven once in profit by 0.5 ATR, then trail by 1 ATR
            if position["side"] == "buy":
                profit_atr = (high - position["entry_price"]) / current_atr if current_atr > 0 else 0
                if profit_atr >= 0.5:
                    new_sl = position["entry_price"] + current_atr * 0.3
                    if new_sl > position["stop_loss"]:
                        position["stop_loss"] = new_sl
                if profit_atr >= 1.5:
                    new_sl = max(position["stop_loss"], high - current_atr * 1.0)
                    position["stop_loss"] = new_sl
            else:
                profit_atr = (position["entry_price"] - low) / current_atr if current_atr > 0 else 0
                if profit_atr >= 0.5:
                    new_sl = position["entry_price"] - current_atr * 0.3
                    if new_sl < position["stop_loss"]:
                        position["stop_loss"] = new_sl
                if profit_atr >= 1.5:
                    new_sl = min(position["stop_loss"], low + current_atr * 1.0)
                    position["stop_loss"] = new_sl

            sl_hit = (
                (position["side"] == "buy" and low <= position["stop_loss"]) or
                (position["side"] == "sell" and high >= position["stop_loss"])
            )
            tp_hit = (
                (position["side"] == "buy" and high >= position["take_profit"]) or
                (position["side"] == "sell" and low <= position["take_profit"])
            )

            if sl_hit:
                exit_price = position["stop_loss"]
                raw_pnl = (exit_price - position["entry_price"]) * position["size"] * (1 if position["side"] == "buy" else -1)
                fee = position["size"] * exit_price * 0.0004 + position["size"] * position["entry_price"] * 0.0004  # 0.04% each side
                pnl = raw_pnl - fee
                balance = position["balance_at_entry"] + pnl
                trades.append(Trade(
                    symbol=symbol, side=position["side"],
                    entry_price=position["entry_price"], exit_price=exit_price,
                    size=position["size"], pnl=pnl,
                    pnl_pct=(pnl / position["margin"]) * 100,
                    entry_time=position["entry_time"], exit_time=timestamp,
                ))
                returns.append(pnl / position["margin"])
                position = None

            elif tp_hit:
                exit_price = position["take_profit"]
                raw_pnl = (exit_price - position["entry_price"]) * position["size"] * (1 if position["side"] == "buy" else -1)
                fee = position["size"] * exit_price * 0.0004 + position["size"] * position["entry_price"] * 0.0004
                pnl = raw_pnl - fee
                balance = position["balance_at_entry"] + pnl
                trades.append(Trade(
                    symbol=symbol, side=position["side"],
                    entry_price=position["entry_price"], exit_price=exit_price,
                    size=position["size"], pnl=pnl,
                    pnl_pct=(pnl / position["margin"]) * 100,
                    entry_time=position["entry_time"], exit_time=timestamp,
                ))
                returns.append(pnl / position["margin"])
                position = None

            # Trailing: update peak & drawdown
            if balance > peak_balance:
                peak_balance = balance
            dd = ((peak_balance - balance) / peak_balance) * 100
            if dd > max_drawdown:
                max_drawdown = dd

            continue  # skip signal generation while in position

        # No position — analyze signal
        window = df.iloc[i - lookback:i + 1]
        signal = analyze_technical(window)

        # Apply sentiment filter: if sentiment opposes technical, skip
        if sentiment_data and signal.signal != Signal.HOLD:
            from src.strategy.sentiment import Sentiment
            if signal.signal == Signal.BUY and sentiment_data.sentiment == Sentiment.BEARISH and sentiment_data.confidence > 0.5:
                continue  # Skip buy when strong bearish sentiment
            if signal.signal == Signal.SELL and sentiment_data.sentiment == Sentiment.BULLISH and sentiment_data.confidence > 0.5:
                continue  # Skip sell when strong bullish sentiment

        if signal.signal == Signal.HOLD or signal.strength < min_signal_strength:
            continue

        # Track equity curve
        equity_curve.append((str(df.index[i]), round(balance, 2)))

        if not np.isfinite(current_atr) or current_atr == 0:
            continue

        # Calculate position size (with leverage)
        risk_amount = balance * (risk_per_trade_pct / 100)
        sl_distance = current_atr * stop_loss_atr_mult
        size = risk_amount / sl_distance if sl_distance > 0 else 0
        cost = size * price
        margin_required = cost / leverage

        if size <= 0 or margin_required > balance:
            continue

        # Set SL/TP
        if signal.signal == Signal.BUY:
            stop_loss = price - sl_distance
            take_profit = price + (sl_distance * take_profit_rr)
            side = "buy"
        else:
            stop_loss = price + sl_distance
            take_profit = price - (sl_distance * take_profit_rr)
            side = "sell"

        margin = cost / leverage
        position = {
            "side": side,
            "entry_price": price,
            "size": size,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "entry_time": timestamp,
            "balance_at_entry": balance,
            "cost": cost,
            "margin": margin,
            "leverage": leverage,
        }
        logger.info(f"  [{timestamp}] OPEN {side.upper()} {symbol} @ {price:.2f} | size={size:.6f} | margin={margin:.2f} | SL={stop_loss:.2f} TP={take_profit:.2f}")

    # Close any open position at end
    if position:
        exit_price = df.iloc[-1]["close"]
        raw_pnl = (exit_price - position["entry_price"]) * position["size"] * (1 if position["side"] == "buy" else -1)
        fee = position["size"] * exit_price * 0.0004 + position["size"] * position["entry_price"] * 0.0004
        pnl = raw_pnl - fee
        balance = position["balance_at_entry"] + pnl
        trades.append(Trade(
            symbol=symbol, side=position["side"],
            entry_price=position["entry_price"], exit_price=exit_price,
            size=position["size"], pnl=pnl,
            pnl_pct=(pnl / position["margin"]) * 100,
            entry_time=position["entry_time"], exit_time=str(df.index[-1]),
        ))
        returns.append(pnl / position["margin"])

    # Stats
    wins = sum(1 for t in trades if t.pnl > 0)
    losses = sum(1 for t in trades if t.pnl <= 0)
    total_trades = len(trades)
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

    # Sharpe ratio (simplified, annualized for hourly data)
    if len(returns) > 1:
        mean_ret = np.mean(returns)
        std_ret = np.std(returns)
        sharpe = (mean_ret / std_ret) * np.sqrt(8760) if std_ret > 0 else 0  # 8760 hours/year
    else:
        sharpe = 0

    total_pnl = balance - initial_balance
    total_pnl_pct = (total_pnl / initial_balance) * 100

    return BacktestResult(
        initial_balance=initial_balance,
        final_balance=round(balance, 2),
        total_pnl=round(total_pnl, 2),
        total_pnl_pct=round(total_pnl_pct, 2),
        total_trades=total_trades,
        wins=wins,
        losses=losses,
        win_rate=round(win_rate, 1),
        max_drawdown_pct=round(max_drawdown, 2),
        sharpe_ratio=round(sharpe, 2),
        trades=trades,
        equity_curve=equity_curve,
    )
