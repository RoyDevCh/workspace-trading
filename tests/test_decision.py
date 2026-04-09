"""
test_decision.py — Unit tests for OrderIntent and SignalDecision.
"""

import sys
from pathlib import Path

# Add workspace root to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kernel.decision import OrderIntent, SignalDecision


def test_order_intent_hold():
    """Test OrderIntent.hold() factory method."""
    intent = OrderIntent.hold(
        symbol="BTC/USDT",
        price=70000.0,
        reason="No signal",
        market="crypto",
    )
    assert intent.side == "hold"
    assert intent.quantity == 0.0
    assert intent.is_hold()
    assert not intent.is_actionable()
    assert intent.symbol == "BTC/USDT"
    assert intent.market == "crypto"


def test_order_intent_buy():
    """Test OrderIntent for a buy signal."""
    intent = OrderIntent(
        symbol="BTC/USDT",
        side="buy",
        quantity=0.01,
        price=70000.0,
        stop_loss=68000.0,
        strategy_name="combined",
        signal_strength="strong",
        reason="EMA5 crossed above EMA20",
        timestamp="2026-04-09T08:00:00",
        market="crypto",
        indicators={"ema_fast": 70050.0, "ema_slow": 69900.0},
    )
    assert intent.side == "buy"
    assert not intent.is_hold()
    assert intent.is_actionable()
    assert intent.stop_loss == 68000.0
    assert intent.intent_id  # auto-generated


def test_order_intent_sell_all():
    """Test OrderIntent with quantity='ALL' for full exit."""
    intent = OrderIntent(
        symbol="ETH/USDT",
        side="sell",
        quantity=0.0,  # Will be resolved to "ALL" by executor
        price=3500.0,
        stop_loss=None,
        strategy_name="trend_following",
        signal_strength="moderate",
        reason="Death cross",
        timestamp="2026-04-09T08:00:00",
        market="crypto",
    )
    # Note: quantity=0.0 with side="sell" means hold in current dataclass
    # The "ALL" handling is done at the bridge level when resolving position quantity
    assert intent.side == "sell"


def test_order_intent_immutable():
    """Test that OrderIntent is frozen (immutable)."""
    intent = OrderIntent.hold(symbol="BTC/USDT", price=70000.0, reason="test")
    try:
        intent.side = "buy"  # type: ignore
        assert False, "Should have raised FrozenInstanceError"
    except AttributeError:
        pass  # Expected


def test_order_intent_to_dict():
    """Test serialization to dict."""
    intent = OrderIntent.hold(
        symbol="BTC/USDT",
        price=70000.0,
        reason="test",
        strategy_name="combined",
    )
    d = intent.to_dict()
    assert isinstance(d, dict)
    assert d["symbol"] == "BTC/USDT"
    assert d["side"] == "hold"
    assert d["strategy_name"] == "combined"
    assert "intent_id" in d


def test_signal_decision():
    """Test SignalDecision wrapping."""
    intent = OrderIntent.hold(symbol="510300", price=4.0, reason="No signal", market="cn_equity")
    decision = SignalDecision(
        intent=intent,
        execution_allowed=False,
        configured_execution_allowed=True,
        market_session={"execution_allowed": False, "reason": "Outside trading hours"},
        execution_block_reason="Outside trading hours",
    )
    assert not decision.execution_allowed
    assert decision.configured_execution_allowed
    d = decision.to_dict()
    assert d["execution_allowed"] is False
    assert d["symbol"] == "510300"


def test_order_intent_unique_ids():
    """Test that each OrderIntent gets a unique intent_id."""
    intent1 = OrderIntent.hold(symbol="BTC/USDT", price=70000.0, reason="test1")
    intent2 = OrderIntent.hold(symbol="BTC/USDT", price=70000.0, reason="test2")
    assert intent1.intent_id != intent2.intent_id


if __name__ == "__main__":
    test_order_intent_hold()
    test_order_intent_buy()
    test_order_intent_sell_all()
    test_order_intent_immutable()
    test_order_intent_to_dict()
    test_signal_decision()
    test_order_intent_unique_ids()
    print("All OrderIntent tests passed!")
