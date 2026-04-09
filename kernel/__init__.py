"""
kernel — Deterministic trading kernel for OpenClaw.

This package contains ONLY pure functions and data classes.
No I/O, no network calls, no file access, no agent context.

Every function in this package is:
- Deterministic: same inputs → same outputs
- Testable: no external dependencies needed
- Stateless: no global mutable state
"""

from .decision import OrderIntent, SignalDecision
from .risk import RiskCheckResult, check_risk
from .strategy.registry import evaluate_signal

__all__ = [
    "OrderIntent",
    "SignalDecision",
    "RiskCheckResult",
    "check_risk",
    "evaluate_signal",
]
