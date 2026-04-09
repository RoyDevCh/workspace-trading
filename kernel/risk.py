"""
risk.py — Pure risk checking functions.

check_risk() takes an OrderIntent + risk parameters and returns
a RiskCheckResult. No I/O, no network calls, no state mutation.

The caller is responsible for:
1. Fetching account balance/positions from the executor
2. Loading the current trading state
3. Passing all required parameters to check_risk()

This separation ensures risk logic is fully deterministic and testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class RiskCheckResult:
    """
    Immutable result of a risk check.

    - allowed: True if the order passes all risk checks
    - reasons: List of specific risk rule violations (empty if allowed)
    - metrics: Computed risk metrics for audit/logging
    """

    allowed: bool
    reasons: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "metrics": dict(self.metrics),
        }


def check_risk(
    side: str,
    market: str,
    price: float,
    quantity: float,
    equity: float,
    cash: float,
    market_value: float,
    leverage: int = 1,
    stop_price: Optional[float] = None,
    estimated_loss: Optional[float] = None,
    committed_daily_risk: float = 0.0,
    futures_trades_today: int = 0,
    # Risk parameters from config
    max_order_value_usdt: float = 50000.0,
    max_position_value_usdt: float = 100000.0,
    max_single_loss_pct: float = 0.02,
    max_daily_loss_pct: float = 0.05,
    max_position_pct: float = 1.0,
    min_cash_ratio: float = 0.10,
    # Futures-specific
    futures_enabled: bool = True,
    futures_max_leverage: int = 5,
    futures_max_position_pct: float = 0.05,
    futures_max_daily_loss_pct: float = 0.05,
    futures_mandatory_stop_loss_pct: float = 0.02,
    futures_require_stop_price: bool = True,
    futures_max_trades_per_day: int = 8,
    # Session / pause checks (passed in by caller)
    is_paused: bool = False,
    pause_until: Optional[str] = None,
    in_blackout: bool = False,
    cn_equity_session_allowed: bool = True,
    cn_equity_session_reason: str = "",
) -> RiskCheckResult:
    """
    Pure risk check — no I/O, no side effects.

    All state (balance, positions, daily risk) must be passed in by the caller.
    This function only evaluates rules and returns a deterministic result.

    Returns RiskCheckResult with:
    - allowed=True if ALL risk checks pass
    - reasons=[] if allowed, or list of specific violations
    - metrics dict with computed values for audit
    """
    reasons: List[str] = []
    order_value = price * quantity
    capital_required = order_value if market != "futures" else (order_value / max(float(leverage), 1.0))

    # Compute estimated_loss if not provided
    if estimated_loss is None:
        if stop_price is not None:
            estimated_loss = abs(price - float(stop_price)) * quantity
        else:
            default_stop_pct = futures_mandatory_stop_loss_pct if market == "futures" else 0.02
            estimated_loss = price * quantity * default_stop_pct

    # Session / pause checks
    if is_paused:
        reasons.append(f"Trading is paused until {pause_until}")
    if in_blackout:
        reasons.append("Current market window is blocked by restrictions")
    if market == "cn_equity" and not cn_equity_session_allowed:
        reasons.append(cn_equity_session_reason or "China A-share market is outside trading hours")

    # Quantity check
    if quantity <= 0:
        reasons.append("Resolved quantity is zero after market lot-size normalization")

    # Early return for fundamental blocks
    if reasons:
        return RiskCheckResult(
            allowed=False,
            reasons=reasons,
            metrics={
                "market": market,
                "equity": equity,
                "cash": cash,
                "order_value": order_value,
                "capital_required": capital_required,
                "estimated_loss": estimated_loss,
                "leverage": leverage,
            },
        )

    # Order value check
    if order_value > max_order_value_usdt:
        reasons.append("Order value exceeds max_order_value_usdt")

    # Futures-specific checks
    if market == "futures":
        if not futures_enabled:
            reasons.append("Futures trading is disabled in trading.futures.enabled")
        if leverage > futures_max_leverage:
            reasons.append("Requested leverage exceeds trading.futures.max_leverage")
        if futures_require_stop_price and stop_price is None and estimated_loss is None:
            reasons.append("Futures orders require stop_price or estimated_loss to enforce mandatory_stop_loss")
        if futures_max_position_pct > 0 and capital_required > equity * futures_max_position_pct:
            reasons.append("Futures initial margin exceeds trading.futures.max_position_pct budget")
        if futures_max_trades_per_day > 0 and futures_trades_today >= futures_max_trades_per_day:
            reasons.append("Futures daily trade count would exceed trading.futures.max_trades_per_day")

    # Position budget check (non-futures)
    if market != "futures" and max_position_pct > 0 and order_value > equity * max_position_pct:
        reasons.append("Order value exceeds trading.max_position_pct budget")

    # Single trade loss check
    if estimated_loss > equity * max_single_loss_pct:
        reasons.append("Estimated single-order loss exceeds max_single_loss_pct budget")

    # Daily risk budget check
    daily_loss_pct = futures_max_daily_loss_pct if market == "futures" else max_daily_loss_pct
    if committed_daily_risk + estimated_loss > equity * daily_loss_pct:
        reasons.append(
            "Futures daily risk budget exceeded"
            if market == "futures"
            else "Daily risk budget would exceed max_daily_loss_pct"
        )

    # Position value check
    if side == "buy" and (market_value + order_value) > max_position_value_usdt:
        reasons.append("Position value would exceed max_position_value_usdt")

    # Cash reserve check
    if market == "futures" and cash > 0 and (cash - capital_required) / equity < min_cash_ratio:
        reasons.append("Available collateral would fall below min_cash_ratio")
    if market != "futures" and side == "buy" and cash > 0 and (cash - order_value) / equity < min_cash_ratio:
        reasons.append("Cash reserve would fall below min_cash_ratio")

    return RiskCheckResult(
        allowed=not reasons,
        reasons=reasons,
        metrics={
            "market": market,
            "equity": equity,
            "cash": cash,
            "market_value": market_value,
            "order_value": order_value,
            "capital_required": capital_required,
            "estimated_loss": estimated_loss,
            "committed_daily_risk": committed_daily_risk,
            "leverage": leverage,
            "futures_trades_today": futures_trades_today,
        },
    )
