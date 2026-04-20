import json
import os
import time
from datetime import datetime, timezone

from config import settings
from src.exchanges.base import Position
from src.strategy.regime import Regime, RegimeResult, BlendedRegimeResult, Bias
from src.utils.logger import logger


STATE_PATH = os.path.join(os.getcwd(), "data", "state.json")

# Correlated-asset clusters — positions inside the same cluster count toward
# a single cap (so three "buy BTC + buy ETH + buy SOL" won't sneak past the
# MAX_SAME_DIRECTION limit).
CORRELATION_CLUSTERS: dict[str, str] = {
    "BTC": "L1_MAJOR",
    "ETH": "L1_MAJOR",
    "SOL": "L1_MAJOR",
    "BNB": "L1_MAJOR",
    "AVAX": "L1_MAJOR",
    "ADA": "L1_MAJOR",
    "DOGE": "MEME",
    "SHIB": "MEME",
    "PEPE": "MEME",
    "WIF": "MEME",
    "BONK": "MEME",
    "HYPE": "L1_MINOR",
    "ARB": "L2_ROLLUP",
    "OP": "L2_ROLLUP",
    "MATIC": "L2_ROLLUP",
    "PAXG": "COMMODITY",
    "XAUT": "COMMODITY",
}


def _load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)
    except OSError as e:
        logger.warning(f"Failed to persist risk state: {e}")


def _cluster_of(symbol_or_coin: str) -> str:
    """Return cluster id for a symbol like BTC/USDT or BTC-USDC or plain BTC."""
    coin = symbol_or_coin.split("/")[0].split("-")[0].upper()
    return CORRELATION_CLUSTERS.get(coin, "OTHER")


