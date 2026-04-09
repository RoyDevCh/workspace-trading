"""
combined.py — Combined strategy evaluation.

Pure function: MA crossover + RSI + EMA200 trend filter + volume confirmation + higher TF filter.

Buy:  MA golden cross + RSI not overbought + price above EMA200 + volume confirmed + higher TF aligned
Sell: MA death cross OR RSI overbought
Hold: None of the above
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import pandas as pd


def evaluate_combined_signal(
    strategy_cfg: Dict[str, Any],
    indicators: Dict[str, Any],
    quantity: float,
    scale: float,
    df: Optional[pd.DataFrame] = None,
    higher_tf_df: Optional[pd.DataFrame] = None,
) -> Tuple[str, str, "float | str"]:
    """
    Evaluate combined strategy signal.

    Parameters:
    - strategy_cfg: must contain fast, slow, rsi_upper, rsi_lower,
                    trend_filter_ema_period, volume_lookback, volume_multiplier,
                    higher_timeframe_ema_period
    - indicators: must contain ma_fast, ma_fast_prev, ma_slow, ma_slow_prev, rsi
    - quantity: base order quantity (already scaled)
    - scale: position scale factor (for logging)
    - df: OHLCV DataFrame (for EMA200 trend filter + volume confirmation)
    - higher_tf_df: higher timeframe DataFrame (optional)

    Returns: (signal, reason, quantity)
    """
    fast_prev = float(indicators["ma_fast_prev"])
    fast_now = float(indicators["ma_fast"])
    slow_prev = float(indicators["ma_slow_prev"])
    slow_now = float(indicators["ma_slow"])
    rsi_value = float(indicators["rsi"]) if indicators.get("rsi") is not None else 50.0
    rsi_upper = float(strategy_cfg.get("rsi_upper", 70))
    rsi_lower = float(strategy_cfg.get("rsi_lower", 30))

    golden_cross = fast_prev <= slow_prev and fast_now > slow_now
    death_cross = fast_prev >= slow_prev and fast_now < slow_now

    # EMA200 trend filter
    trend_filter_period = max(int(strategy_cfg.get("trend_filter_ema_period", 200) or 200), 1)
    above_200ema = True
    ema200_value = None
    if df is not None and not df.empty and len(df) >= trend_filter_period:
        ema200 = pd.to_numeric(df["close"], errors="coerce").ewm(span=trend_filter_period, adjust=False).mean()
        ema200_value = float(ema200.iloc[-1])
        above_200ema = float(df["close"].iloc[-1]) > ema200_value

    # Volume confirmation
    volume_lookback = max(int(strategy_cfg.get("volume_lookback", 20) or 20), 1)
    volume_multiplier = float(strategy_cfg.get("volume_multiplier", 1.5) or 1.5)
    volume_ok = True
    average_volume = None
    current_volume = None
    if df is not None and not df.empty and golden_cross and len(df) >= volume_lookback:
        volume_series = pd.to_numeric(df["volume"], errors="coerce")
        average_volume = float(volume_series.tail(volume_lookback).mean())
        current_volume = float(volume_series.iloc[-1])
        volume_ok = current_volume > (average_volume * volume_multiplier)

    # Higher timeframe filter
    higher_tf_period = max(int(strategy_cfg.get("higher_timeframe_ema_period", 50) or 50), 1)
    higher_trend_ok = True
    higher_ema_value = None
    if higher_tf_df is not None and not higher_tf_df.empty and len(higher_tf_df) >= higher_tf_period:
        higher_close = pd.to_numeric(higher_tf_df["close"], errors="coerce")
        higher_ema = higher_close.ewm(span=higher_tf_period, adjust=False).mean()
        higher_ema_value = float(higher_ema.iloc[-1])
        higher_trend_ok = float(higher_close.iloc[-1]) > higher_ema_value

    # Mutate indicators dict for downstream consumers (maintains compatibility)
    indicators["trend_filter_above_ema"] = above_200ema
    indicators["trend_filter_ema_value"] = ema200_value
    indicators["volume_confirmation"] = volume_ok
    indicators["avg_volume"] = average_volume
    indicators["current_volume"] = current_volume
    indicators["higher_trend_ok"] = higher_trend_ok
    indicators["higher_tf_ema"] = higher_ema_value

    # Signal evaluation
    if golden_cross and rsi_value <= rsi_upper and above_200ema and volume_ok and higher_trend_ok:
        if float(quantity) <= 0:
            return (
                "hold",
                f"Combined-strategy buy signal fired but scaled quantity rounded to zero after position scale {scale:.2f}",
                quantity,
            )
        return (
            "buy",
            f"Enhanced combined entry: MA{strategy_cfg['fast']} crossed above MA{strategy_cfg['slow']}, RSI {rsi_value:.2f} <= {rsi_upper:.2f}, price above EMA{trend_filter_period}, volume confirmed, higher timeframe aligned",
            quantity,
        )

    if death_cross or rsi_value >= rsi_upper:
        if strategy_cfg.get("sell_all_on_exit", True):
            quantity = "ALL"
        trigger = "death_cross" if death_cross else f"RSI {rsi_value:.2f} >= {rsi_upper:.2f}"
        return ("sell", f"Enhanced combined exit: {trigger}", quantity)

    return (
        "hold",
        f"No configured rule fired; MA{strategy_cfg['fast']}={indicators['ma_fast']:.2f}, MA{strategy_cfg['slow']}={indicators['ma_slow']:.2f}, RSI {rsi_value:.2f}, oversold guard {rsi_lower:.2f}",
        quantity,
    )
