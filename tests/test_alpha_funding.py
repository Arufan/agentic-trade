"""Unit tests for the funding contrarian alpha module."""

from src.strategy.alpha.funding_contrarian import detect_funding_contrarian
from src.strategy.alpha.base import AlphaSource
from src.strategy.technical import Signal


HOURS_PER_YEAR = 24 * 365


def _rate_for_annual(annual: float) -> float:
    return annual / HOURS_PER_YEAR


def test_no_price_history_returns_hold():
    sig = detect_funding_contrarian(
        funding_1h=_rate_for_annual(0.80),
        recent_price_pct=None,
    )
    assert sig.action == Signal.HOLD
    assert "insufficient" in sig.reasoning.lower()
    assert sig.source == AlphaSource.FUNDING_CONTRARIAN


def test_moderate_funding_no_signal():
    # 30% annual funding < 50% threshold → HOLD
    sig = detect_funding_contrarian(
        funding_1h=_rate_for_annual(0.30),
        recent_price_pct=0.05,
    )
    assert sig.action == Signal.HOLD
    assert "not extreme" in sig.reasoning.lower()


def test_extreme_funding_flat_price_is_hold():
    # Funding 80% but price flat 1% → no squeeze setup
    sig = detect_funding_contrarian(
        funding_1h=_rate_for_annual(0.80),
        recent_price_pct=0.01,
        min_price_move=0.02,
    )
    assert sig.action == Signal.HOLD
    assert "flat" in sig.reasoning.lower() or "no squeeze" in sig.reasoning.lower()


def test_crowded_longs_fade_signals_sell():
    # Funding +80% annual, price pumped +5% → crowded longs → SELL (fade)
    sig = detect_funding_contrarian(
        funding_1h=_rate_for_annual(0.80),
        recent_price_pct=0.05,
    )
    assert sig.action == Signal.SELL
    assert sig.metadata.get("pattern") == "long_squeeze_setup"
    assert sig.strength >= 0.5


def test_crowded_shorts_fade_signals_buy():
    # Funding -80% annual, price dumped -5% → crowded shorts → BUY (fade)
    sig = detect_funding_contrarian(
        funding_1h=_rate_for_annual(-0.80),
        recent_price_pct=-0.05,
    )
    assert sig.action == Signal.BUY
    assert sig.metadata.get("pattern") == "short_squeeze_setup"


def test_funding_price_mismatch_is_hold():
    # Positive funding but price dumped → funding/price divergence → HOLD
    sig = detect_funding_contrarian(
        funding_1h=_rate_for_annual(0.80),
        recent_price_pct=-0.05,
    )
    assert sig.action == Signal.HOLD
    assert "mismatch" in sig.reasoning.lower()


def test_strength_scales_with_magnitude():
    weak = detect_funding_contrarian(
        funding_1h=_rate_for_annual(0.55),  # just over threshold
        recent_price_pct=0.03,
    )
    strong = detect_funding_contrarian(
        funding_1h=_rate_for_annual(2.00),  # 4x threshold
        recent_price_pct=0.10,
    )
    assert weak.action == Signal.SELL
    assert strong.action == Signal.SELL
    assert strong.strength >= weak.strength


def test_strength_capped_at_one():
    sig = detect_funding_contrarian(
        funding_1h=_rate_for_annual(5.00),
        recent_price_pct=0.50,
    )
    assert sig.strength <= 1.0


def test_custom_threshold_respected():
    # With a high 100% threshold, 60% funding shouldn't trigger
    sig = detect_funding_contrarian(
        funding_1h=_rate_for_annual(0.60),
        recent_price_pct=0.05,
        extreme_annual=1.00,
    )
    assert sig.action == Signal.HOLD


def test_metadata_includes_annualized_value():
    sig = detect_funding_contrarian(
        funding_1h=_rate_for_annual(0.80),
        recent_price_pct=0.05,
    )
    assert "annualized" in sig.metadata
    assert abs(sig.metadata["annualized"] - 0.80) < 0.01
