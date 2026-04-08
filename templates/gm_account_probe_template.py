from gm.api import *
from gm.model.storage import context

import json
import time


ACCOUNT_ID = {{account_id|py}}
SYMBOL = {{symbol|py}}
POLL_ATTEMPTS = int({{poll_attempts}})
POLL_INTERVAL_SEC = float({{poll_interval_sec}})

if ACCOUNT_ID:
    set_account_id(ACCOUNT_ID)

_captured = False


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
        if not SYMBOL or str(row.get("symbol") or "") == SYMBOL:
            filtered.append(row)
    return filtered


def _capture_snapshot():
    global _captured
    if _captured:
        return

    payload = {"account_id": ACCOUNT_ID, "symbol": SYMBOL, "attempts": []}

    for attempt in range(max(POLL_ATTEMPTS, 1)):
        snapshot = {"attempt": attempt + 1}

        try:
            snapshot["cash"] = get_cash(account_id=ACCOUNT_ID or None)
        except Exception as exc:
            snapshot["cash_error"] = str(exc)

        try:
            snapshot["context_accounts"] = list((context.accounts or {}).keys())
            account = context.account(ACCOUNT_ID or "")
            snapshot["context_account_exists"] = bool(account)
            snapshot["context_cash"] = getattr(account, "cash", None) if account else None
            snapshot["context_positions"] = account.positions(symbol=SYMBOL) if account else []
        except Exception as exc:
            snapshot["context_error"] = str(exc)

        try:
            positions = get_position(account_id=ACCOUNT_ID or None)
            snapshot["positions"] = _filter_symbol(_rows(positions))
        except Exception as exc:
            snapshot["positions_error"] = str(exc)

        try:
            orders = get_orders()
            snapshot["orders"] = _filter_symbol(_rows(orders))
        except Exception as exc:
            snapshot["orders_error"] = str(exc)

        try:
            unfinished = get_unfinished_orders()
            snapshot["unfinished_orders"] = _filter_symbol(_rows(unfinished))
        except Exception as exc:
            snapshot["unfinished_orders_error"] = str(exc)

        payload["attempts"].append(snapshot)

        if (
            snapshot.get("cash")
            or snapshot.get("context_account_exists")
            or snapshot.get("positions")
            or snapshot.get("orders")
            or snapshot.get("unfinished_orders")
        ):
            break

        if attempt + 1 < max(POLL_ATTEMPTS, 1) and POLL_INTERVAL_SEC > 0:
            time.sleep(POLL_INTERVAL_SEC)

    latest = payload["attempts"][-1] if payload["attempts"] else {}
    payload.update(latest)

    _captured = True
    _emit("OPENCLAW_ACCOUNT_PROBE", payload)
    stop()


def init(context):
    _emit("OPENCLAW_ACCOUNT_PROBE_INIT", {"account_id": ACCOUNT_ID, "symbol": SYMBOL})


def on_trade_data_connected(context):
    _emit("OPENCLAW_ACCOUNT_PROBE_TRADE_CONNECTED", {"account_id": ACCOUNT_ID})
    _capture_snapshot()


def on_account_status(context, account):
    _emit("OPENCLAW_ACCOUNT_PROBE_ACCOUNT_STATUS", account)
    _capture_snapshot()


def on_error(context, code, info):
    _emit("OPENCLAW_ACCOUNT_PROBE_ERROR", {"code": code, "info": info})