class RiskManager:
    def __init__(
        self,
        risk_per_trade_pct: float | None = None,
        max_drawdown_pct: float | None = None,
        max_total_exposure: float | None = None,
        max_positions: int | None = None,
        max_same_direction: int | None = None,
        max_trade_size_usdt: float | None = None,
        max_per_cluster: int | None = None,
        daily_loss_kill_pct: float | None = None,
        daily_lock_hours: float | None = None,
        persist: bool = True,
    ):
        self.risk_per_trade_pct = risk_per_trade_pct or settings.RISK_PER_TRADE_PCT
        self.max_drawdown_pct = max_drawdown_pct or settings.MAX_DRAWDOWN_PCT
        self.max_total_exposure = max_total_exposure or settings.MAX_TOTAL_EXPOSURE
        self.max_positions = max_positions or settings.MAX_POSITIONS
        self.max_same_direction = max_same_direction or settings.MAX_SAME_DIRECTION
        self.max_trade_size_usdt = max_trade_size_usdt or settings.MAX_TRADE_SIZE_USDT
        self.max_per_cluster = max_per_cluster or settings.MAX_PER_CLUSTER
        self.daily_loss_kill_pct = (
            daily_loss_kill_pct if daily_loss_kill_pct is not None
            else getattr(settings, "DAILY_LOSS_KILL_PCT", 0.0)
        )
        self.daily_lock_hours = (
            daily_lock_hours if daily_lock_hours is not None
            else getattr(settings, "DAILY_LOCK_HOURS", 24.0)
        )
        self.persist = persist

        state = _load_state() if persist else {}
        self._peak_balance: float = float(state.get("peak_balance", 0.0))
        # Daily-loss kill-switch state
        self._daily_start_balance: float = float(state.get("daily_start_balance", 0.0))
        self._daily_start_date: str = str(state.get("daily_start_date", ""))
        self._lock_until_ts: float = float(state.get("lock_until_ts", 0.0))
        if self._peak_balance > 0:
            logger.info(f"Restored peak_balance={self._peak_balance:.2f} from {STATE_PATH}")
        if self._lock_until_ts > time.time():
            remain = (self._lock_until_ts - time.time()) / 3600
            logger.warning(f"Daily-loss lock active — {remain:.1f}h remaining")

    # ---- Position sizing ----

    def atr_based_size(self, balance: float, entry_price: float, atr: float) -> float:
        """ATR-based position sizing: risk a fixed % of balance per trade.

        risk_amount = balance * RISK_PER_TRADE_PCT%
        sl_distance = ATR * 1.5
        notional = risk_amount / (sl_distance_pct)

        Low volatility (small ATR) → bigger position
        High volatility (large ATR) → smaller position
        """
        risk_amount = balance * (self.risk_per_trade_pct / 100)
        sl_distance = atr * 1.5
        if sl_distance <= 0 or entry_price <= 0:
            return 0
        sl_pct = sl_distance / entry_price
        notional = risk_amount / sl_pct
        # Cap at 50% of balance per trade
        notional = min(notional, balance * 0.5)
        notional = self.cap_trade_size(notional)
        logger.info(f"ATR sizing: balance={balance:.2f}, risk={risk_amount:.2f}, sl_dist={sl_distance:.2f}, notional={notional:.2f}")
        return notional

    def vol_target_size(
        self,
        balance: float,
        closes,
        target_daily_vol_pct: float | None = None,
        bars_per_day: int = 24,
        window: int = 48,
    ) -> float:
        """Volatility-targeting position sizing.

        We estimate realized daily volatility from log returns of `closes` and
        pick a notional such that::

            notional * daily_vol ≈ balance * target_daily_vol_pct%

        i.e. the expected daily P&L has a consistent risk contribution
        regardless of which symbol we're trading. Unlike ATR sizing, this
        sidesteps the price-level dependence of ATR.

        Args:
            balance:              USDT balance.
            closes:               pandas Series / list of close prices (ordered).
            target_daily_vol_pct: daily vol target in *percent of balance*
                                  (e.g. 1.0 means 1 %).
            bars_per_day:         number of bars per day for this timeframe
                                  (24 for 1h, 96 for 15m, 288 for 5m).
            window:               rolling window (# bars) for vol estimation.

        Returns:
            Notional USDT capped by cap_trade_size and 50 % of balance. Returns
            0.0 if the series is too short or vol is non-finite.
        """
        target = target_daily_vol_pct if target_daily_vol_pct is not None \
            else settings.TARGET_DAILY_VOL_PCT

        # Minimum sample size — need at least a full day of bars to be meaningful.
        try:
            n = len(closes)
        except TypeError:
            return 0.0
        if n < max(bars_per_day + 1, 10):
            return 0.0

        # Keep the last `window` bars to stay responsive.
        try:
            series = closes.iloc[-window:] if hasattr(closes, "iloc") else closes[-window:]
            # log returns
            import math
            import numpy as np
            vals = [float(x) for x in list(series) if x is not None]
            if len(vals) < 10:
                return 0.0
            rets = [math.log(vals[i] / vals[i - 1])
                    for i in range(1, len(vals))
                    if vals[i - 1] > 0 and vals[i] > 0]
            if len(rets) < 5:
                return 0.0
            bar_vol = float(np.std(rets, ddof=1))
        except Exception as e:
            logger.debug(f"vol_target_size: vol estimation failed: {e}")
            return 0.0

        if not bar_vol or bar_vol != bar_vol or bar_vol <= 0:
            # zero / nan vol → can't size, fall through to 0.0 (caller should fallback)
            return 0.0

        # Scale per-bar vol to daily: sigma_daily ≈ sigma_bar * sqrt(bars_per_day)
        import math
        daily_vol = bar_vol * math.sqrt(bars_per_day)

        risk_amount = balance * (target / 100.0)     # USDT per day allowed to fluctuate
        notional = risk_amount / daily_vol            # so that notional * daily_vol ≈ risk

        # Cap at 50 % of balance per trade (same as ATR path) and global cap.
        notional = max(0.0, min(notional, balance * 0.5))
        notional = self.cap_trade_size(notional)
        logger.info(
            f"VolTgt sizing: bal={balance:.2f}, target={target:.2f}%/day, "
            f"daily_vol={daily_vol*100:.2f}% → notional={notional:.2f}"
        )
        return notional

    def scale_by_confidence(self, notional: float, confidence: float) -> float:
        """Scale by confidence — boost instead of pure reduce.

        conf=0.55 -> 0.775x, conf=0.70 -> 0.85x, conf=0.90 -> 0.95x
        """
        factor = 0.5 + confidence * 0.5
        scaled = notional * factor
        logger.info(f"Size scaled: {notional:.2f} * {factor:.3f} = {scaled:.2f} USDT")
        return scaled

    def cap_trade_size(self, notional: float) -> float:
        """Cap trade size to MAX_TRADE_SIZE_USDT."""
        if notional > self.max_trade_size_usdt:
            logger.info(f"Trade size capped: {notional:.2f} -> {self.max_trade_size_usdt:.2f} USDT")
            return self.max_trade_size_usdt
        return notional

    # ---- Pre-trade risk checks ----

    def check_drawdown(self, current_balance: float) -> bool:
        """Check if max drawdown exceeded. Returns True if trading should stop.

        Peak balance is persisted to data/state.json so restarts don't reset it.
        """
        updated = False
        if current_balance > self._peak_balance:
            self._peak_balance = current_balance
            updated = True

        if updated and self.persist:
            state = _load_state()
            state["peak_balance"] = self._peak_balance
            _save_state(state)

        if self._peak_balance == 0:
            return False

        drawdown = ((self._peak_balance - current_balance) / self._peak_balance) * 100
        if drawdown >= self.max_drawdown_pct:
            logger.warning(f"Max drawdown hit: {drawdown:.1f}% (limit: {self.max_drawdown_pct}%)")
            return True
        return False

    def check_daily_loss(self, current_balance: float) -> tuple[bool, str]:
        """Intraday loss kill-switch.

        Tracks a UTC-day anchor balance; if the running loss from that anchor
        exceeds DAILY_LOSS_KILL_PCT, trading is locked for DAILY_LOCK_HOURS.

        Returns (blocked, reason). blocked=True means DO NOT open new trades.
        Existing positions, SL/TP, and trailing stops are NOT touched — those
        remain under the normal execution path so we don't leave exposures
        unmanaged during the lock.

        Disabled when daily_loss_kill_pct <= 0.
        """
        if self.daily_loss_kill_pct <= 0:
            return False, "daily-loss kill disabled"

        now_ts = time.time()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Anchor rollover: new UTC day → reset anchor balance + clear stale lock
        if self._daily_start_date != today or self._daily_start_balance <= 0:
            self._daily_start_balance = float(current_balance)
            self._daily_start_date = today
            # Clearing lock on day rollover is a deliberate policy: 24h pause
            # is already longer than any UTC day rollover; keep whichever is
            # longer by comparing timestamps.
            if self._lock_until_ts <= now_ts:
                self._lock_until_ts = 0.0
            if self.persist:
                state = _load_state()
                state["daily_start_balance"] = self._daily_start_balance
                state["daily_start_date"] = self._daily_start_date
                state["lock_until_ts"] = self._lock_until_ts
                _save_state(state)
            logger.info(f"Daily anchor set: {today} @ balance={current_balance:.2f}")

        # If lock is still in effect from a prior trip, block.
        if self._lock_until_ts > now_ts:
            remain = (self._lock_until_ts - now_ts) / 3600
            return True, f"daily-loss lock: {remain:.1f}h remaining"

        # Compute running loss vs today's anchor.
        if self._daily_start_balance <= 0:
            return False, "no daily anchor yet"
        loss_pct = (self._daily_start_balance - current_balance) / self._daily_start_balance
        if loss_pct >= self.daily_loss_kill_pct:
            # Trip the lock.
            self._lock_until_ts = now_ts + self.daily_lock_hours * 3600
            if self.persist:
                state = _load_state()
                state["lock_until_ts"] = self._lock_until_ts
                _save_state(state)
            msg = (
                f"DAILY LOSS KILL-SWITCH TRIPPED: -{loss_pct*100:.2f}% today "
                f"(limit {self.daily_loss_kill_pct*100:.1f}%) → locked {self.daily_lock_hours}h"
            )
            logger.warning(msg)
            return True, msg

        return False, f"daily loss {loss_pct*100:+.2f}% (limit {self.daily_loss_kill_pct*100:.1f}%)"

    def check_cluster_limit(self, positions: list[Position], symbol: str) -> bool:
        """Return True if opening a new position for `symbol` stays under the
        per-cluster cap. Correlated assets (BTC/ETH/SOL) share a slot."""
        target = _cluster_of(symbol)
        same_cluster = [p for p in positions if _cluster_of(p.symbol) == target]
        if len(same_cluster) >= self.max_per_cluster:
            logger.warning(
                f"Cluster limit: {len(same_cluster)} positions in cluster {target} "
                f"(max {self.max_per_cluster})"
            )
            return False
        return True

    def check_exposure(self, positions: list[Position], balance_total: float) -> bool:
        """Return True if total exposure is within limit."""
        if balance_total <= 0:
            return False
        used = sum(p.size * p.entry_price for p in positions)
        exposure_ratio = used / balance_total
        if exposure_ratio >= self.max_total_exposure:
            logger.warning(
                f"Exposure limit: {exposure_ratio:.0%} >= {self.max_total_exposure:.0%} "
                f"(used={used:.2f}, balance={balance_total:.2f})"
            )
            return False
        return True

    def check_position_count(self, positions: list[Position]) -> bool:
        """Return True if we can open more positions."""
        if len(positions) >= self.max_positions:
            logger.warning(f"Position limit: {len(positions)}/{self.max_positions}")
            return False
        return True

    def check_direction_limit(self, positions: list[Position], action: str) -> bool:
        """Return True if we can open a position in this direction."""
        same_dir = [p for p in positions if p.side == action]
        if len(same_dir) >= self.max_same_direction:
            logger.warning(f"Direction limit: {len(same_dir)} {action} positions (max {self.max_same_direction})")
            return False
        return True

    def pre_trade_check(self, action: str, positions: list[Position], balance: dict, notional: float, symbol: str | None = None) -> tuple[bool, str]:
        """Run all pre-trade risk checks. Returns (allowed, reason)."""
        if action not in ("buy", "sell"):
            return False, "hold action"

        # 1. Max positions
        if not self.check_position_count(positions):
            return False, f"max {self.max_positions} positions reached"

        # 2. Direction limit
        if not self.check_direction_limit(positions, action):
            return False, f"max {self.max_same_direction} {action} positions"

        # 3. Correlation cluster cap (only if symbol provided)
        if symbol and not self.check_cluster_limit(positions, symbol):
            return False, f"cluster {_cluster_of(symbol)} already has {self.max_per_cluster} pos"

        # 4. Total exposure
        if not self.check_exposure(positions, balance["total"]):
            return False, f"exposure >= {self.max_total_exposure:.0%}"

        # 5. Trade size cap / minimum notional
        capped = self.cap_trade_size(notional)
        if capped <= 0:
            return False, "trade size zero"
        if capped < settings.MIN_TRADE_SIZE_USDT:
            return False, f"size ${capped:.2f} < MIN_TRADE_SIZE_USDT (${settings.MIN_TRADE_SIZE_USDT})"

        return True, "ok"

    # ---- Stop loss / Take profit ----

    def calculate_stop_loss(self, entry_price: float, side: str, atr: float, multiplier: float = 1.5) -> float:
        """Calculate stop loss based on ATR."""
        if side == "buy":
            return entry_price - (atr * multiplier)
        return entry_price + (atr * multiplier)

    def calculate_take_profit(self, entry_price: float, side: str, risk_reward_ratio: float = 2.0, stop_distance: float = 0) -> float:
        """Calculate take profit based on risk:reward ratio."""
        if stop_distance == 0:
            return entry_price
        tp_distance = stop_distance * risk_reward_ratio
        if side == "buy":
            return entry_price + tp_distance
        return entry_price - tp_distance

    def regime_size_modifier(self, regime: RegimeResult | BlendedRegimeResult, action: str) -> float:
        """Adjust position size based on market regime, trade direction, and AI bias.

        - BULL + buy  → 1.2x (aggressive with trend)
        - BULL + sell → 0.5x (counter-trend, reduce)
        - BEAR + sell → 1.2x (aggressive with trend)
        - BEAR + buy  → 0.5x (counter-trend, reduce)
        - SIDEWAYS    → 0.6x (defensive, choppy market)

        AI bias override:
        - risk_off + buy  → extra 0.8x penalty
        - risk_on  + sell → extra 0.8x penalty
        """
        regime_enum = regime.regime

        if regime_enum == Regime.BULL:
            modifier = 1.2 if action == "buy" else 0.5
        elif regime_enum == Regime.BEAR:
            modifier = 1.2 if action == "sell" else 0.5
        else:
            modifier = 0.6

        # AI bias override (only on BlendedRegimeResult)
        if isinstance(regime, BlendedRegimeResult):
            if regime.ai_bias == Bias.RISK_OFF and action == "buy":
                modifier *= 0.8
            elif regime.ai_bias == Bias.RISK_ON and action == "sell":
                modifier *= 0.8

        logger.info(f"Regime modifier: {regime_enum.value} + {action} = {modifier}x")
        return modifier
