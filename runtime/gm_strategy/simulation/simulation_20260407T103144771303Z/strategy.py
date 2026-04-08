from gm.api import *

import json


ACCOUNT_ID = 'openclawtest'
SYMBOL = 'SHSE.510300'

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

    payload = {"account_id": ACCOUNT_ID, "symbol": SYMBOL}

    try:
        payload["cash"] = get_cash(account_id=ACCOUNT_ID or None)
    except Exception as exc:
        payload["cash_error"] = str(exc)

    try:
        account = context.account(ACCOUNT_ID or "")
        payload["context_account_exists"] = bool(account)
        payload["context_cash"] = getattr(account, "cash", None) if account else None
        payload["context_positions"] = account.positions(symbol=SYMBOL) if account else []
    except Exception as exc:
        payload["context_error"] = str(exc)

    try:
        positions = get_position(account_id=ACCOUNT_ID or None)
        payload["positions"] = _filter_symbol(_rows(positions))
    except Exception as exc:
        payload["positions_error"] = str(exc)

    try:
        orders = get_orders()
        payload["orders"] = _filter_symbol(_rows(orders))
    except Exception as exc:
        payload["orders_error"] = str(exc)

    try:
        unfinished = get_unfinished_orders()
        payload["unfinished_orders"] = _filter_symbol(_rows(unfinished))
    except Exception as exc:
        payload["unfinished_orders_error"] = str(exc)

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
        'strategy_id': 'openclaw_account_probe_set_account',
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
            'strategy_id': 'openclaw_account_probe_set_account',
            'pid': __openclaw_os.getpid(),
            'cwd': __openclaw_os.getcwd(),
            'file': __file__,
        },
    )
    try:
        __openclaw_status = run(
            strategy_id='openclaw_account_probe_set_account',
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
