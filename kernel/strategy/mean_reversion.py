"""
mean_reversion.py — Mean-reversion strategy evaluation.

Pure function: Bollinger Band touch + RSI filter.

Buy:  Price touches lower BB + RSI oversold
Sell: Price touches upper BB + RSI overbought, OR price recovers above midline + RSI confirmation
Hold: None of the above
"""

from __future__ import annotations

from typing import Any, Dict, Tuple


def _safe_float(value: Any) -> "float | None":
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def evaluate_mean_reversion_signal(
    strategy_cfg: Dict[str, Any],
    indicators: Dict[str, Any],
    quantity: float,
    scale: float,
) -> Tuple[str, str, "float | str"]:
    """
    Evaluate mean-reversion signal.

    Parameters:
    - strategy_cfg: must contain buy_rsi_below, sell_rsi_above, midline_exit_rsi_above
    - indicators: must contain close, rsi, bb_lower, bb_upper, bb_middle
    - quantity: base order quantity (already scaled)
    - scale: position scale factor (for logging)

    Returns: (signal, reason, quantity)
    """
    close = float(indicators["close"])
    rsi_value = float(indicators["rsi"]) if indicators.get("rsi") is not None else 50.0
    bb_lower = _safe_float(indicators.get("bb_lower"))
    bb_upper = _safe_float(indicators.get("bb_upper"))
    bb_middle = _safe_float(indicators.get("bb_middle"))

    if bb_lower is None or bb_upper is None or bb_middle is None:
        return ("hold", "Mean-reversion signal skipped because Bollinger bands are incomplete", quantity)

    buy_rsi_below = float(strategy_cfg.get("buy_rsi_below", 38))
    sell_rsi_above = float(strategy_cfg.get("sell_rsi_above", 68))
    midline_exit_rsi_above = float(strategy_cfg.get("midline_exit_rsi_above", 52))
    touched_lower = close <= bb_lower
    touched_upper = close >= bb_upper
    crossed_midline = bool(strategy_cfg.get("exit_on_midline", True)) and close >= bb_middle

    if touched_lower and rsi_value <= buy_rsi_below:
        if float(quantity) <= 0:
            return (
                "hold",
                f"Mean-reversion buy signal fired but scaled quantity rounded to zero after macro position scale {scale:.2f}",
                quantity,
            )
        return (
            "buy",
            f"Mean-reversion entry: close {close:.2f} touched lower band {bb_lower:.2f} and RSI {rsi_value:.2f} <= {buy_rsi_below:.2f}",
            quantity,
        )

    if touched_upper and rsi_value >= sell_rsi_above:
        if strategy_cfg.get("sell_all_on_exit", True):
            quantity = "ALL"
        return (
            "sell",
            f"Mean-reversion exit: close {close:.2f} reached upper band {bb_upper:.2f} and RSI {rsi_value:.2f} >= {sell_rsi_above:.2f}",
            quantity,
        )

    if crossed_midline and rsi_value >= midline_exit_rsi_above:
        if strategy_cfg.get("sell_all_on_exit", True):
            quantity = "ALL"
        return (
            "sell",
            f"Mean-reversion take-profit: close {close:.2f} recovered above middle band {bb_middle:.2f} and RSI {rsi_value:.2f} >= {midline_exit_rsi_above:.2f}",
            quantity,
        )

    return (
        "hold",
        f"No configured rule fired; close {close:.2f}, BB[{bb_lower:.2f}, {bb_middle:.2f}, {bb_upper:.2f}], RSI {rsi_value:.2f}",
        quantity,
    )
