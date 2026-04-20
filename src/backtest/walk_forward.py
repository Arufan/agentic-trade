"""Walk-forward backtest harness.

Why: a single in-sample backtest is trivial to overfit. Walk-forward
splits the history into rolling (train, test) windows. The strategy is
"trained" (parameters selected from a small grid) on each train window
and evaluated on the immediately following test window. Concatenating
the out-of-sample test PnLs gives a much more honest picture of live
expectancy.

This module is intentionally small — it wraps `run_backtest` without
duplicating the per-bar simulation logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import numpy as np
import pandas as pd

from src.backtest.engine import BacktestResult, Trade, run_backtest
from src.utils.logger import logger


@dataclass
class Fold:
    idx: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    best_params: dict
    train_result: BacktestResult
    test_result: BacktestResult


@dataclass
class WalkForwardReport:
    folds: list[Fold] = field(default_factory=list)
    combined_trades: list[Trade] = field(default_factory=list)
    oos_total_pnl: float = 0.0
    oos_total_pnl_pct: float = 0.0
    oos_win_rate: float = 0.0
    oos_sharpe: float = 0.0
    oos_max_drawdown_pct: float = 0.0
    initial_balance: float = 0.0
    final_balance: float = 0.0


def _default_grid() -> list[dict]:
    """A small, defensible parameter grid. Keep it tight — a wide grid
    over a short history is just another form of overfit."""
    grid: list[dict] = []
    for rr in (1.2, 1.5, 2.0):
        for sl in (1.0, 1.5, 2.0):
            for strength in (0.25, 0.35):
                grid.append({
                    "take_profit_rr": rr,
                    "stop_loss_atr_mult": sl,
                    "min_signal_strength": strength,
                })
    return grid


def _score(result: BacktestResult) -> float:
    """Rank params by something closer to expectancy than raw PnL.
    We combine PnL% with trade count (prefer >= 10 trades) and a
    drawdown penalty, so a one-lucky-trade run doesn't win."""
    if result.total_trades < 5:
        return -1e9
    pnl = result.total_pnl_pct
    dd = result.max_drawdown_pct
    trade_bonus = min(result.total_trades, 30) * 0.1
    return pnl - dd * 0.5 + trade_bonus


def walk_forward(
    df: pd.DataFrame,
    symbol: str,
    *,
    initial_balance: float = 50.0,
    train_window: int = 500,
    test_window: int = 200,
    step: int | None = None,
    param_grid: Iterable[dict] | None = None,
    leverage: float = 20.0,
    risk_per_trade_pct: float = 2.0,
    use_sentiment: bool = False,
) -> WalkForwardReport:
    """Run a walk-forward evaluation over `df`.

    Each fold:
      1) For every params in `param_grid`, run_backtest on the train slice.
      2) Pick the params with the best _score on train.
      3) Apply those params to the test slice, starting from the balance
         at the end of the previous test slice (compounding OOS).
    """
    if step is None:
        step = test_window
    if param_grid is None:
        param_grid = _default_grid()
    param_grid = list(param_grid)

    n = len(df)
    if n < train_window + test_window:
        raise ValueError(
            f"Not enough data for walk-forward: need >= {train_window + test_window}, got {n}"
        )

    report = WalkForwardReport(initial_balance=initial_balance)
    balance = initial_balance
    peak = initial_balance
    max_dd = 0.0
    fold_idx = 0
    start = 0

    while start + train_window + test_window <= n:
        train_slice = df.iloc[start:start + train_window]
        test_slice = df.iloc[start + train_window:start + train_window + test_window]

        # 1) grid search on train
        best_params = None
        best_score = -float("inf")
        best_train_result: BacktestResult | None = None
        for params in param_grid:
            try:
                res = run_backtest(
                    df=train_slice,
                    symbol=symbol,
                    initial_balance=initial_balance,  # detached train
                    leverage=leverage,
                    risk_per_trade_pct=risk_per_trade_pct,
                    use_sentiment=False,  # sentiment on train is pointless (stale)
                    **params,
                )
            except Exception as e:
                logger.warning(f"Fold {fold_idx} train params {params} failed: {e}")
                continue
            s = _score(res)
            if s > best_score:
                best_score = s
                best_params = params
                best_train_result = res

        if best_params is None or best_train_result is None:
            logger.warning(f"Fold {fold_idx}: no viable params on train slice, skipping")
            start += step
            fold_idx += 1
            continue

        # 2) evaluate on OOS test slice, compounding balance
        try:
            test_result = run_backtest(
                df=test_slice,
                symbol=symbol,
                initial_balance=balance,
                leverage=leverage,
                risk_per_trade_pct=risk_per_trade_pct,
                use_sentiment=use_sentiment,
                **best_params,
            )
        except Exception as e:
            logger.warning(f"Fold {fold_idx} test run failed: {e}")
            start += step
            fold_idx += 1
            continue

        balance = test_result.final_balance
        if balance > peak:
            peak = balance
        dd_pct = ((peak - balance) / peak) * 100 if peak > 0 else 0
        if dd_pct > max_dd:
            max_dd = dd_pct

        fold = Fold(
            idx=fold_idx,
            train_start=str(train_slice.index[0]),
            train_end=str(train_slice.index[-1]),
            test_start=str(test_slice.index[0]),
            test_end=str(test_slice.index[-1]),
            best_params=best_params,
            train_result=best_train_result,
            test_result=test_result,
        )
        report.folds.append(fold)
        report.combined_trades.extend(test_result.trades)
        logger.info(
            f"Fold {fold_idx}: train {fold.train_start[:10]}..{fold.train_end[:10]} "
            f"-> test {fold.test_start[:10]}..{fold.test_end[:10]} | "
            f"params={best_params} | OOS PnL%={test_result.total_pnl_pct:+.2f} "
            f"trades={test_result.total_trades} wr={test_result.win_rate:.1f}%"
        )

        start += step
        fold_idx += 1

    # OOS aggregate stats
    wins = sum(1 for t in report.combined_trades if t.pnl > 0)
    total = len(report.combined_trades)
    report.oos_win_rate = (wins / total * 100) if total else 0.0
    oos_returns = [t.pnl_pct / 100 for t in report.combined_trades]
    if len(oos_returns) > 1:
        mean = float(np.mean(oos_returns))
        std = float(np.std(oos_returns))
        report.oos_sharpe = (mean / std) * float(np.sqrt(8760)) if std > 0 else 0.0
    report.oos_total_pnl = balance - initial_balance
    report.oos_total_pnl_pct = ((balance - initial_balance) / initial_balance * 100) if initial_balance else 0.0
    report.oos_max_drawdown_pct = round(max_dd, 2)
    report.final_balance = round(balance, 2)

    return report
