from gm.api import *

import json
import time


ACCOUNT_ID = {{account_id|py}}
SYMBOL = {{symbol|py}}
SIDE = str({{side|py}}).strip().lower()
ORDER_VOLUME = int({{order_volume}})
ORDER_TYPE_NAME = str({{order_type|py}}).strip().lower()
POST_SUBMIT_SLEEP_SEC = float({{post_submit_sleep_sec}})
ALLOW_SELL_WITHOUT_POSITION = bool({{allow_sell_without_position|py}})

if ACCOUNT_ID:
    set_account_id(ACCOUNT_ID)

_submitted = False
_post_submit_snapshot_done = False


def _json_safe(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return repr(value)


def _emit(name, payload):
    print(f"{name}=" + json.dumps(_json_safe(payload), ensure_ascii=False), flush=True)


def _position_volume():
    try:
        positions = get_position(account_id=ACCOUNT_ID or None)
    except Exception as exc:
        _emit("OPENCLAW_POSITION_ERROR", {"error": str(exc)})
        return 0

    _emit("OPENCLAW_POSITIONS", positions)

    if not positions:
        return 0
    if isinstance(positions, dict):
        rows = positions.get("data") or positions.get("positions") or [positions]
    else:
        rows = positions
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol") or "") != SYMBOL:
            continue
        side = row.get("side")
        if side not in (None, 1, "1", "long", "Long"):
            continue
        for key in (
            "available_now",
            "credit_position_sellable_volume",
            "can_sell_volume",
            "available",
            "available_today",
            "volume",
        ):
            value = row.get(key)
            if value in (None, ""):
                continue
            try:
                return int(float(value))
            except Exception:
                continue
    return 0


def _rows(value):
    if value is None:
        return []
    if isinstance(value, dict):
        return value.get("data") or value.get("orders") or value.get("positions") or [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _filter_symbol(rows):
    filtered = []
    for row in rows:
        if not isinstance(row, dict):
            filtered.append(row)
            continue
        if str(row.get("symbol") or "") == SYMBOL:
            filtered.append(row)
    return filtered


def _emit_post_submit_snapshot():
    global _post_submit_snapshot_done
    if _post_submit_snapshot_done:
        return

    payload = {"account_id": ACCOUNT_ID, "symbol": SYMBOL}

    try:
        payload["cash"] = get_cash(account_id=ACCOUNT_ID or None)
    except Exception as exc:
        payload["cash_error"] = str(exc)

    try:
        positions = get_position(account_id=ACCOUNT_ID or None)
        payload["positions"] = _filter_symbol(_rows(positions))
    except Exception as exc:
        payload["positions_error"] = str(exc)

    try:
        payload["orders"] = _filter_symbol(_rows(get_orders()))
    except Exception as exc:
        payload["orders_error"] = str(exc)

    try:
        payload["unfinished_orders"] = _filter_symbol(_rows(get_unfinished_orders()))
    except Exception as exc:
        payload["unfinished_orders_error"] = str(exc)

    _post_submit_snapshot_done = True
    _emit("OPENCLAW_POST_SUBMIT_SNAPSHOT", payload)
    stop()


def _submit_order():
    global _submitted
    if _submitted:
        return

    try:
        cash = get_cash(account_id=ACCOUNT_ID or None)
        _emit("OPENCLAW_CASH", cash)
    except Exception as exc:
        _emit("OPENCLAW_CASH_ERROR", {"error": str(exc)})

    side_value = OrderSide_Buy if SIDE == "buy" else OrderSide_Sell
    effect = PositionEffect_Open if SIDE == "buy" else PositionEffect_Close
    order_type = OrderType_Market if ORDER_TYPE_NAME == "market" else OrderType_Limit
    volume = ORDER_VOLUME
    if SIDE == "sell":
        available_volume = _position_volume()
        if not ALLOW_SELL_WITHOUT_POSITION:
            volume = min(volume, available_volume)
    else:
        _position_volume()

    if volume <= 0:
        _emit(
            "OPENCLAW_ORDER_SKIPPED",
            {
                "reason": "resolved order volume is zero",
                "account_id": ACCOUNT_ID,
                "symbol": SYMBOL,
                "side": SIDE,
            },
        )
        stop()
        return

    try:
        order = order_volume(
            symbol=SYMBOL,
            volume=volume,
            side=side_value,
            order_type=order_type,
            position_effect=effect,
            account=ACCOUNT_ID or "",
        )
    except Exception as exc:
        _emit(
            "OPENCLAW_ORDER_SUBMIT_ERROR",
            {
                "account_id": ACCOUNT_ID,
                "symbol": SYMBOL,
                "side": SIDE,
                "submitted_volume": volume,
                "error": str(exc),
            },
        )
        return

    _submitted = True
    _emit(
        "OPENCLAW_ORDER_SUBMITTED",
        {
            "account_id": ACCOUNT_ID,
            "symbol": SYMBOL,
            "side": SIDE,
            "requested_volume": ORDER_VOLUME,
            "submitted_volume": volume,
            "response": order,
        },
    )
    if POST_SUBMIT_SLEEP_SEC > 0:
        time.sleep(POST_SUBMIT_SLEEP_SEC)
    _emit_post_submit_snapshot()


def init(context):
    _emit(
        "OPENCLAW_INIT",
        {
            "account_id": ACCOUNT_ID,
            "symbol": SYMBOL,
            "side": SIDE,
            "order_volume": ORDER_VOLUME,
            "order_type": ORDER_TYPE_NAME,
        },
    )
    _submit_order()


def on_trade_data_connected(context):
    _emit("OPENCLAW_TRADE_CONNECTED", {"account_id": ACCOUNT_ID})
    _submit_order()


def on_order_status(context, order):
    _emit("OPENCLAW_ORDER_STATUS", order)
    _emit_post_submit_snapshot()


def on_execution_report(context, report):
    _emit("OPENCLAW_EXECUTION_REPORT", report)
    _emit_post_submit_snapshot()


def on_account_status(context, account):
    _emit("OPENCLAW_ACCOUNT_STATUS", account)
    _submit_order()


def on_error(context, code, info):
    _emit("OPENCLAW_ERROR", {"code": code, "info": info})
