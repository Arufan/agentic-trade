import threading
from config import settings
from src.utils.logger import logger


def _get_journal():
    from src.utils.trade_journal import journal
    return journal


class TelegramBot:
    """Telegram bot for trade notifications and remote commands."""

    def __init__(self):
        self.enabled = settings.TELEGRAM_ENABLED and bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID)
        self.token = settings.TELEGRAM_BOT_TOKEN
        self.chat_id = settings.TELEGRAM_CHAT_ID
        self._app = None
        self._thread = None
        self._bot_instance = None
        self._stop_event = threading.Event()

        # Shared state for command responses (set by main loop)
        self._get_balance = None  # callable → dict
        self._get_positions = None  # callable → list
        self._get_trades = None  # callable → list
        self._stop_bot = None  # callable → None
        self._run_screening = None  # callable → str (returns screening result)

        if self.enabled:
            logger.info("Telegram bot enabled")
        else:
            logger.info("Telegram bot disabled")

    def set_callbacks(self, get_balance=None, get_positions=None, get_trades=None, stop_bot=None, run_screening=None):
        """Set callbacks for command handlers to access bot state."""
        self._get_balance = get_balance
        self._get_positions = get_positions
        self._get_trades = get_trades
        self._stop_bot = stop_bot
        self._run_screening = run_screening

    def send_message(self, text: str):
        """Send a message to the Telegram chat."""
        if not self.enabled:
            return
        try:
            import requests
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            requests.post(url, json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
            }, timeout=10)
        except Exception as e:
            logger.warning(f"Telegram send failed: {e}")

    def send_trade_alert(self, side: str, symbol: str, price: float, amount: float,
                         sl: float = 0, tp: float = 0, **kwargs):
        """Send a formatted trade execution alert with detailed analysis."""
        from datetime import datetime, timezone, timedelta

        confidence = kwargs.get("confidence", 0)
        reasoning = kwargs.get("reasoning", "")
        indicators = kwargs.get("indicators", {})
        sentiment_summary = kwargs.get("sentiment_summary", "")
        sentiment_val = kwargs.get("sentiment", "neutral")
        sentiment_conf = kwargs.get("sentiment_confidence", 0)
        risk_pct = kwargs.get("risk_pct", 0)
        notional = kwargs.get("notional", 0)
        atr = indicators.get("atr", 0)
        rsi = indicators.get("rsi", 0)
        macd = indicators.get("macd_hist", 0)
        ema_8 = indicators.get("ema_8", 0)
        ema_21 = indicators.get("ema_21", 0)
        ema_55 = indicators.get("ema_55", 0)
        adx = indicators.get("adx", 0)
        fvg_signal = indicators.get("fvg_signal", "none")

        direction = "LONG" if side == "buy" else "SHORT"
        dir_icon = "🟢" if side == "buy" else "🔴"

        # Calculate TP levels based on R:R from entry to SL distance
        sl_distance = abs(price - sl) if sl else atr * 1.5
        if sl_distance == 0:
            sl_distance = price * 0.01

        if side == "buy":
            tp1 = price + sl_distance * 1.0
            tp2 = price + sl_distance * 2.0
            tp3 = price + sl_distance * 3.0
        else:
            tp1 = price - sl_distance * 1.0
            tp2 = price - sl_distance * 2.0
            tp3 = price - sl_distance * 3.0

        # Technical bias
        tech_signal = kwargs.get("tech_signal", "hold")
        tech_strength = kwargs.get("tech_strength", 0)
        tech_pct = int(tech_strength * 100)

        # Sentiment label
        sent_emoji = {"bullish": "BULLISH", "bearish": "BEARISH", "neutral": "NETRAL"}.get(sentiment_val, "NETRAL")

        # WIB timezone
        wib = timezone(timedelta(hours=7))
        now_wib = datetime.now(wib).strftime("%d %b %Y %H:%M WIB")

        # Build message
        msg = (
            f"<b>{dir_icon} {direction} {symbol}</b>\n"
            f"\n"
            f"Arah: <b>{direction}</b>\n"
            f"Keyakinan: <b>{confidence:.0%}</b>\n"
            f"\n"
            f"📍 Entry: <code>{price:.2f}</code>\n"
            f"🛑 Stop Loss: <code>{sl:.2f}</code> (1.5 ATR = {atr * 1.5:.2f} pts)\n"
            f"🎯 Target 1: <code>{tp1:.2f}</code> (1R — 50% ukuran)\n"
            f"🎯 Target 2: <code>{tp2:.2f}</code> (2R — 30% ukuran)\n"
            f"🎯 Target 3: <code>{tp3:.2f}</code> (3R — 20% trailing)\n"
            f"\n"
            f"📊 <b>ANALISA AGEN:</b>\n"
            f"• Sentimen: {sent_emoji} ({sentiment_conf:.0%})\n"
            f"• AI Digest: {reasoning[:150]}\n"
            f"• Teknikal: {tech_signal.upper()} ({tech_pct}%)\n"
            f"• RSI: {rsi:.1f} | MACD: {macd:.4f} | ADX: {adx:.1f}\n"
            f"• EMA 8/21/55: {ema_8:.2f} / {ema_21:.2f} / {ema_55:.2f}\n"
            f"• FVG: {fvg_signal}\n"
            f"\n"
            f"🔍 <b>RINGKASAN SIGNAL:</b>\n"
            f"• {'Konfirmasi' if confidence >= 0.7 else 'Moderat'} {direction} dengan keyakinan {confidence:.0%}\n"
            f"• Ukuran posisi: ${notional:.2f} ({amount:.6f})\n"
            f"\n"
            f"⚡ Risiko: {risk_pct:.1f}% dari akun\n"
            f"⏰ Waktu: {now_wib}"
        )
        self.send_message(msg)

    def send_error_alert(self, error: str):
        """Send an error/critical alert."""
        self.send_message(f"⚠️ <b>Error:</b>\n<code>{error}</code>")

    def send_cycle_digest(self, cycle_num: int, executed: int, rejected: int,
                          rejection_breakdown: dict, pairs: list[str],
                          budget_snapshot: dict | None = None):
        """Send a compact digest of the last N cycles.

        Used for periodic observability — covers executed/rejected counts,
        rejection-reason breakdown, and Tavily budget state. Called from
        the live loop every TELEGRAM_DIGEST_INTERVAL_CYCLES cycles.
        """
        lines = [
            f"📊 <b>Cycle Digest #{cycle_num}</b>",
            f"Pairs: <code>{', '.join(pairs)}</code>",
            f"Executed: <b>{executed}</b> | Rejected: <b>{rejected}</b>",
        ]
        if rejection_breakdown:
            lines.append("")
            lines.append("<b>Rejection breakdown:</b>")
            for reason, count in sorted(rejection_breakdown.items(), key=lambda x: -x[1]):
                lines.append(f"  • {reason}: {count}")
        if budget_snapshot:
            used = budget_snapshot.get("used", 0)
            threshold = budget_snapshot.get("threshold", 0)
            remaining = budget_snapshot.get("remaining", 0)
            month = budget_snapshot.get("month", "?")
            lines.append("")
            lines.append(
                f"💰 Tavily [{month}]: {used}/{threshold} used "
                f"({remaining} left)"
            )
        self.send_message("\n".join(lines))

    def send_position_close(self, symbol: str, side: str, entry_price: float,
                            exit_price: float, pnl: float, amount: float,
                            duration_s: float | None = None,
                            reason: str = "closed"):
        """Send an immediate alert when a position is closed.

        Called from the journal-reconciliation step once a detected close
        is confirmed. Includes PnL, duration, and the inferred close reason
        (SL hit / TP hit / manual / trailing) when available.
        """
        from datetime import timedelta
        pnl_icon = "✅" if pnl > 0 else ("❌" if pnl < 0 else "⚪")
        pnl_sign = "+" if pnl >= 0 else ""
        direction = "LONG" if side == "buy" else "SHORT"
        move_pct = 0.0
        if entry_price > 0:
            if side == "buy":
                move_pct = (exit_price - entry_price) / entry_price * 100
            else:
                move_pct = (entry_price - exit_price) / entry_price * 100

        duration_part = ""
        if duration_s is not None and duration_s > 0:
            td = timedelta(seconds=int(duration_s))
            duration_part = f"\n⏱ Duration: {td}"

        msg = (
            f"{pnl_icon} <b>Position Closed</b> ({reason})\n"
            f"{direction} <b>{symbol}</b>\n"
            f"\n"
            f"Entry: <code>{entry_price:.4f}</code>\n"
            f"Exit:  <code>{exit_price:.4f}</code>\n"
            f"Move:  <b>{pnl_sign}{move_pct:.2f}%</b>\n"
            f"PnL:   <b>{pnl_sign}{pnl:.4f}</b> USDC\n"
            f"Size:  <code>{amount:.6f}</code>"
            f"{duration_part}"
        )
        self.send_message(msg)

    def send_hold_notice(self, symbol: str, reason: str,
                         signal_action: str, signal_conf: float,
                         ai_action: str, ai_conf: float):
        """Optional 'why we held' note (off by default, noisy).

        Guarded by TELEGRAM_HOLD_ALERT_ENABLED. Useful in the first few
        days of a new strategy-knob tuning cycle to observe what's
        getting filtered, then disable.
        """
        if not bool(getattr(settings, "TELEGRAM_HOLD_ALERT_ENABLED", False)):
            return
        self.send_message(
            f"⏸ <b>HOLD</b> {symbol}: {reason}\n"
            f"  Signal: {signal_action} ({signal_conf:.0%})\n"
            f"  AI: {ai_action} ({ai_conf:.0%})"
        )

    def send_event_warning(self, currency: str, title: str, impact: str,
                           minutes_ahead: float, blackout_min: int = 0,
                           size_mult: float = 1.0, window_h: float = 0.0):
        """Heads-up alert when a high-impact macro event is approaching.

        Typically fired once per event (caller dedupes with last_warned_id).
        Operators use this to size-down manually ahead of the auto-gate
        kicking in, or to watch the tape during FOMC/CPI prints.
        """
        when = (
            f"{minutes_ahead:.0f} min"
            if minutes_ahead < 120
            else f"{minutes_ahead / 60.0:.1f} h"
        )
        parts = [
            f"📅 <b>Macro Event Incoming</b>",
            f"<b>{currency} {title}</b> ({impact})",
            f"in <code>{when}</code>",
        ]
        if blackout_min > 0:
            parts.append(f"• Blackout: no new entries within T-{blackout_min} min")
        if size_mult < 1.0 and window_h > 0:
            parts.append(f"• Size ×{size_mult:.2f} for ±{window_h:.1f}h")
        self.send_message("\n".join(parts))

    def send_pnl_report(self, balance: dict, positions: list):
        """Send a balance and position report."""
        lines = [
            f"📊 <b>Status Report</b>",
            f"Balance: <code>{balance.get('total', 0):.2f} USDC</code>",
            f"Free: <code>{balance.get('free', 0):.2f}</code>",
        ]
        if positions:
            lines.append("\n<b>Open Positions:</b>")
            for p in positions:
                pnl_sign = "+" if p.unrealized_pnl >= 0 else ""
                lines.append(f"  {p.symbol} | {p.side.upper()} | PnL: {pnl_sign}{p.unrealized_pnl:.2f}")
        else:
            lines.append("\nNo open positions")
        self.send_message("\n".join(lines))

    def start_command_listener(self):
        """Start listening for Telegram commands in a background thread."""
        if not self.enabled:
            return
        self._thread = threading.Thread(target=self._run_listener, daemon=True)
        self._thread.start()
        logger.info("Telegram command listener started")

    def _run_listener(self):
        """Run the Telegram polling loop."""
        try:
            import requests
            last_update_id = 0
            while not self._stop_event.is_set():
                try:
                    url = f"https://api.telegram.org/bot{self.token}/getUpdates"
                    resp = requests.post(url, json={
                        "offset": last_update_id + 1,
                        "timeout": 5,
                        "allowed_updates": ["message"],
                    }, timeout=10)
                    data = resp.json()

                    for update in data.get("result", []):
                        last_update_id = update["update_id"]
                        message = update.get("message", {})
                        text = message.get("text", "")
                        chat_id = str(message.get("chat", {"id": 0}).get("id", 0))

                        # Only respond to our chat
                        if chat_id != self.chat_id:
                            continue

                        self._handle_command(text)

                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Telegram listener crashed: {e}")

    def _handle_command(self, text: str):
        """Handle incoming Telegram command."""
        text = text.strip()
        logger.info(f"Telegram command: {text}")

        if text == "/start":
            self.send_message(
                "🤖 <b>Agentic Trade Bot</b> is running!\n\n"
                "Commands:\n"
                "/status — Balance & positions\n"
                "/trades — Recent trades\n"
                "/history — Trade history & performance\n"
                "/screening — Run manual screening now\n"
                "/stop — Stop the bot"
            )

        elif text == "/status":
            if self._get_balance:
                try:
                    balance = self._get_balance()
                    positions = self._get_positions() if self._get_positions else []
                    self.send_pnl_report(balance, positions)
                except Exception as e:
                    self.send_message(f"❌ Failed to get status: {e}")
            else:
                self.send_message("⚠️ Bot not running")

        elif text == "/trades":
            if self._get_trades:
                try:
                    trades = self._get_trades()
                    if not trades:
                        self.send_message("📋 No trades yet")
                    else:
                        lines = ["📋 <b>Recent Trades:</b>"]
                        for t in trades[-10:]:
                            pnl_sign = "+" if t.pnl > 0 else ""
                            lines.append(
                                f"  {t.side.upper()} {t.symbol} @ {t.entry_price:.2f} → "
                                f"{t.exit_price:.2f} | {pnl_sign}{t.pnl:.2f}"
                            )
                        self.send_message("\n".join(lines))
                except Exception as e:
                    self.send_message(f"❌ Failed to get trades: {e}")
            else:
                self.send_message("⚠️ Bot not running")

        elif text == "/history":
            try:
                j = _get_journal()
                stats = j.get_stats()
                trades = j.get_recent_trades(10)
                lines = [
                    f"📊 <b>Trade History</b>",
                    f"Total: {stats['total_trades']} | Open: {stats['open_trades']}",
                    f"Closed: {stats['closed_trades']} ({stats['wins']}W / {stats['losses']}L)",
                    f"Win Rate: {stats['win_rate']:.1f}%",
                    f"Net PnL: {'+'if stats['net_pnl']>=0 else ''}{stats['net_pnl']:.4f} USDT",
                ]
                if trades:
                    lines.append("\n<b>Last trades:</b>")
                    for t in trades[-10:]:
                        if t["status"] == "open":
                            lines.append(f"  📌 {t['side'].upper()} {t['symbol']} @ {t['entry_price']:.2f} (open)")
                        else:
                            icon = "✅" if (t.get("pnl") or 0) > 0 else "❌"
                            pnl_s = f"{'+'if (t.get('pnl') or 0)>0 else ''}{t.get('pnl', 0):.4f}"
                            lines.append(f"  {icon} {t['side'].upper()} {t['symbol']} @ {t['entry_price']:.2f} → {t.get('exit_price', '?')} | {pnl_s}")
                self.send_message("\n".join(lines))
            except Exception as e:
                self.send_message(f"❌ Failed to get history: {e}")

        elif text == "/screening":
            if self._run_screening:
                self.send_message("🔍 <b>Running manual screening...</b>")
                try:
                    result = self._run_screening()
                    self.send_message(result)
                except Exception as e:
                    self.send_message(f"❌ Screening failed: {e}")
            else:
                self.send_message("⚠️ Bot not running")

        elif text == "/stop":
            self.send_message("🛑 <b>Stopping bot...</b>")
            if self._stop_bot:
                self._stop_bot()

    def stop(self):
        """Stop the Telegram listener."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info("Telegram bot stopped")


# Singleton instance
telegram = TelegramBot()
