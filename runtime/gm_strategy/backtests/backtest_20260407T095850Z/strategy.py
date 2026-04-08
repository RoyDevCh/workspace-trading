from gm.api import *

SYMBOL = 'SHSE.600000'
ACCOUNT_ID = 'openclawtest'
FREQUENCY = '1d'
FAST_PERIOD = int(3)
SLOW_PERIOD = int(10)
ORDER_VOLUME = int(100)
REBALANCE_TIME = '14:50:00'


def init(context):
    subscribe(symbols=SYMBOL, frequency=FREQUENCY, count=max(SLOW_PERIOD + 5, 60))
    schedule(schedule_func=algo, date_rule="1d", time_rule=REBALANCE_TIME)


def _long_position_volume() -> int:
    try:
        positions = get_position(account_id=ACCOUNT_ID or None)
    except Exception:
        return 0

    if not positions:
        return 0

    if isinstance(positions, dict):
        items = positions.get("data") or positions.get("positions") or [positions]
    else:
        items = positions

    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("symbol") or "") != SYMBOL:
            continue
        side = item.get("side")
        if side not in (None, 1, "1", "long", "Long"):
            continue
        for key in ("volume", "available", "can_sell_volume"):
            value = item.get(key)
            if value in (None, ""):
                continue
            try:
                return int(float(value))
            except Exception:
                continue
    return 0


def algo(context):
    bars = history_n(
        symbol=SYMBOL,
        frequency=FREQUENCY,
        count=max(SLOW_PERIOD + 5, 60),
        fields="close",
        df=True,
    )
    if bars is None or len(bars) < SLOW_PERIOD:
        return

    closes = bars["close"].astype(float)
    fast_ma = float(closes.tail(FAST_PERIOD).mean())
    slow_ma = float(closes.tail(SLOW_PERIOD).mean())
    held_volume = _long_position_volume()

    if fast_ma > slow_ma and held_volume <= 0:
        order_volume(
            symbol=SYMBOL,
            volume=ORDER_VOLUME,
            side=OrderSide_Buy,
            order_type=OrderType_Market,
            position_effect=PositionEffect_Open,
            account=ACCOUNT_ID or "",
        )
        return

    if fast_ma < slow_ma and held_volume > 0:
        order_volume(
            symbol=SYMBOL,
            volume=held_volume,
            side=OrderSide_Sell,
            order_type=OrderType_Market,
            position_effect=PositionEffect_Close,
            account=ACCOUNT_ID or "",
        )

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
        'mode': 'backtest',
        'strategy_id': 'openclaw_gm_strategy',
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
            'mode': 'backtest',
            'strategy_id': 'openclaw_gm_strategy',
            'pid': __openclaw_os.getpid(),
            'cwd': __openclaw_os.getcwd(),
            'file': __file__,
        },
    )
    try:
        __openclaw_status = run(
            strategy_id='openclaw_gm_strategy',
            filename=__openclaw_os.path.basename(__file__),
            mode=MODE_BACKTEST,
            token=__openclaw_token,
            backtest_start_time='2025-01-01 09:30:00',
            backtest_end_time='2025-12-31 15:00:00',
            backtest_initial_cash=1000000.0,
            backtest_transaction_ratio=1.0,
            backtest_commission_ratio=0.0001,
            backtest_slippage_ratio=0.0001,
            backtest_adjust=0,
            backtest_check_cache=1,
            backtest_match_mode=0,
            backtest_intraday=0,
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
