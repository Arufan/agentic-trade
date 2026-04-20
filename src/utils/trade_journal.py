import json
import os
import uuid
from datetime import datetime, timedelta
from src.utils.logger import logger

JOURNAL_PATH = os.path.join(os.getcwd(), "data", "trades.json")


def _load() -> list:
    if not os.path.exists(JOURNAL_PATH):
        return []
    try:
        with open(JOURNAL_PATH, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def _save(trades: list):
    os.makedirs(os.path.dirname(JOURNAL_PATH), exist_ok=True)
    with open(JOURNAL_PATH, "w") as f:
        json.dump(trades, f, indent=2, default=str)


class TradeJournal:
    def log_entry(self, symbol: str, side: str, price: float, amount: float,
                  confidence: float, reasoning: str, indicators: dict,
                  sentiment: str) -> str:
        """Log a new trade entry. Returns the trade ID."""
        trade_id = str(uuid.uuid4())[:8]
        entry = {
            "id": trade_id,
            "timestamp": datetime.utcnow().isoformat(),
            "symbol": symbol,
            "side": side,
            "entry_price": price,
            "amount": amount,
            "confidence": confidence,
            "reasoning": reasoning,
            "indicators": indicators,
            "sentiment": sentiment,
            "status": "open",
            "exit_price": None,
            "exit_time": None,
            "pnl": None,
            "pnl_pct": None,
        }
        trades = _load()
        trades.append(entry)
        _save(trades)
        logger.info(f"Trade journal: logged {side} {symbol} entry (id={trade_id})")
        return trade_id

    def close_trade(self, trade_id: str, exit_price: float, pnl: float):
        """Mark a trade as closed with exit price and PnL."""
        trades = _load()
        for t in trades:
            if t["id"] == trade_id:
                t["status"] = "closed"
                t["exit_price"] = exit_price
                t["exit_time"] = datetime.utcnow().isoformat()
                t["pnl"] = round(pnl, 4)
                entry = t["entry_price"] or 0
                if entry > 0:
                    t["pnl_pct"] = round((pnl / entry) * 100, 2)
                logger.info(f"Trade journal: closed {t['symbol']} {t['side']} (pnl={pnl:.4f})")
                break
        _save(trades)

    def close_by_symbol(self, symbol: str, side: str, exit_price: float, pnl: float):
        """Close the most recent open trade matching symbol/side."""
        trades = _load()
        for t in reversed(trades):
            if t["symbol"] == symbol and t["status"] == "open":
                # For longs closed by sells, or shorts closed by buys
                if (t["side"] == "buy" and side == "sell") or (t["side"] == "sell" and side == "buy"):
                    self.close_trade(t["id"], exit_price, pnl)
                    return t["id"]
        return None

    def get_open_trades(self) -> list:
        """Get all currently open trades."""
        return [t for t in _load() if t["status"] == "open"]

    def get_open_trade_for(self, symbol: str, side: str):
        """Get the most recent open trade for a symbol/side."""
        for t in reversed(_load()):
            if t["symbol"] == symbol and t["status"] == "open":
                return t
        return None

    def get_recent_trades(self, n: int = 10) -> list:
        """Get the last N trades (any status)."""
        return _load()[-n:]

    def get_performance_summary(self, days: int = 7) -> str:
        """Generate a human-readable performance summary for the AI prompt."""
        trades = _load()
        if not trades:
            return "No trade history yet. This is your first session."

        cutoff = datetime.utcnow() - timedelta(days=days)
        recent = []
        for t in trades:
            try:
                ts = datetime.fromisoformat(t["timestamp"])
                if ts >= cutoff:
                    recent.append(t)
            except (ValueError, TypeError):
                recent.append(t)

        closed = [t for t in recent if t["status"] == "closed" and t.get("pnl") is not None]
        if not closed:
            open_count = len([t for t in recent if t["status"] == "open"])
            summary = f"No closed trades in the last {days} days."
            if open_count:
                summary += f" {open_count} position(s) currently open."
            return summary

        wins = [t for t in closed if t["pnl"] > 0]
        losses = [t for t in closed if t["pnl"] <= 0]
        win_rate = (len(wins) / len(closed)) * 100 if closed else 0
        net_pnl = sum(t["pnl"] for t in closed)

        # Current streak
        streak = 0
        streak_type = ""
        for t in reversed(closed):
            is_win = t["pnl"] > 0
            if streak == 0:
                streak_type = "win" if is_win else "loss"
                streak = 1
            elif (is_win and streak_type == "win") or (not is_win and streak_type == "loss"):
                streak += 1
            else:
                break

        lines = [
            f"Last {days} days: {len(closed)} closed trades, {len(wins)} wins ({win_rate:.0f}%), net PnL: {'+'if net_pnl>=0 else ''}{net_pnl:.2f} USDT",
            f"Current streak: {streak} {streak_type}{'s' if streak>1 else ''}",
        ]

        # Last 5 closed trades
        lines.append("\nRecent trades:")
        for t in closed[-5:]:
            icon = "✅" if t["pnl"] > 0 else "❌"
            pnl_s = f"{'+'if t['pnl']>0 else ''}{t['pnl']:.2f}"
            pnl_pct = t.get("pnl_pct", 0)
            lines.append(
                f"  {icon} {t['side'].upper()} {t['symbol']} @ {t['entry_price']:.2f} → "
                f"{t.get('exit_price', '?')} ({pnl_s}, {'+'if pnl_pct>=0 else ''}{pnl_pct:.2f}%) — \"{t.get('reasoning', '')[:60]}\""
            )

        # Pattern detection
        if len(losses) >= 2:
            loss_sides = [t["side"] for t in losses[-5:]]
            if loss_sides.count("sell") >= 2:
                lines.append("\n⚠️ Multiple losing SELL trades recently. Be more cautious with short positions.")
            elif loss_sides.count("buy") >= 2:
                lines.append("\n⚠️ Multiple losing BUY trades recently. Be more cautious with long entries.")

        return "\n".join(lines)

    def get_stats(self) -> dict:
        """Get quick stats for Telegram reporting."""
        trades = _load()
        closed = [t for t in trades if t["status"] == "closed" and t.get("pnl") is not None]
        wins = [t for t in closed if t["pnl"] > 0]
        return {
            "total_trades": len(trades),
            "closed_trades": len(closed),
            "open_trades": len([t for t in trades if t["status"] == "open"]),
            "wins": len(wins),
            "losses": len(closed) - len(wins),
            "win_rate": (len(wins) / len(closed) * 100) if closed else 0,
            "net_pnl": sum(t["pnl"] for t in closed) if closed else 0,
        }


# Singleton
journal = TradeJournal()
