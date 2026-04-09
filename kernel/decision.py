"""
decision.py — OrderIntent data class and signal decision types.

The OrderIntent is the ONLY way a trading signal flows from
strategy evaluation to risk checking to order execution.
No other path is allowed.

Design principle: every field is required and explicit.
No implicit state, no "payload" dicts, no optional side channels.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class OrderIntent:
    """
    Immutable representation of a trading signal that has NOT yet been risk-checked.

    This is the output of strategy evaluation and the input to risk checking.
    It captures WHAT the strategy wants to do, not whether it SHOULD be done.

    Field semantics:
    - symbol: e.g. "BTC/USDT", "510300"
    - side: "buy" | "sell" | "hold"
    - quantity: float (0.0 for hold), or "ALL" for full position exit
    - price: current market price at signal generation time
    - stop_loss: ATR-based stop loss price (None if not applicable)
    - strategy_name: e.g. "combined", "trend_following"
    - signal_strength: "strong" | "moderate" | "weak" (for logging, never affects execution)
    - reason: human-readable explanation of why this signal was generated
    - timestamp: ISO 8601 UTC timestamp of when this intent was created
    - intent_id: unique identifier for idempotency tracking
    - market: "crypto" | "futures" | "cn_equity"
    - indicators: snapshot of indicators that triggered this signal (for audit)
    - position_scale: the position scale applied at signal time
    """

    symbol: str
    side: str  # "buy" | "sell" | "hold"
    quantity: float  # 0.0 for hold, positive for buy/sell
    price: float
    stop_loss: Optional[float]
    strategy_name: str
    signal_strength: str  # "strong" | "moderate" | "weak"
    reason: str
    timestamp: str  # ISO 8601
    intent_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    market: str = ""
    indicators: Dict[str, Any] = field(default_factory=dict)
    position_scale: float = 1.0

    def is_hold(self) -> bool:
        return self.side == "hold" or self.quantity <= 0

    def is_actionable(self) -> bool:
        return self.side in ("buy", "sell") and (self.quantity > 0 or str(self.quantity).upper() == "ALL")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def hold(
        cls,
        symbol: str,
        price: float,
        reason: str,
        market: str = "",
        strategy_name: str = "unknown",
        indicators: Optional[Dict[str, Any]] = None,
        position_scale: float = 1.0,
        timestamp: Optional[str] = None,
    ) -> OrderIntent:
        """Convenience constructor for HOLD signals."""
        return cls(
            symbol=symbol,
            side="hold",
            quantity=0.0,
            price=price,
            stop_loss=None,
            strategy_name=strategy_name,
            signal_strength="none",
            reason=reason,
            timestamp=timestamp or datetime.utcnow().isoformat(),
            market=market,
            indicators=indicators or {},
            position_scale=position_scale,
        )


@dataclass(frozen=True)
class SignalDecision:
    """
    Result of signal evaluation for a single symbol.

    Wraps an OrderIntent with additional metadata about the evaluation context.
    """

    intent: OrderIntent
    execution_allowed: bool
    configured_execution_allowed: bool
    market_session: Dict[str, Any] = field(default_factory=dict)
    execution_block_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        result = self.intent.to_dict()
        result["execution_allowed"] = self.execution_allowed
        result["configured_execution_allowed"] = self.configured_execution_allowed
        result["market_session"] = self.market_session
        result["execution_block_reason"] = self.execution_block_reason
        return result
