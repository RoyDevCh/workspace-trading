from gm.api import *

SYMBOL = 'SHSE.600000'
FREQUENCY = '1d'
FAST_PERIOD = int(5)
SLOW_PERIOD = int(10)
ORDER_VOLUME = int(100)
REBALANCE_TIME = '14:50:00'


def init(context):
    subscribe(symbols=SYMBOL, frequency=FREQUENCY, count=max(SLOW_PERIOD + 5, 60))
    schedule(schedule_func=algo, date_rule="1d", time_rule=REBALANCE_TIME)


def _long_position_volume() -> int:
    try:
        positions = get_position()
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
        )
        return

    if fast_ma < slow_ma and held_volume > 0:
        order_volume(
            symbol=SYMBOL,
            volume=held_volume,
            side=OrderSide_Sell,
            order_type=OrderType_Market,
            position_effect=PositionEffect_Close,
        )

# --- OpenClaw bootstrap ---
import json as __openclaw_json
import os as __openclaw_os
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

def __openclaw_capture_metrics(indicator):
    payload = __openclaw_json_safe(indicator)
    encoded = __openclaw_json.dumps(payload, ensure_ascii=False)
    print('OPENCLAW_METRICS=' + encoded)
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

if __name__ == '__main__':
    __openclaw_token = __openclaw_os.environ.get('GM_TOKEN', '').strip()
    if not __openclaw_token:
        raise RuntimeError('Missing token env: GM_TOKEN')
    run(
        strategy_id='openclaw_gm_opt_smoke',
        filename=__openclaw_os.path.basename(__file__),
        mode=MODE_BACKTEST,
        token=__openclaw_token,
        backtest_start_time='2025-01-02 09:30:00',
        backtest_end_time='2025-03-31 15:00:00',
        backtest_initial_cash=1000000.0,
        backtest_transaction_ratio=1.0,
        backtest_commission_ratio=0.0001,
        backtest_slippage_ratio=0.0001,
        backtest_adjust=0,
        backtest_check_cache=1,
        backtest_match_mode=0,
        backtest_intraday=0,
    )
