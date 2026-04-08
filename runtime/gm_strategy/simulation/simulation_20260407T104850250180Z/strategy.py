from gm.api import *
from gm.model.storage import context

import json
import time


ACCOUNT_ID = 'e1255940-325a-11f1-ab23-00163e022aa6'
SYMBOL = 'SHSE.510300'
POLL_ATTEMPTS = int(3)
POLL_INTERVAL_SEC = float(2)

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

# --- OpenClaw bootstrap ---
import json as __openclaw_json
import os as __openclaw_os
import traceback as __openclaw_traceback
from pathlib import Path as __OpenClawPath
from gm.api import *

def __openclaw_json_safe(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): __openclaw_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [__openclaw_json_safe(item) for item in value]
    return repr(value)

def __openclaw_emit(name, payload):
    encoded = __openclaw_json.dumps(__openclaw_json_safe(payload), ensure_ascii=False)
    print(f'{name}=' + encoded, flush=True)

def __openclaw_capture_metrics(indicator):
    payload = __openclaw_json_safe(indicator)
    encoded = __openclaw_json.dumps(payload, ensure_ascii=False)
    print('OPENCLAW_METRICS=' + encoded, flush=True)
    metrics_path = __openclaw_os.environ.get('OPENCLAW_METRICS_PATH', '').strip()
    if metrics_path:
        __OpenClawPath(metrics_path).write_text(encoded, encoding='utf-8')

__openclaw_user_on_backtest_finished = globals().get('on_backtest_finished')
if callable(__openclaw_user_on_backtest_finished):
    def on_backtest_finished(context, indicator):
        __openclaw_user_on_backtest_finished(context, indicator)
        __openclaw_capture_metrics(indicator)
else:
    def on_backtest_finished(context, indicator):
        __openclaw_capture_metrics(indicator)

__openclaw_emit(
    'OPENCLAW_BOOTSTRAP_READY',
    {
        'mode': 'simulation',
        'strategy_id': 'openclaw_probe_real_id',
        'pid': __openclaw_os.getpid(),
        'cwd': __openclaw_os.getcwd(),
        'file': __file__,
    },
)

if __name__ == '__main__':
    __openclaw_token = __openclaw_os.environ.get('GM_TOKEN', '').strip()
    if not __openclaw_token:
        raise RuntimeError('Missing token env: GM_TOKEN')
    __openclaw_emit(
        'OPENCLAW_RUN_START',
        {
            'mode': 'simulation',
            'strategy_id': 'openclaw_probe_real_id',
            'pid': __openclaw_os.getpid(),
            'cwd': __openclaw_os.getcwd(),
            'file': __file__,
        },
    )
    try:
        __openclaw_status = run(
            strategy_id='openclaw_probe_real_id',
            filename=__openclaw_os.path.basename(__file__),
            mode=MODE_LIVE,
            token=__openclaw_token,
        )
    except Exception as __openclaw_exc:
        __openclaw_emit(
            'OPENCLAW_RUN_EXCEPTION',
            {
                'error': repr(__openclaw_exc),
                'traceback': __openclaw_traceback.format_exc(),
            },
        )
        raise
    else:
        __openclaw_emit('OPENCLAW_RUN_EXIT', {'status': __openclaw_status})
