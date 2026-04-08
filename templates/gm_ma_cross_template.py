from gm.api import *

SYMBOL = {{symbol|py}}
ACCOUNT_ID = {{account_id|py}}
FREQUENCY = {{frequency|py}}
FAST_PERIOD = int({{fast_period}})
SLOW_PERIOD = int({{slow_period}})
ORDER_VOLUME = int({{order_volume}})
REBALANCE_TIME = {{rebalance_time|py}}


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
