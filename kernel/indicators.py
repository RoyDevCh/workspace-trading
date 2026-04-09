"""
indicators.py — Pure technical indicator calculations.

All functions are pure: they take a pandas DataFrame or Series
and return computed values. No I/O, no side effects.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd


def ema_series(closes: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average."""
    return closes.ewm(span=max(int(period), 1), adjust=False).mean()


def sma_series(closes: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average."""
    return closes.rolling(window=max(int(period), 1), min_periods=1).mean()


def rsi_series(closes: pd.Series, period: int) -> pd.Series:
    """Relative Strength Index."""
    period = max(int(period), 2)
    delta = closes.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("inf"))
    return 100.0 - (100.0 / (1.0 + rs))


def macd_bundle(closes: pd.Series) -> Dict[str, float]:
    """MACD line, signal line, and histogram."""
    ema12 = closes.ewm(span=12, adjust=False).mean()
    ema26 = closes.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    hist = macd_line - signal_line
    try:
        return {
            "macd": float(macd_line.iloc[-1]),
            "macd_signal": float(signal_line.iloc[-1]),
            "macd_hist": float(hist.iloc[-1]),
        }
    except (IndexError, ValueError):
        return {"macd": 0.0, "macd_signal": 0.0, "macd_hist": 0.0}


def bollinger_bundle(closes: pd.Series, period: int, stddev: float) -> Dict[str, Any]:
    """Bollinger Bands (middle, upper, lower)."""
    period = max(int(period), 2)
    stddev = max(float(stddev), 0.1)
    middle = closes.rolling(window=period, min_periods=period).mean()
    std = closes.rolling(window=period, min_periods=period).std()
    upper = middle + std * stddev
    lower = middle - std * stddev
    try:
        return {
            "bb_middle": float(middle.iloc[-1]) if not pd.isna(middle.iloc[-1]) else None,
            "bb_upper": float(upper.iloc[-1]) if not pd.isna(upper.iloc[-1]) else None,
            "bb_lower": float(lower.iloc[-1]) if not pd.isna(lower.iloc[-1]) else None,
        }
    except (IndexError, ValueError):
        return {"bb_middle": None, "bb_upper": None, "bb_lower": None}


def atr_series(df: pd.DataFrame, period: int) -> pd.Series:
    """Average True Range."""
    if df is None or df.empty:
        return pd.Series(dtype=float)
    window = max(int(period or 14), 1)
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(window, min_periods=window).mean()


def latest_atr_value(df: pd.DataFrame, period: int) -> Optional[float]:
    """Latest ATR value, or None if unavailable."""
    try:
        value = float(atr_series(df, period).iloc[-1])
    except Exception:
        return None
    if pd.isna(value):
        return None
    return value


def calculate_indicator_bundle(
    df: pd.DataFrame,
    strategy: Dict[str, Any],
    calc_params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Calculate all indicators needed for signal evaluation.

    Pure function: takes OHLCV DataFrame + strategy params,
    returns indicator bundle dict.

    Parameters:
    - df: OHLCV DataFrame with 'close', 'high', 'low', 'volume' columns
    - strategy: strategy config dict with period parameters
    - calc_params: optional override params (fast, slow, rsi_period, bb_period, bb_stddev)

    Returns: dict with all indicator values
    """
    calc_params = calc_params or {}
    fast_period = int(
        calc_params.get(
            "fast",
            calc_params.get(
                "sma_fast_period",
                calc_params.get("ema_fast_period", strategy.get("fast", strategy.get("sma_fast_period", strategy.get("ema_fast_period", 5)))),
            ),
        )
    )
    slow_period = int(
        calc_params.get(
            "slow",
            calc_params.get(
                "sma_slow_period",
                calc_params.get("ema_slow_period", strategy.get("slow", strategy.get("sma_slow_period", strategy.get("ema_slow_period", 20)))),
            ),
        )
    )
    rsi_period = int(calc_params.get("rsi_period", strategy.get("rsi_period", 14)))
    bb_period = int(calc_params.get("bb_period", strategy.get("bb_period", 20)))
    bb_stddev = float(calc_params.get("bb_stddev", strategy.get("bb_stddev", strategy.get("bb_std", 2.0))))

    closes = df["close"]
    fast_ema = ema_series(closes, fast_period)
    slow_ema = ema_series(closes, slow_period)
    fast_sma = sma_series(closes, fast_period)
    slow_sma = sma_series(closes, slow_period)
    rsi = rsi_series(closes, rsi_period)
    bb = bollinger_bundle(closes, bb_period, bb_stddev)

    bundle = {
        "strategy_name": str(strategy.get("name") or "unknown"),
        "strategy_kind": str(strategy.get("kind") or "trend_following"),
        "ema_fast_period": fast_period,
        "ema_slow_period": slow_period,
        "rsi_period": rsi_period,
        "ema_fast": float(fast_ema.iloc[-1]),
        "ema_fast_prev": float(fast_ema.iloc[-2]) if len(fast_ema) > 1 else float(fast_ema.iloc[-1]),
        "ema_slow": float(slow_ema.iloc[-1]),
        "ema_slow_prev": float(slow_ema.iloc[-2]) if len(slow_ema) > 1 else float(slow_ema.iloc[-1]),
        "ma_fast": float(fast_sma.iloc[-1]) if not pd.isna(fast_sma.iloc[-1]) else float(fast_ema.iloc[-1]),
        "ma_fast_prev": float(fast_sma.iloc[-2]) if len(fast_sma) > 1 and not pd.isna(fast_sma.iloc[-2]) else (
            float(fast_sma.iloc[-1]) if not pd.isna(fast_sma.iloc[-1]) else float(fast_ema.iloc[-1])
        ),
        "ma_slow": float(slow_sma.iloc[-1]) if not pd.isna(slow_sma.iloc[-1]) else float(slow_ema.iloc[-1]),
        "ma_slow_prev": float(slow_sma.iloc[-2]) if len(slow_sma) > 1 and not pd.isna(slow_sma.iloc[-2]) else (
            float(slow_sma.iloc[-1]) if not pd.isna(slow_sma.iloc[-1]) else float(slow_ema.iloc[-1])
        ),
        "ma_fast_period": fast_period,
        "ma_slow_period": slow_period,
        "rsi": float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else None,
        "close": float(closes.iloc[-1]),
        "close_prev": float(closes.iloc[-2]) if len(closes) > 1 else float(closes.iloc[-1]),
    }
    bundle.update(bb)
    bundle.update(macd_bundle(closes))
    return bundle
