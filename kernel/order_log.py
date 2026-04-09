"""
order_log.py — Idempotent order logging.

Every order intent gets a unique intent_id. The order lifecycle is:
  intent → risk_checked → approved → submitted → filled

Each transition is recorded as a separate event in the decision log.
Duplicate transitions for the same intent_id are safely ignored.

This module provides:
- OrderEvent data class for structured logging
- Helper to build events from OrderIntent / RiskCheckResult
- Helper to check for duplicate intent_ids
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class OrderEvent:
    """
    Structured record of an order lifecycle transition.

    - intent_id: links back to the OrderIntent that originated this order
    - event_type: "intent" | "risk_checked" | "approved" | "rejected" | "submitted" | "filled" | "blocked" | "error"
    - timestamp: ISO 8601
    - symbol, side, quantity, price: core order details
    - details: additional context (risk reasons, fill info, etc.)
    """

    intent_id: str
    event_type: str
    timestamp: str
    symbol: str
    side: str
    quantity: float = 0.0
    price: float = 0.0
    strategy_name: str = ""
    market: str = ""
    reason: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_jsonl(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)


def build_intent_event(
    intent_id: str,
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    strategy_name: str,
    market: str,
    reason: str,
    indicators: Optional[Dict[str, Any]] = None,
    position_scale: float = 1.0,
) -> OrderEvent:
    """Build an event for the initial intent generation."""
    return OrderEvent(
        intent_id=intent_id,
        event_type="intent",
        timestamp=datetime.utcnow().isoformat(),
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        strategy_name=strategy_name,
        market=market,
        reason=reason,
        details={
            "indicators": indicators or {},
            "position_scale": position_scale,
        },
    )


def build_risk_event(
    intent_id: str,
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    allowed: bool,
    reasons: List[str],
    metrics: Optional[Dict[str, Any]] = None,
) -> OrderEvent:
    """Build an event for the risk check result."""
    return OrderEvent(
        intent_id=intent_id,
        event_type="risk_checked" if allowed else "blocked",
        timestamp=datetime.utcnow().isoformat(),
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        reason="Risk check passed" if allowed else "Risk check blocked: " + "; ".join(reasons),
        details={
            "allowed": allowed,
            "risk_reasons": reasons,
            "risk_metrics": metrics or {},
        },
    )


def build_execution_event(
    intent_id: str,
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    status: str,  # "approved" | "rejected" | "submitted" | "filled" | "error"
    reason: str = "",
    fill_price: Optional[float] = None,
    fill_quantity: Optional[float] = None,
    order_id: Optional[str] = None,
    error: Optional[str] = None,
) -> OrderEvent:
    """Build an event for the execution result."""
    details: Dict[str, Any] = {}
    if fill_price is not None:
        details["fill_price"] = fill_price
    if fill_quantity is not None:
        details["fill_quantity"] = fill_quantity
    if order_id is not None:
        details["order_id"] = order_id
    if error is not None:
        details["error"] = error

    return OrderEvent(
        intent_id=intent_id,
        event_type=status,
        timestamp=datetime.utcnow().isoformat(),
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        reason=reason,
        details=details,
    )


def intent_id_exists(intent_id: str, log_path: Path) -> bool:
    """
    Check if an intent_id already exists in the decision log.

    Used for idempotency: if the same intent_id was already logged,
    we skip re-logging to avoid duplicates.
    """
    if not log_path.exists():
        return False
    try:
        with log_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    if event.get("intent_id") == intent_id:
                        return True
                except (json.JSONDecodeError, KeyError):
                    continue
    except Exception:
        return False
    return False
