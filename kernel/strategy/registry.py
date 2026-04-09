"""
registry.py — Strategy dispatch.

Given a strategy kind and parameters, dispatches to the correct
evaluation function. Pure function, no I/O.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import pandas as pd

from .trend_following import evaluate_trend_following_signal
from .mean_reversion import evaluate_mean_reversion_signal
from .combined import evaluate_combined_signal

# Type alias for signal result
SignalResult = Tuple[str, str, "float | str"]


def evaluate_signal(
    strategy_kind: str,
    strategy_cfg: Dict[str, Any],
    indicators: Dict[str, Any],
    quantity: float,
    scale: float,
    df: Optional[pd.DataFrame] = None,
    higher_tf_df: Optional[pd.DataFrame] = None,
    market: str = "",
) -> SignalResult:
    """
    Dispatch to the correct strategy evaluation function.

    Returns (signal, reason, quantity):
      signal: "buy" | "sell" | "hold"
      reason: human-readable explanation
      quantity: float or "ALL" for full exit
    """
    kind = str(strategy_kind or "trend_following").strip().lower()

    if kind == "mean_reversion":
        return evaluate_mean_reversion_signal(strategy_cfg, indicators, quantity, scale)
    elif kind == "combined":
        return evaluate_combined_signal(
            strategy_cfg, indicators, quantity, scale,
            df=df, higher_tf_df=higher_tf_df,
        )
    else:
        # Default: trend_following
        return evaluate_trend_following_signal(
            strategy_cfg, indicators, quantity, scale,
            df=df, higher_tf_df=higher_tf_df, market=market,
        )
