"""Key price-level engine.

Computes higher-timeframe pivots that tend to act as support/resistance
magnets and reaction zones:

  - prev quarter mid         (HTF pivot; strong S/R)
  - current year mid         (HTF pivot; yearly anchor)
  - prev month high          (monthly supply)
  - prev week high/mid/low   (weekly range references)
  - weekly open              (current ISO week Monday open)
  - weekly low               (current week's rolling low)
  - monday high/mid/low      (current week Monday bar)
  - daily open               (today's session open)

The engine returns a ``KeyLevelResult`` with:

  * ``levels``             — full list of named levels with priority.
  * ``nearest_support``    — highest level below current price.
  * ``nearest_resistance`` — lowest level above current price.
  * ``confluence_zones``   — price zones where ≥2 levels stack within
                             a tight band (default 0.4% of price).
  * ``bias_score``         — signed -1..+1 summary:
                               +  : price leaning against a support zone
                               -  : price leaning against a resistance zone
  * ``confluence_score``   — 0..1, how "sticky" the nearest zone is.

Design note: this module is pure — it expects a daily OHLC dataframe
indexed by datetime (UTC). The caller (live loop / backtest) is
responsible for fetching enough history (≥ 120 bars recommended so
quarterly pivots are always populated).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from src.utils.logger import logger


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

# Priority is intentionally coarse: high-TF pivots (quarter/year) dominate,
# weekly mid-range comes next, daily/session references last. Tie-breakers
# within a band use absolute distance from price.
PRIORITY: dict[str, int] = {
    "prev_quarter_mid": 5,
    "current_year_mid": 5,
    "prev_month_high":  4,
    "prev_week_high":   4,
    "prev_week_mid":    3,
    "prev_week_low":    4,
    "weekly_open":      3,
    "weekly_low":       3,
    "monday_high":      3,
    "monday_mid":       2,
    "monday_low":       3,
    "daily_open":       2,
}


@dataclass
class KeyLevel:
    name: str
    price: float
    priority: int

    @property
    def is_valid(self) -> bool:
        return self.price > 0


@dataclass
class KeyLevelResult:
    levels: list[KeyLevel]
    nearest_support: Optional[KeyLevel]
    nearest_resistance: Optional[KeyLevel]
    confluence_zones: list[tuple[float, list[str]]] = field(default_factory=list)
    bias_score: float = 0.0          # -1..+1 (signed lean)
    confluence_score: float = 0.0    # 0..1 (stickiness of nearest zone)
    reasoning: str = ""

    def as_dict(self) -> dict:
        return {
            "nearest_support": self.nearest_support.name if self.nearest_support else None,
            "nearest_support_price": self.nearest_support.price if self.nearest_support else None,
            "nearest_resistance": self.nearest_resistance.name if self.nearest_resistance else None,
            "nearest_resistance_price": self.nearest_resistance.price if self.nearest_resistance else None,
            "bias_score": round(self.bias_score, 3),
            "confluence_score": round(self.confluence_score, 3),
            "n_levels": len(self.levels),
            "n_zones": len(self.confluence_zones),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_utc_index(df: pd.DataFrame) -> pd.DataFrame:
    """Return df with a timezone-aware UTC DatetimeIndex."""
    if df.empty:
        return df
    idx = df.index
    if not isinstance(idx, pd.DatetimeIndex):
        df = df.copy()
        df.index = pd.to_datetime(df.index, utc=True)
    elif idx.tz is None:
        df = df.copy()
        df.index = df.index.tz_localize("UTC")
    elif str(idx.tz) != "UTC":
        df = df.copy()
        df.index = df.index.tz_convert("UTC")
    return df


def _slice(df: pd.DataFrame, start: datetime, end: datetime) -> pd.DataFrame:
    """Inclusive start, exclusive end. Handles empty slices safely."""
    if df.empty:
        return df
    return df[(df.index >= start) & (df.index < end)]


def _week_monday(dt: datetime) -> datetime:
    """Return Monday 00:00 UTC of the ISO week containing dt."""
    # Python: Monday=0 ... Sunday=6
    days_since_mon = dt.weekday()
    monday = (dt - timedelta(days=days_since_mon)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday


def _quarter_bounds(dt: datetime) -> tuple[datetime, datetime]:
    """Return (start_of_quarter, start_of_next_quarter) in UTC."""
    q_start_month = ((dt.month - 1) // 3) * 3 + 1
    start = datetime(dt.year, q_start_month, 1, tzinfo=timezone.utc)
    if q_start_month == 10:
        end = datetime(dt.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(dt.year, q_start_month + 3, 1, tzinfo=timezone.utc)
    return start, end


# ---------------------------------------------------------------------------
# Per-level computation
# ---------------------------------------------------------------------------

def _level_from_slice(df_slice: pd.DataFrame, kind: str) -> Optional[float]:
    """Return high/low/mid/open of a df slice; None if empty."""
    if df_slice.empty:
        return None
    try:
        if kind == "high":
            return float(df_slice["high"].max())
        if kind == "low":
            return float(df_slice["low"].min())
        if kind == "mid":
            return float((df_slice["high"].max() + df_slice["low"].min()) / 2.0)
        if kind == "open":
            return float(df_slice["open"].iloc[0])
    except Exception as e:
        logger.debug(f"levels: failed to compute {kind}: {e}")
    return None


def _compute_all_levels(df: pd.DataFrame, now: datetime) -> list[KeyLevel]:
    """Compute the full set of key levels from a daily-or-finer df.

    The df must have columns: open, high, low, close (ohlcv standard).
    Indexed by UTC datetimes.
    """
    df = _ensure_utc_index(df)
    levels: list[KeyLevel] = []

    def _add(name: str, price: Optional[float]) -> None:
        if price is not None and price > 0:
            levels.append(KeyLevel(name=name, price=price, priority=PRIORITY[name]))

    # --- Quarterly pivot (previous quarter) ---
    q_start, q_end = _quarter_bounds(now)
    # Previous quarter = [q_start - (q_end - q_start), q_start)
    prev_q_start = q_start - (q_end - q_start)
    prev_q_slice = _slice(df, prev_q_start, q_start)
    _add("prev_quarter_mid", _level_from_slice(prev_q_slice, "mid"))

    # --- Yearly pivot (current year-to-date) ---
    year_start = datetime(now.year, 1, 1, tzinfo=timezone.utc)
    ytd_slice = _slice(df, year_start, now)
    _add("current_year_mid", _level_from_slice(ytd_slice, "mid"))

    # --- Previous month ---
    if now.month == 1:
        prev_month_start = datetime(now.year - 1, 12, 1, tzinfo=timezone.utc)
        prev_month_end = datetime(now.year, 1, 1, tzinfo=timezone.utc)
    else:
        prev_month_start = datetime(now.year, now.month - 1, 1, tzinfo=timezone.utc)
        prev_month_end = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    pm_slice = _slice(df, prev_month_start, prev_month_end)
    _add("prev_month_high", _level_from_slice(pm_slice, "high"))

    # --- Previous week ---
    monday_this = _week_monday(now)
    monday_prev = monday_this - timedelta(days=7)
    prev_week_slice = _slice(df, monday_prev, monday_this)
    _add("prev_week_high", _level_from_slice(prev_week_slice, "high"))
    _add("prev_week_low",  _level_from_slice(prev_week_slice, "low"))
    _add("prev_week_mid",  _level_from_slice(prev_week_slice, "mid"))

    # --- Current week: open + Monday bar + running low ---
    this_week_slice = _slice(df, monday_this, monday_this + timedelta(days=7))
    if not this_week_slice.empty:
        try:
            _add("weekly_open", float(this_week_slice["open"].iloc[0]))
        except Exception:
            pass
        _add("weekly_low", _level_from_slice(this_week_slice, "low"))

        # Monday bar only — one calendar day.
        monday_bar = _slice(df, monday_this, monday_this + timedelta(days=1))
        _add("monday_high", _level_from_slice(monday_bar, "high"))
        _add("monday_low",  _level_from_slice(monday_bar, "low"))
        _add("monday_mid",  _level_from_slice(monday_bar, "mid"))

    # --- Daily open (today) ---
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_bar = _slice(df, today, today + timedelta(days=1))
    if not today_bar.empty:
        try:
            _add("daily_open", float(today_bar["open"].iloc[0]))
        except Exception:
            pass

    return levels


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _build_confluence_zones(
    levels: list[KeyLevel],
    current_price: float,
    band_pct: float = 0.004,
) -> list[tuple[float, list[str]]]:
    """Cluster levels that sit within ``band_pct`` of each other.

    Returns list of (zone_price, [level_names]) sorted by zone_price asc.
    Only zones with ≥ 2 members are kept.
    """
    if current_price <= 0 or not levels:
        return []

    # Sort by price, greedy cluster
    sorted_levels = sorted(levels, key=lambda lv: lv.price)
    zones: list[list[KeyLevel]] = []
    current_cluster: list[KeyLevel] = []
    for lv in sorted_levels:
        if not current_cluster:
            current_cluster = [lv]
            continue
        band = current_cluster[0].price * band_pct
        if abs(lv.price - current_cluster[0].price) <= band:
            current_cluster.append(lv)
        else:
            if len(current_cluster) >= 2:
                zones.append(current_cluster)
            current_cluster = [lv]
    if len(current_cluster) >= 2:
        zones.append(current_cluster)

    out: list[tuple[float, list[str]]] = []
    for cluster in zones:
        mean_price = sum(lv.price for lv in cluster) / len(cluster)
        names = [lv.name for lv in cluster]
        out.append((mean_price, names))
    return out


def _nearest_levels(
    levels: list[KeyLevel], price: float,
) -> tuple[Optional[KeyLevel], Optional[KeyLevel]]:
    below = [lv for lv in levels if lv.price < price]
    above = [lv for lv in levels if lv.price > price]
    nearest_support = max(below, key=lambda lv: lv.price) if below else None
    nearest_resistance = min(above, key=lambda lv: lv.price) if above else None
    return nearest_support, nearest_resistance


def _bias_and_confluence(
    levels: list[KeyLevel],
    current_price: float,
    nearest_support: Optional[KeyLevel],
    nearest_resistance: Optional[KeyLevel],
    zones: list[tuple[float, list[str]]],
    proximity_pct: float = 0.006,
) -> tuple[float, float]:
    """Compute (bias_score, confluence_score).

    bias_score:
      +1.0 : price sitting exactly on a high-priority support zone (bullish)
      -1.0 : price sitting on a high-priority resistance zone (bearish)
       0.0 : price comfortably between levels, no strong lean

    confluence_score:
      0..1 based on how many of the confluence zones are within the
      proximity band of the current price, weighted by priority.
    """
    if current_price <= 0 or not levels:
        return 0.0, 0.0

    def _distance_pct(p: float) -> float:
        return abs(current_price - p) / current_price

    # Bias from nearest support/resistance: closer = stronger signal.
    bias = 0.0
    if nearest_support is not None:
        dist_pct = _distance_pct(nearest_support.price)
        if dist_pct <= proximity_pct:
            # Price is hugging support → bullish lean, scaled by priority.
            strength = (1.0 - dist_pct / proximity_pct) * (nearest_support.priority / 5.0)
            bias += min(1.0, strength)
    if nearest_resistance is not None:
        dist_pct = _distance_pct(nearest_resistance.price)
        if dist_pct <= proximity_pct:
            strength = (1.0 - dist_pct / proximity_pct) * (nearest_resistance.priority / 5.0)
            bias -= min(1.0, strength)

    bias = max(-1.0, min(1.0, bias))

    # Confluence: if a confluence zone is within proximity, boost the score.
    conf = 0.0
    for zone_price, names in zones:
        dist_pct = _distance_pct(zone_price)
        if dist_pct <= proximity_pct:
            # Weight by number of members and their priorities.
            member_priority = sum(PRIORITY[n] for n in names) / 5.0
            nearness = 1.0 - dist_pct / proximity_pct
            conf = max(conf, min(1.0, nearness * member_priority / max(1, len(names))))

    return bias, conf


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_key_levels(
    daily_df: pd.DataFrame,
    current_price: float,
    symbol: str = "",
    now: Optional[datetime] = None,
    band_pct: float = 0.004,
    proximity_pct: float = 0.006,
) -> KeyLevelResult:
    """Build a KeyLevelResult from a daily-resolution dataframe.

    ``daily_df`` should have at least ~120 rows so the quarterly pivot
    is always computable. Fewer rows are tolerated — the missing
    levels simply won't appear in the output.

    ``now`` defaults to UTC now. Pass explicitly in backtests.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if daily_df is None or daily_df.empty:
        return KeyLevelResult(levels=[], nearest_support=None, nearest_resistance=None,
                               reasoning="no data")

    required = {"open", "high", "low", "close"}
    if not required.issubset(daily_df.columns):
        logger.warning(f"levels: {symbol} daily_df missing OHLC columns")
        return KeyLevelResult(levels=[], nearest_support=None, nearest_resistance=None,
                               reasoning="missing OHLC columns")

    levels = _compute_all_levels(daily_df, now)
    if not levels:
        return KeyLevelResult(levels=[], nearest_support=None, nearest_resistance=None,
                               reasoning="no levels computed")

    nearest_support, nearest_resistance = _nearest_levels(levels, current_price)
    zones = _build_confluence_zones(levels, current_price, band_pct=band_pct)
    bias, confluence = _bias_and_confluence(
        levels, current_price, nearest_support, nearest_resistance,
        zones, proximity_pct=proximity_pct,
    )

    # Reasoning string — readable log / telegram fragment.
    sup_txt = f"{nearest_support.name}@{nearest_support.price:.2f}" if nearest_support else "—"
    res_txt = f"{nearest_resistance.name}@{nearest_resistance.price:.2f}" if nearest_resistance else "—"
    reasoning = (
        f"sup={sup_txt} res={res_txt} "
        f"bias={bias:+.2f} confluence={confluence:.2f} "
        f"({len(levels)} levels, {len(zones)} zones)"
    )

    return KeyLevelResult(
        levels=levels,
        nearest_support=nearest_support,
        nearest_resistance=nearest_resistance,
        confluence_zones=zones,
        bias_score=bias,
        confluence_score=confluence,
        reasoning=reasoning,
    )
