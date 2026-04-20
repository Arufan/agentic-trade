"""Trailing-stop manager for live execution.

Mirrors the trailing logic inside src/backtest/engine.py so that live and
backtest behave the same way:

  - Once a position has moved +0.5 ATR in our favour, move the SL to
    breakeven + 0.3 ATR (lock in a small profit).
  - Once it has moved +1.5 ATR in our favour, trail the SL 1.0 ATR
    behind the most favourable price seen since entry.

State is persisted per symbol to data/state.json so restarts don't
reset the trailing.
"""

from __future__ import annotations

import json
import os
from typing import Iterable

from src.exchanges.base import BaseExchange, Position
from src.utils.logger import logger

STATE_PATH = os.path.join(os.getcwd(), "data", "state.json")


def _load() -> dict:
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save(state: dict):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)
    except OSError as e:
        logger.warning(f"Failed to persist trailing state: {e}")


class TrailingStopManager:
    def __init__(self, exchange: BaseExchange):
        self.exchange = exchange

    # ---- state helpers ----
    @staticmethod
    def _key(position: Position) -> str:
        return f"{position.symbol}:{position.side}"

    def register(self, position: Position, initial_sl: float, tp: float, atr: float):
        """Call right after placing a new SL/TP pair."""
        state = _load()
        trailing = state.setdefault("trailing", {})
        trailing[self._key(position)] = {
            "entry": position.entry_price,
            "side": position.side,
            "initial_sl": initial_sl,
            "current_sl": initial_sl,
            "tp": tp,
            "atr": atr,
            "extreme": position.entry_price,  # max for longs, min for shorts
        }
        _save(state)

    def forget(self, symbol: str, side: str):
        state = _load()
        trailing = state.get("trailing", {})
        trailing.pop(f"{symbol}:{side}", None)
        state["trailing"] = trailing
        _save(state)

    # ---- main update ----
    def update(self, positions: Iterable[Position], current_atr_by_symbol: dict[str, float]) -> list[dict]:
        """Tighten SL for any position that has moved in profit.

        Returns a list of {symbol, side, old_sl, new_sl} for positions whose
        stop was moved. Callers must replace the trigger order on-exchange.
        """
        state = _load()
        trailing = state.setdefault("trailing", {})
        moved: list[dict] = []

        for pos in positions:
            key = self._key(pos)
            entry_state = trailing.get(key)
            if entry_state is None:
                # Position exists on-exchange but we have no record — skip.
                continue

            atr = current_atr_by_symbol.get(pos.symbol) or entry_state.get("atr") or 0
            if atr <= 0:
                continue

            try:
                ticker = self.exchange.get_ticker(pos.symbol)
                last = float(ticker.get("last") or 0)
            except Exception as e:
                logger.warning(f"Trailing: ticker fetch failed {pos.symbol}: {e}")
                continue
            if last <= 0:
                continue

            side = entry_state["side"]
            entry = entry_state["entry"]
            current_sl = entry_state["current_sl"]

            # Track running extreme
            if side == "buy":
                extreme = max(entry_state["extreme"], last)
                profit_atr = (extreme - entry) / atr
            else:
                extreme = min(entry_state["extreme"], last)
                profit_atr = (entry - extreme) / atr
            entry_state["extreme"] = extreme

            new_sl = current_sl
            if profit_atr >= 1.5:
                if side == "buy":
                    candidate = extreme - atr * 1.0
                    if candidate > new_sl:
                        new_sl = candidate
                else:
                    candidate = extreme + atr * 1.0
                    if candidate < new_sl:
                        new_sl = candidate
            elif profit_atr >= 0.5:
                if side == "buy":
                    candidate = entry + atr * 0.3
                    if candidate > new_sl:
                        new_sl = candidate
                else:
                    candidate = entry - atr * 0.3
                    if candidate < new_sl:
                        new_sl = candidate

            if new_sl != current_sl:
                entry_state["current_sl"] = new_sl
                moved.append({
                    "symbol": pos.symbol,
                    "side": side,
                    "old_sl": current_sl,
                    "new_sl": new_sl,
                    "tp": entry_state["tp"],
                    "amount": pos.size,
                })
                logger.info(
                    f"Trailing: {pos.symbol} {side.upper()} profit_atr={profit_atr:.2f} "
                    f"SL {current_sl:.4f} → {new_sl:.4f}"
                )

        state["trailing"] = trailing
        _save(state)
        return moved
