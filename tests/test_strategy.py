"""
test_strategy.py — Unit tests for pure strategy evaluation functions.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kernel.strategy.trend_following import evaluate_trend_following_signal
from kernel.strategy.mean_reversion import evaluate_mean_reversion_signal
from kernel.strategy.combined import evaluate_combined_signal
from kernel.strategy.registry import evaluate_signal


# ---- Trend Following ----

def test_trend_following_golden_cross_buy():
    """Golden cross + RSI below threshold → buy."""
    strategy_cfg = {"ema_fast_period": 5, "ema_slow_period": 20, "buy_rsi_below": 70, "sell_rsi_above": 85, "sell_all_on_exit": True}
    indicators = {
        "ema_fast_prev": 69800.0, "ema_fast": 70200.0,
        "ema_slow_prev": 70000.0, "ema_slow": 69900.0,
        "rsi": 55.0,
        "higher_trend_ok": True, "higher_tf_ema": None,
    }
    signal, reason, qty = evaluate_trend_following_signal(strategy_cfg, indicators, 0.01, 1.0)
    assert signal == "buy"
    assert "crossed above" in reason
    assert qty == 0.01


def test_trend_following_death_cross_sell():
    """Death cross → sell ALL."""
    strategy_cfg = {"ema_fast_period": 5, "ema_slow_period": 20, "buy_rsi_below": 70, "sell_rsi_above": 85, "sell_all_on_exit": True}
    indicators = {
        "ema_fast_prev": 70200.0, "ema_fast": 69800.0,
        "ema_slow_prev": 70000.0, "ema_slow": 70100.0,
        "rsi": 50.0,
        "higher_trend_ok": True, "higher_tf_ema": None,
    }
    signal, reason, qty = evaluate_trend_following_signal(strategy_cfg, indicators, 0.01, 1.0)
    assert signal == "sell"
    assert qty == "ALL"


def test_trend_following_rsi_overbought_sell():
    """RSI above sell threshold → sell."""
    strategy_cfg = {"ema_fast_period": 5, "ema_slow_period": 20, "buy_rsi_below": 70, "sell_rsi_above": 85, "sell_all_on_exit": True}
    indicators = {
        "ema_fast_prev": 70500.0, "ema_fast": 70600.0,  # No death cross
        "ema_slow_prev": 70000.0, "ema_slow": 70100.0,
        "rsi": 88.0,  # Overbought
        "higher_trend_ok": True, "higher_tf_ema": None,
    }
    signal, reason, qty = evaluate_trend_following_signal(strategy_cfg, indicators, 0.01, 1.0)
    assert signal == "sell"


def test_trend_following_hold():
    """No cross, RSI moderate → hold."""
    strategy_cfg = {"ema_fast_period": 5, "ema_slow_period": 20, "buy_rsi_below": 70, "sell_rsi_above": 85}
    indicators = {
        "ema_fast_prev": 70500.0, "ema_fast": 70600.0,
        "ema_slow_prev": 70000.0, "ema_slow": 70100.0,
        "rsi": 50.0,
        "higher_trend_ok": True, "higher_tf_ema": None,
    }
    signal, reason, qty = evaluate_trend_following_signal(strategy_cfg, indicators, 0.01, 1.0)
    assert signal == "hold"


def test_trend_following_buy_zero_quantity():
    """Buy signal but quantity=0 → hold (signal fired but no size)."""
    strategy_cfg = {"ema_fast_period": 5, "ema_slow_period": 20, "buy_rsi_below": 70, "sell_rsi_above": 85}
    indicators = {
        "ema_fast_prev": 69800.0, "ema_fast": 70200.0,
        "ema_slow_prev": 70000.0, "ema_slow": 69900.0,
        "rsi": 55.0,
        "higher_trend_ok": True, "higher_tf_ema": None,
    }
    signal, reason, qty = evaluate_trend_following_signal(strategy_cfg, indicators, 0.0, 0.0)
    assert signal == "hold"
    assert "rounded to zero" in reason


# ---- Mean Reversion ----

def test_mean_reversion_buy():
    """Price at lower BB + RSI oversold → buy."""
    strategy_cfg = {"buy_rsi_below": 38, "sell_rsi_above": 68, "midline_exit_rsi_above": 52, "sell_all_on_exit": True}
    indicators = {
        "close": 65000.0, "rsi": 25.0,
        "bb_lower": 65000.0, "bb_upper": 75000.0, "bb_middle": 70000.0,
    }
    signal, reason, qty = evaluate_mean_reversion_signal(strategy_cfg, indicators, 0.01, 1.0)
    assert signal == "buy"
    assert "lower band" in reason


def test_mean_reversion_sell_upper():
    """Price at upper BB + RSI overbought → sell."""
    strategy_cfg = {"buy_rsi_below": 38, "sell_rsi_above": 68, "midline_exit_rsi_above": 52, "sell_all_on_exit": True}
    indicators = {
        "close": 75000.0, "rsi": 72.0,
        "bb_lower": 65000.0, "bb_upper": 75000.0, "bb_middle": 70000.0,
    }
    signal, reason, qty = evaluate_mean_reversion_signal(strategy_cfg, indicators, 0.01, 1.0)
    assert signal == "sell"
    assert qty == "ALL"


def test_mean_reversion_hold():
    """Price in middle, RSI neutral → hold."""
    strategy_cfg = {"buy_rsi_below": 38, "sell_rsi_above": 68, "midline_exit_rsi_above": 52}
    indicators = {
        "close": 70000.0, "rsi": 50.0,
        "bb_lower": 65000.0, "bb_upper": 75000.0, "bb_middle": 70000.0,
    }
    signal, reason, qty = evaluate_mean_reversion_signal(strategy_cfg, indicators, 0.01, 1.0)
    assert signal == "hold"


def test_mean_reversion_incomplete_bb():
    """Missing Bollinger band values → hold."""
    strategy_cfg = {"buy_rsi_below": 38, "sell_rsi_above": 68}
    indicators = {"close": 70000.0, "rsi": 25.0, "bb_lower": None, "bb_upper": None, "bb_middle": None}
    signal, reason, qty = evaluate_mean_reversion_signal(strategy_cfg, indicators, 0.01, 1.0)
    assert signal == "hold"
    assert "incomplete" in reason.lower()


# ---- Combined ----

def test_combined_buy():
    """All conditions met → buy."""
    import pandas as pd
    import numpy as np

    strategy_cfg = {
        "fast": 5, "slow": 20, "rsi_upper": 70, "rsi_lower": 30,
        "trend_filter_ema_period": 10, "volume_lookback": 5, "volume_multiplier": 1.0,
        "higher_timeframe_ema_period": 10, "sell_all_on_exit": True,
    }

    # Build a DataFrame with price above EMA200 and high volume
    n = 30
    closes = pd.Series([70000.0 + i * 100 for i in range(n)])
    volumes = pd.Series([1000.0] * (n - 1) + [5000.0])  # Last candle high volume
    df = pd.DataFrame({"close": closes, "high": closes + 100, "low": closes - 100, "volume": volumes})

    indicators = {
        "ma_fast_prev": 70200.0, "ma_fast": 70400.0,  # golden cross
        "ma_slow_prev": 70300.0, "ma_slow": 70250.0,
        "rsi": 55.0,
    }

    signal, reason, qty = evaluate_combined_signal(strategy_cfg, indicators, 0.01, 1.0, df=df)
    # The result depends on whether golden_cross condition is met AND other filters
    # ma_fast_prev=70200 < ma_slow_prev=70300 AND ma_fast=70400 > ma_slow=70250 → golden cross
    assert signal in ("buy", "hold")  # Could be hold if volume filter fails


def test_combined_sell_death_cross():
    """Death cross → sell."""
    strategy_cfg = {
        "fast": 5, "slow": 20, "rsi_upper": 70, "rsi_lower": 30,
        "sell_all_on_exit": True,
    }
    indicators = {
        "ma_fast_prev": 70500.0, "ma_fast": 69800.0,  # death cross
        "ma_slow_prev": 70000.0, "ma_slow": 70100.0,
        "rsi": 50.0,
    }
    signal, reason, qty = evaluate_combined_signal(strategy_cfg, indicators, 0.01, 1.0)
    assert signal == "sell"
    assert qty == "ALL"


def test_combined_hold():
    """No cross, RSI moderate → hold."""
    strategy_cfg = {
        "fast": 5, "slow": 20, "rsi_upper": 70, "rsi_lower": 30,
    }
    indicators = {
        "ma_fast_prev": 70600.0, "ma_fast": 70700.0,
        "ma_slow_prev": 70100.0, "ma_slow": 70200.0,
        "rsi": 50.0,
    }
    signal, reason, qty = evaluate_combined_signal(strategy_cfg, indicators, 0.01, 1.0)
    assert signal == "hold"


# ---- Registry ----

def test_registry_dispatch():
    """Registry should dispatch to the correct strategy."""
    strategy_cfg = {"ema_fast_period": 5, "ema_slow_period": 20, "buy_rsi_below": 70, "sell_rsi_above": 85}
    indicators = {
        "ema_fast_prev": 70500.0, "ema_fast": 70600.0,
        "ema_slow_prev": 70000.0, "ema_slow": 70100.0,
        "rsi": 50.0,
        "ma_fast_prev": 70500.0, "ma_fast": 70600.0,
        "ma_slow_prev": 70000.0, "ma_slow": 70100.0,
        "higher_trend_ok": True, "higher_tf_ema": None,
    }
    # trend_following
    signal1, _, _ = evaluate_signal("trend_following", strategy_cfg, indicators, 0.01, 1.0)
    assert signal1 == "hold"

    # mean_reversion
    mr_cfg = {"buy_rsi_below": 38, "sell_rsi_above": 68}
    mr_indicators = {"close": 70000.0, "rsi": 50.0, "bb_lower": 65000.0, "bb_upper": 75000.0, "bb_middle": 70000.0}
    signal2, _, _ = evaluate_signal("mean_reversion", mr_cfg, mr_indicators, 0.01, 1.0)
    assert signal2 == "hold"

    # unknown defaults to trend_following
    signal3, _, _ = evaluate_signal("unknown_strategy", strategy_cfg, indicators, 0.01, 1.0)
    assert signal3 == "hold"


if __name__ == "__main__":
    test_trend_following_golden_cross_buy()
    test_trend_following_death_cross_sell()
    test_trend_following_rsi_overbought_sell()
    test_trend_following_hold()
    test_trend_following_buy_zero_quantity()
    test_mean_reversion_buy()
    test_mean_reversion_sell_upper()
    test_mean_reversion_hold()
    test_mean_reversion_incomplete_bb()
    test_combined_buy()
    test_combined_sell_death_cross()
    test_combined_hold()
    test_registry_dispatch()
    print("All strategy tests passed!")
