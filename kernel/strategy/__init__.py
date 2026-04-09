"""
strategy — Pure strategy evaluation functions.

Each strategy function takes:
- strategy_cfg: strategy parameters (from trading_rules.yaml)
- indicators: computed indicator values
- quantity: base order quantity
- scale: position scale factor

And returns: (signal, reason, quantity)
  signal: "buy" | "sell" | "hold"
  reason: human-readable explanation
  quantity: float or "ALL" for full exit

No I/O, no network calls, no state access.
"""

from .registry import evaluate_signal
from .trend_following import evaluate_trend_following_signal
from .mean_reversion import evaluate_mean_reversion_signal
from .combined import evaluate_combined_signal

__all__ = [
    "evaluate_signal",
    "evaluate_trend_following_signal",
    "evaluate_mean_reversion_signal",
    "evaluate_combined_signal",
]
