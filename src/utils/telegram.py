import threading
from config import settings
from src.utils.logger import logger


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

        if self.enabled:
            logger.info("Telegram bot enabled")
        else:
            logger.info("Telegram bot disabled")

    def set_callbacks(self, get_balance=None, get_positions=None, get_trades=None, stop_bot=None):
        """Set callbacks for command handlers to access bot state."""
        self._get_balance = get_balance
        self._get_positions = get_positions
        self._get_trades = get_trades
        self._stop_bot = stop_bot

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

    def send_trade_alert(self, side: str, symbol: str, price: float, amount: float, sl: float = 0, tp: float = 0):
        """Send a formatted trade execution alert."""
        emoji = "🟢" if side == "buy" else "🔴"
        msg = (
            f"{emoji} <b>{side.upper()} {symbol}</b>\n"
            f"Price: <code>{price:.2f}</code>\n"
            f"Size: <code>{amount:.6f}</code>"
        )
        if sl:
            msg += f"\nStop-Loss: <code>{sl:.2f}</code>"
        if tp:
            msg += f"\nTake-Profit: <code>{tp:.2f}</code>"
        self.send_message(msg)

    def send_error_alert(self, error: str):
        """Send an error/critical alert."""
        self.send_message(f"⚠️ <b>Error:</b>\n<code>{error}</code>")

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
