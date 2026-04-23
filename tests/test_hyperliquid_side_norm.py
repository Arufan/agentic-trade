"""Regression test for the HL side-normalization bug.

In v1, main.py passed `decision["action"]` (a plain str) into
`exchange.place_order_with_sl_tp(...)`. That string flowed down into
`HyperliquidExchange.place_order`, which crashed on `side.value` *after*
the order had already been signed and sent to Hyperliquid. The practical
effect: an orphan position on HL with no SL/TP, no journal entry, and no
trailing-stop registration. See Apr-21-2026 live-test incident (Short
PAXG 0.003 @ 4504.7, hash 0x3a3746ad…).

These tests lock in the fix: every entry point that takes a `side`
parameter must accept both `OrderSide` enum and plain "buy"/"sell"
strings without raising AttributeError.
"""
from unittest.mock import patch, MagicMock

import pytest

from src.exchanges.base import OrderSide, OrderType
from src.exchanges.hyperliquid import HyperliquidExchange


def _make_exchange():
    """Build an HL client without touching env or network."""
    with patch.object(HyperliquidExchange, "__init__", lambda self: None):
        ex = HyperliquidExchange()
    ex.wallet_address = "0x0000000000000000000000000000000000000001"
    ex.private_key = "0x" + "11" * 32
    ex._use_vault = False
    ex._sz_decimals = {"PAXG": 4, "BTC": 5, "ETH": 4}
    ex._asset_index = {"PAXG": 0, "BTC": 1, "ETH": 2}
    return ex


def _stub_internals(ex, mid: float = 4500.0):
    """Stub the network-bound helpers so place_order runs fully in-process."""
    ex._coin = lambda symbol: symbol.split("/")[0]
    ex._asset = lambda symbol: ex._asset_index[symbol.split("/")[0]]
    ex._get_mid_price = lambda coin: mid
    ex._send_order = MagicMock(return_value={
        "status": "ok",
        "response": {
            "data": {
                "statuses": [{"filled": {"oid": 123456, "totalSz": "0.003", "avgPx": str(mid)}}]
            }
        },
    })


@pytest.mark.parametrize("side_input", ["buy", "sell", "BUY", "Sell"])
def test_place_order_accepts_string_side(side_input):
    """place_order must not crash when called with a plain string side."""
    ex = _make_exchange()
    _stub_internals(ex)

    # This call would previously raise AttributeError('str' obj has no .value)
    # *after* _send_order, leaving an orphan fill on HL.
    order = ex.place_order("PAXG/USDC", side_input, 0.003, OrderType.MARKET)

    assert order.status == "filled"
    assert order.id == "123456"
    # Side gets normalized: is_buy flag in the outgoing wire must reflect intent
    sent_wire = ex._send_order.call_args[0][0]
    expected_buy = side_input.lower() == "buy"
    assert sent_wire["b"] is expected_buy


@pytest.mark.parametrize("side_input", [OrderSide.BUY, OrderSide.SELL])
def test_place_order_still_accepts_enum(side_input):
    """Belt-and-suspenders fix must not break the normal enum path."""
    ex = _make_exchange()
    _stub_internals(ex)

    order = ex.place_order("BTC/USDC", side_input, 0.001, OrderType.MARKET)
    assert order.status == "filled"

    sent_wire = ex._send_order.call_args[0][0]
    assert sent_wire["b"] is (side_input == OrderSide.BUY)


def test_place_order_with_sl_tp_accepts_string_side():
    """The wrapper that main.py actually calls must also tolerate strings."""
    ex = _make_exchange()
    _stub_internals(ex)
    ex._send_tpsl = MagicMock(return_value={"status": "ok", "response": {}})

    entry, sl, tp = ex.place_order_with_sl_tp(
        "PAXG/USDC", "sell", 0.003,
        entry_price=4504.7, sl_price=4554.0, tp_price=4400.0,
    )
    assert entry.status == "filled"
    assert sl == 4554.0 and tp == 4400.0
    # SL/TP pair was actually constructed and sent (would fail if side.value crashed inside _build_sl_tp_action)
    assert ex._send_tpsl.called


def test_build_sl_tp_action_accepts_string_side():
    """The action-builder helper also takes a side and must tolerate strings."""
    ex = _make_exchange()
    ex._coin = lambda s: s.split("/")[0]
    ex._asset = lambda s: ex._asset_index[s.split("/")[0]]

    action = ex._build_sl_tp_action(
        "PAXG/USDC", "buy", 0.003,
        sl_price=4400.0, tp_price=4700.0, reference_price=4500.0,
    )
    assert action["grouping"] == "normalTpsl"
    assert len(action["orders"]) == 2
    # reduce-only on both, close side is opposite of entry
    assert all(o["r"] is True for o in action["orders"])
    # For long entry, close side is sell → b=False on both SL and TP wires
    assert all(o["b"] is False for o in action["orders"])
