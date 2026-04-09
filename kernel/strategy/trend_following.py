"""
trend_following.py — Trend-following strategy evaluation.

Pure function: EMA crossover + RSI filter + optional higher timeframe filter.

Buy:  EMA fast crosses above EMA slow + RSI below threshold + higher TF aligned
Sell: EMA fast crosses below EMA slow OR RSI above threshold
Hold: None of the above
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import pandas as pd


def evaluate_trend_following_signal(
    strategy_cfg: Dict[str, Any],
    indicators: Dict[str, Any],
    quantity: float,
    scale: float,
    df: Optional[pd.DataFrame] = None,
    higher_tf_df: Optional[pd.DataFrame] = None,
    market: str = "",
) -> Tuple[str, str, "float | str"]:
    """
    Evaluate trend-following signal.

    Parameters:
    - strategy_cfg: must contain ema_fast_period, ema_slow_period, buy_rsi_below, sell_rsi_above
    - indicators: must contain ema_fast, ema_fast_prev, ema_slow, ema_slow_prev, rsi
    - quantity: base order quantity (already scaled)
    - scale: position scale factor (for logging)
    - df: OHLCV DataFrame (for higher timeframe filter)
    - higher_tf_df: higher timeframe DataFrame (optional)
    - market: market identifier

    Returns: (signal, reason, quantity)
    """
    fast_prev = float(indicators["ema_fast_prev"])
    fast_now = float(indicators["ema_fast"])
    slow_prev = float(indicators["ema_slow_prev"])
    slow_now = float(indicators["ema_slow"])
    rsi_value = float(indicators["rsi"]) if indicators.get("rsi") is not None else 50.0
    buy_rsi_below = float(strategy_cfg.get("buy_rsi_below", 70))
    sell_rsi_above = float(strategy_cfg.get("sell_rsi_above", 85))

    golden_cross = fast_prev <= slow_prev and fast_now > slow_now
    death_cross = fast_prev >= slow_prev and fast_now < slow_now

    # Higher timeframe filter (A-share or cn_equity)
    higher_trend_ok = True
    higher_tf_ema = None
    use_higher_tf_filter = market == "cn_equity" and bool(strategy_cfg.get("use_higher_timeframe_filter", False))
    higher_tf_period = max(int(strategy_cfg.get("higher_timeframe_ema_period", 13) or 13), 1)

    if use_higher_tf_filter and higher_tf_df is not None and not higher_tf_df.empty and len(higher_tf_df) >= higher_tf_period:
        higher_close = pd.to_numeric(higher_tf_df["close"], errors="coerce")
        higher_ema_series = higher_close.ewm(span=higher_tf_period, adjust=False).mean()
        higher_tf_ema = float(higher_ema_series.iloc[-1])
        higher_trend_ok = float(higher_close.iloc[-1]) > higher_tf_ema

    # Mutate indicators dict for downstream consumers (maintains compatibility)
    indicators["higher_trend_ok"] = higher_trend_ok
    indicators["higher_tf_ema"] = higher_tf_ema

    if golden_cross and rsi_value < buy_rsi_below and higher_trend_ok:
        if float(quantity) <= 0:
            return (
                "hold",
                f"Trend-following buy signal fired but scaled quantity rounded to zero after macro position scale {scale:.2f}",
                quantity,
            )
        return (
            "buy",
            f"Trend-following entry: EMA{strategy_cfg['ema_fast_period']} crossed above EMA{strategy_cfg['ema_slow_period']}, RSI {rsi_value:.2f} < {buy_rsi_below:.2f}, higher timeframe aligned",
            quantity,
        )

    if death_cross or rsi_value > sell_rsi_above:
        if strategy_cfg.get("sell_all_on_exit", True):
            quantity = "ALL"
        return (
            "sell",
            f"Trend-following exit: death_cross={death_cross} or RSI {rsi_value:.2f} > {sell_rsi_above:.2f}",
            quantity,
        )

    return (
        "hold",
        f"No configured rule fired; EMA{strategy_cfg['ema_fast_period']}={indicators['ema_fast']:.2f}, EMA{strategy_cfg['ema_slow_period']}={indicators['ema_slow']:.2f}, RSI {rsi_value:.2f}, higher timeframe ok={higher_trend_ok}",
        quantity,
    )
