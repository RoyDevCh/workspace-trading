from gm.api import *

import json


ACCOUNT_ID = 'openclawtest'
SYMBOL = 'SHSE.510300'
SIDE = str('buy').strip().lower()
ORDER_VOLUME = int(100)
ORDER_TYPE_NAME = str('market').strip().lower()

_submitted = False


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
        for key in ("available", "can_sell_volume", "volume"):
            value = row.get(key)
            if value in (None, ""):
                continue
            try:
                return int(float(value))
            except Exception:
                continue
    return 0


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
        volume = min(volume, _position_volume())
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
    status = str((order or {}).get("status") or "").strip()
    if status:
        stop()


def on_execution_report(context, report):
    _emit("OPENCLAW_EXECUTION_REPORT", report)


def on_account_status(context, account):
    _emit("OPENCLAW_ACCOUNT_STATUS", account)
    _submit_order()


def on_error(context, code, info):
    _emit("OPENCLAW_ERROR", {"code": code, "info": info})

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
        'strategy_id': 'openclaw_one_shot_buy_bgfix',
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
            'strategy_id': 'openclaw_one_shot_buy_bgfix',
            'pid': __openclaw_os.getpid(),
            'cwd': __openclaw_os.getcwd(),
            'file': __file__,
        },
    )
    try:
        __openclaw_status = run(
            strategy_id='openclaw_one_shot_buy_bgfix',
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
