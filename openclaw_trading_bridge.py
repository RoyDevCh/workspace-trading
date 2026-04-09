#!/usr/bin/env python
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
import yaml

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


THIS_FILE = Path(__file__).resolve()
WORKSPACE_DIR = THIS_FILE.parent
OPENCLAW_ROOT = WORKSPACE_DIR.parent
DEFAULT_RULES_PATH = WORKSPACE_DIR / "trading_rules.yaml"
DEFAULT_STATE_PATH = WORKSPACE_DIR / "runtime" / "trading_state.json"
DEFAULT_MACRO_STATE_PATH = WORKSPACE_DIR / "runtime" / "macro_state.json"
DEFAULT_LOG_PATH = WORKSPACE_DIR / "logs" / "trading_decisions.jsonl"
DEFAULT_OPTIMIZATION_REPORT_PATH = WORKSPACE_DIR / "runtime" / "strategy_report.json"
DEFAULT_RUNTIME_LOCK_PATH = WORKSPACE_DIR / "runtime" / "trading_runtime.lock"
DEFAULT_TIMEZONE = "Asia/Shanghai"
ENV_CANDIDATE_PATHS = [
    OPENCLAW_ROOT / "agents" / "trading" / ".env",
    OPENCLAW_ROOT / "agents" / "trading" / "agent" / ".env",
    WORKSPACE_DIR / ".env",
]

_ENV_LOADED = False

sys.path.insert(0, str(OPENCLAW_ROOT))

from execution import create_executor
from gatekeeper.gatekeeper import (
    APPROVAL_STATE_PATH,
    GateKeeperSkill,
    collect_feishu_targets,
    find_feishu_reply,
    load_json as load_approval_json,
    now_ts as approval_now_ts,
    parse_iso_timestamp,
    save_json as save_approval_json,
    send_native_feishu_text,
)
from sensory import create_data_manager


class TradingBridgeError(RuntimeError):
    pass


def load_runtime_env() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return

    for env_path in ENV_CANDIDATE_PATHS:
        if not env_path.exists():
            continue

        for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip().lstrip("\ufeff")
            value = value.strip().strip("'").strip('"')
            if key and (key not in os.environ or not os.environ.get(key)):
                os.environ[key] = value

    _ENV_LOADED = True


@contextmanager
def temporarily_clear_proxy_env():
    proxy_keys = [
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ]
    saved = {key: os.environ.get(key) for key in proxy_keys if key in os.environ}
    for key in proxy_keys:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value


def read_payload() -> Dict[str, Any]:
    raw = sys.stdin.read().lstrip("\ufeff").strip()
    if not raw:
        return {}
    return json.loads(raw)


def load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def get_rules_path(payload: Dict[str, Any]) -> Path:
    candidate = payload.get("rules_file")
    return Path(candidate) if candidate else DEFAULT_RULES_PATH


def default_rules() -> Dict[str, Any]:
    return {
        "meta": {"timezone": DEFAULT_TIMEZONE},
        "agent": {
            "core_symbols": ["BTC/USDT", "ETH/USDT", "510300", "159915"],
            "symbols": ["BTC/USDT", "ETH/USDT"],
            "review_markets": ["crypto", "cn_equity"],
            "auto_execution_markets": ["crypto", "cn_equity"],
            "watchlists": {
                "crypto": ["BTC/USDT", "ETH/USDT"],
                "cn_equity": ["510300", "159915"],
                "futures": ["BTC/USDT", "ETH/USDT"],
            },
            "symbol_groups": {
                "crypto": ["BTC/USDT", "ETH/USDT"],
                "cn_equity": ["510300", "159915"],
                "futures": ["BTC/USDT", "ETH/USDT"],
            },
            "market_settings": {
                "crypto": {
                    "timeframe": "1h",
                    "lookback": 100,
                    "source": "ccxt",
                },
                "cn_equity": {
                    "timeframe": "1d",
                    "lookback": 120,
                    "source": "sina",
                },
                "futures": {
                    "timeframe": "1h",
                    "lookback": 120,
                    "source": "ccxt",
                },
            },
            "dynamic_assets": {},
            "interval_minutes": 5,
            "data_source": "ccxt",
            "timeframe": "1h",
            "lookback": 100,
        },
        "strategy": {
            "default_strategy": "combined",
            "definitions": {
                "trend_following": {
                    "name": "trend_following",
                    "kind": "trend_following",
                    "ema_fast_period": 5,
                    "ema_slow_period": 20,
                    "rsi_period": 14,
                    "buy_rsi_below": 70,
                    "sell_rsi_above": 85,
                    "sell_all_on_exit": True,
                    "order_size": {
                        "default": 0.001,
                    },
                },
                "combined": {
                    "name": "combined",
                    "kind": "combined",
                    "fast": 5,
                    "slow": 20,
                    "rsi_period": 14,
                    "rsi_upper": 70,
                    "rsi_lower": 30,
                    "atr_period": 14,
                    "atr_multiplier": 2.0,
                    "volume_multiplier": 1.5,
                    "volume_lookback": 20,
                    "trend_filter_ema_period": 200,
                    "higher_timeframe": "4h",
                    "higher_timeframe_ema_period": 50,
                    "higher_timeframe_lookback": 120,
                    "use_4h_filter": True,
                    "move_stop_to_entry_on_profit": True,
                    "breakeven_trigger_pct": 0.03,
                    "sell_all_on_exit": True,
                    "order_size": {
                        "default": 0.001,
                    },
                },
                "mean_reversion": {
                    "name": "mean_reversion",
                    "kind": "mean_reversion",
                    "bb_period": 20,
                    "bb_stddev": 2.0,
                    "rsi_period": 14,
                    "buy_rsi_below": 38,
                    "sell_rsi_above": 68,
                    "exit_on_midline": True,
                    "midline_exit_rsi_above": 52,
                    "sell_all_on_exit": True,
                    "order_size": {
                        "default": 0.05,
                    },
                },
            },
            "market_overrides": {
                "cn_equity": {
                    "strategy": "trend_following",
                    "ema_fast_period": 8,
                    "ema_slow_period": 21,
                    "rsi_period": 14,
                    "buy_rsi_below": 68,
                    "sell_rsi_above": 82,
                    "use_higher_timeframe_filter": True,
                    "higher_timeframe": "1wk",
                    "higher_timeframe_ema_period": 13,
                    "higher_timeframe_lookback": 80,
                    "order_size": {
                        "default": 100,
                    },
                },
            },
            "symbol_overrides": {
                "BTC/USDT": {
                    "strategy": "combined",
                    "fast": 5,
                    "slow": 20,
                    "rsi_period": 14,
                    "rsi_upper": 70,
                    "rsi_lower": 30,
                    "atr_period": 14,
                    "atr_multiplier": 2.0,
                    "volume_multiplier": 1.5,
                    "volume_lookback": 20,
                    "trend_filter_ema_period": 200,
                    "higher_timeframe": "4h",
                    "higher_timeframe_ema_period": 50,
                    "higher_timeframe_lookback": 120,
                    "use_4h_filter": True,
                    "position_scale": 1.0,
                    "order_size": {
                        "default": 0.01,
                        "per_symbol": {
                            "BTC/USDT": 0.01,
                        },
                    },
                },
                "ETH/USDT": {
                    "strategy": "combined",
                    "fast": 5,
                    "slow": 20,
                    "rsi_period": 14,
                    "rsi_upper": 70,
                    "rsi_lower": 30,
                    "atr_period": 14,
                    "atr_multiplier": 2.0,
                    "volume_multiplier": 1.5,
                    "volume_lookback": 20,
                    "trend_filter_ema_period": 200,
                    "higher_timeframe": "4h",
                    "higher_timeframe_ema_period": 50,
                    "higher_timeframe_lookback": 120,
                    "use_4h_filter": True,
                    "position_scale": 0.8,
                    "order_size": {
                        "default": 0.1,
                        "per_symbol": {
                            "ETH/USDT": 0.1,
                        },
                    },
                },
                "510300": {
                    "strategy": "trend_following",
                    "ema_fast_period": 8,
                    "ema_slow_period": 21,
                    "order_size": {
                        "default": 200,
                        "per_symbol": {
                            "510300": 200,
                        },
                    },
                },
                "159915": {
                    "strategy": "trend_following",
                    "ema_fast_period": 8,
                    "ema_slow_period": 21,
                    "order_size": {
                        "default": 200,
                        "per_symbol": {
                            "159915": 200,
                        },
                    },
                },
            },
        },
        "risk": {
            "max_single_loss_pct": 0.02,
            "max_daily_loss_pct": 0.05,
            "default_stop_loss_pct": 0.02,
            "max_order_value_usdt": 50000,
            "max_position_value_usdt": 100000,
            "max_daily_notional_usdt": 200000,
            "min_cash_ratio": 0.10,
            "consecutive_failures_to_pause": 3,
            "pause_minutes_after_failures": 30,
        },
        "account": {
            "execution_mode": "ccxt",
            "exchange": "binance",
            "testnet": True,
            "assumed_equity_usdt": 10000,
            "assumed_equity_by_market": {
                "crypto": 10000,
                "cn_equity": 100000,
                "futures": 10000,
            },
            "market_modes": {
                "crypto": "ccxt_futures",
                "cn_equity": "ths_bridge",
                "futures": "ccxt_futures",
            },
        },
        "macro": {
            "state_file": str(DEFAULT_MACRO_STATE_PATH),
            "refresh_interval_minutes": 15,
            "crypto_source": "ccxt",
            "crypto_symbol": "BTC/USDT",
            "crypto_interval": "1d",
            "cn_equity_source": "auto",
            "cn_equity_symbol": "510300",
            "cn_equity_interval": "1d",
            "lookback": 120,
            "volatility_window": 20,
            "high_vol_percentile": 0.8,
            "position_scale_reduction_on_high_vol": 0.5,
        },
        "market_sessions": {
            "cn_equity": {
                "enabled": True,
                "timezone": DEFAULT_TIMEZONE,
                "trading_days": ["mon", "tue", "wed", "thu", "fri"],
                "holidays": [],
                "extra_trading_days": [],
                "sessions": [
                    {"name": "morning", "start": "09:30", "end": "11:30"},
                    {"name": "afternoon", "start": "13:00", "end": "15:00"},
                ],
            },
        },
        "optimization": {
            "enabled": True,
            "report_file": str(DEFAULT_OPTIMIZATION_REPORT_PATH),
            "lookback_events": 200,
            "lookback_days": 30,
            "min_executed_trades": 3,
            "min_pnl_samples": 3,
            "low_win_rate": 0.4,
            "high_win_rate": 0.6,
            "negative_avg_pnl_ratio": -0.002,
            "positive_avg_pnl_ratio": 0.003,
            "reduce_order_size_factor": 0.9,
            "increase_order_size_factor": 1.05,
            "min_order_size_floor": {
                "crypto": 0.001,
                "cn_equity": 100,
            },
            "max_order_size_cap": {
                "crypto": 0.05,
                "cn_equity": 5000,
            },
        },
        "auto_discover": {
            "enabled": True,
            "max_total_symbols": 15,
            "cleanup_on_discovery": True,
            "crypto": {
                "enabled": True,
                "min_volume_usdt": 1000,
                "min_price": 0.01,
                "min_volatility_pct": 3,
                "max_new_per_scan": 3,
                "max_inactive_days": 7,
                "max_drawdown_pct": 0.15,
                "strategy": "combined",
                "position_scale": 0.2,
                "order_size_default": 0.001,
            },
            "cn_equity": {
                "enabled": True,
                "min_amount": 100000000,
                "min_turnover": 2.0,
                "min_price": 1.0,
                "fallback_min_change_pct": 1.0,
                "max_new_per_scan": 3,
                "max_inactive_trading_days": 10,
                "max_drawdown_pct": 0.10,
                "strategy": "trend_following",
                "position_scale": 0.3,
                "order_size_default": 100,
                "candidate_universe": [
                    "510300",
                    "159915",
                    "510050",
                    "588000",
                    "512100",
                    "159949",
                    "600519",
                    "000858",
                    "601318",
                    "300750",
                    "600036",
                    "000001",
                ],
            },
        },
        "logging": {
            "decision_log": str(DEFAULT_LOG_PATH),
            "state_file": str(DEFAULT_STATE_PATH),
        },
    }


def load_rules(payload: Dict[str, Any]) -> Dict[str, Any]:
    return deep_merge(default_rules(), load_yaml(get_rules_path(payload)))


def load_root_config() -> Dict[str, Any]:
    load_runtime_env()
    return load_yaml(OPENCLAW_ROOT / "config.yaml")


def resolve_timezone(rules: Dict[str, Any]):
    tz_name = rules.get("meta", {}).get("timezone", DEFAULT_TIMEZONE)
    if ZoneInfo is None:
        return timezone(timedelta(hours=8))
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo(DEFAULT_TIMEZONE)


def now_local(rules: Dict[str, Any]) -> datetime:
    return datetime.now(resolve_timezone(rules))


def today_key(rules: Dict[str, Any]) -> str:
    return now_local(rules).strftime("%Y-%m-%d")


def _normalize_weekday_token(value: Any) -> str:
    text = str(value or "").strip().lower()
    mapping = {
        "mon": "mon",
        "monday": "mon",
        "1": "mon",
        "tue": "tue",
        "tuesday": "tue",
        "2": "tue",
        "wed": "wed",
        "wednesday": "wed",
        "3": "wed",
        "thu": "thu",
        "thursday": "thu",
        "4": "thu",
        "fri": "fri",
        "friday": "fri",
        "5": "fri",
        "sat": "sat",
        "saturday": "sat",
        "6": "sat",
        "sun": "sun",
        "sunday": "sun",
        "0": "sun",
        "7": "sun",
    }
    return mapping.get(text, "")


def _parse_clock_minutes(raw_value: Any, fallback: int) -> int:
    text = str(raw_value or "").strip()
    if not text or ":" not in text:
        return fallback
    hour_text, minute_text = text.split(":", 1)
    try:
        hour = max(min(int(hour_text), 23), 0)
        minute = max(min(int(minute_text), 59), 0)
    except Exception:
        return fallback
    return hour * 60 + minute


def market_session_config(rules: Dict[str, Any], root_cfg: Dict[str, Any] | None = None, market: str = "cn_equity") -> Dict[str, Any]:
    defaults = {
        "cn_equity": {
            "enabled": True,
            "timezone": DEFAULT_TIMEZONE,
            "trading_days": ["mon", "tue", "wed", "thu", "fri"],
            "holidays": [],
            "extra_trading_days": [],
            "sessions": [
                {"name": "morning", "start": "09:30", "end": "11:30"},
                {"name": "afternoon", "start": "13:00", "end": "15:00"},
            ],
        },
    }
    root_cfg = root_cfg or load_root_config()
    merged = deep_merge(defaults.get(market, {}), root_cfg.get("market_sessions", {}).get(market, {}))
    return deep_merge(merged, rules.get("market_sessions", {}).get(market, {}))


def market_session_status(
    market: str,
    rules: Dict[str, Any],
    root_cfg: Dict[str, Any] | None = None,
    current_time: datetime | None = None,
) -> Dict[str, Any]:
    root_cfg = root_cfg or load_root_config()
    current = current_time or now_local(rules)
    if current.tzinfo is None:
        current = current.replace(tzinfo=resolve_timezone(rules))
    else:
        current = current.astimezone(resolve_timezone(rules))

    if market != "cn_equity":
        return {
            "market": market,
            "enabled": True,
            "timezone": str(current.tzinfo or DEFAULT_TIMEZONE),
            "current_time": current.isoformat(),
            "trading_day": True,
            "within_session": True,
            "execution_allowed": True,
            "phase": "always_open",
            "reason": "Market trades continuously",
        }

    cfg = market_session_config(rules, root_cfg, market=market)
    enabled = bool(cfg.get("enabled", True))
    timezone_name = str(cfg.get("timezone") or DEFAULT_TIMEZONE)
    weekday = current.strftime("%a").lower()
    weekday_token = _normalize_weekday_token(weekday)
    date_text = current.strftime("%Y-%m-%d")

    trading_days = {
        token
        for token in (_normalize_weekday_token(item) for item in cfg.get("trading_days", []))
        if token
    }
    holidays = {str(item).strip() for item in cfg.get("holidays", []) if str(item).strip()}
    extra_trading_days = {str(item).strip() for item in cfg.get("extra_trading_days", []) if str(item).strip()}

    is_holiday = date_text in holidays
    trading_day = date_text in extra_trading_days or (weekday_token in trading_days and not is_holiday)
    current_minutes = current.hour * 60 + current.minute

    sessions_summary: List[Dict[str, Any]] = []
    active_session = ""
    within_session = False
    for session_cfg in cfg.get("sessions", []):
        start_minutes = _parse_clock_minutes(session_cfg.get("start"), 0)
        end_minutes = _parse_clock_minutes(session_cfg.get("end"), 0)
        session_name = str(session_cfg.get("name") or "").strip() or f"session_{len(sessions_summary) + 1}"
        session_open = trading_day and start_minutes <= current_minutes < end_minutes
        sessions_summary.append(
            {
                "name": session_name,
                "start": str(session_cfg.get("start") or ""),
                "end": str(session_cfg.get("end") or ""),
                "active": bool(session_open),
            }
        )
        if session_open:
            within_session = True
            active_session = session_name

    phase = "closed"
    reason = "China A-share market is outside trading hours"
    if not enabled:
        phase = "disabled"
        reason = "China A-share market-hours guard is disabled"
    elif is_holiday:
        phase = "holiday"
        reason = f"China A-share market is closed for holiday on {date_text}"
    elif not trading_day:
        phase = "weekend"
        reason = f"China A-share market is closed on non-trading day {date_text}"
    elif within_session:
        phase = active_session or "open"
        reason = f"China A-share market session is open ({phase})"
    else:
        session_ranges = [
            (
                _parse_clock_minutes(item.get("start"), 0),
                _parse_clock_minutes(item.get("end"), 0),
                str(item.get("name") or "").strip() or "session",
            )
            for item in cfg.get("sessions", [])
        ]
        if session_ranges and current_minutes < session_ranges[0][0]:
            phase = "pre_open"
            reason = "China A-share market has not opened yet"
        elif len(session_ranges) >= 2 and session_ranges[0][1] <= current_minutes < session_ranges[1][0]:
            phase = "midday_break"
            reason = "China A-share market is in the midday break"
        else:
            phase = "after_close"
            reason = "China A-share market is closed after the session"

    execution_allowed = enabled and trading_day and within_session
    return {
        "market": market,
        "enabled": enabled,
        "timezone": timezone_name,
        "current_time": current.isoformat(),
        "date": date_text,
        "weekday": weekday_token,
        "trading_day": trading_day,
        "within_session": within_session,
        "execution_allowed": execution_allowed,
        "phase": phase,
        "active_session": active_session,
        "reason": reason,
        "sessions": sessions_summary,
    }


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def resolve_workspace_path(raw_path: Any, default_path: Path) -> Path:
    if raw_path in (None, ""):
        return default_path
    candidate = Path(str(raw_path)).expanduser()
    if not candidate.is_absolute():
        candidate = OPENCLAW_ROOT / candidate
    return candidate


def runtime_lock_path(rules: Dict[str, Any]) -> Path:
    state_path = Path(rules["logging"]["state_file"])
    return state_path.parent / DEFAULT_RUNTIME_LOCK_PATH.name


@contextmanager
def runtime_lock(rules: Dict[str, Any], timeout_seconds: float = 30.0, stale_seconds: float = 1800.0):
    lock_path = runtime_lock_path(rules)
    ensure_parent(lock_path)
    deadline = time.time() + timeout_seconds

    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "created_at": datetime.now(timezone.utc).isoformat(),
                        },
                        ensure_ascii=False,
                    )
                )
            break
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > stale_seconds:
                    lock_path.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.time() >= deadline:
                raise TradingBridgeError(f"Timed out waiting for runtime lock: {lock_path}")
            time.sleep(0.2)

    try:
        yield
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass


def load_state(rules: Dict[str, Any]) -> Dict[str, Any]:
    state_path = Path(rules["logging"]["state_file"])
    if not state_path.exists():
        return {
            "consecutive_failures": 0,
            "paused_until": None,
            "daily_risk_committed": {},
            "open_positions": {},
            "recent_events": [],
            "updated_at": None,
        }
    with state_path.open("r", encoding="utf-8") as handle:
        state = json.load(handle)
    if not isinstance(state, dict):
        state = {}
    state.setdefault("consecutive_failures", 0)
    state.setdefault("paused_until", None)
    state.setdefault("daily_risk_committed", {})
    state.setdefault("open_positions", {})
    state.setdefault("recent_events", [])
    state.setdefault("updated_at", None)
    return state


def save_state(state: Dict[str, Any], rules: Dict[str, Any]) -> None:
    state_path = Path(rules["logging"]["state_file"])
    ensure_parent(state_path)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp_path = state_path.with_suffix(f"{state_path.suffix}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(to_jsonable(state), handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, state_path)


def repair_trailing_log_garbage(log_path: Path) -> None:
    if not log_path.exists():
        return

    lines = log_path.read_text(encoding="utf-8").splitlines()
    trimmed = list(lines)
    changed = False

    while trimmed:
        candidate = trimmed[-1].strip()
        if not candidate:
            trimmed.pop()
            changed = True
            continue
        try:
            json.loads(candidate)
            break
        except Exception:
            trimmed.pop()
            changed = True

    if not changed:
        return

    payload = "\n".join(trimmed)
    if payload:
        payload += "\n"
    log_path.write_text(payload, encoding="utf-8")


def append_decision(event: Dict[str, Any], rules: Dict[str, Any]) -> None:
    log_path = Path(rules["logging"]["decision_log"])
    ensure_parent(log_path)
    repair_trailing_log_garbage(log_path)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(to_jsonable(event), ensure_ascii=False) + "\n")


def load_decision_events(rules: Dict[str, Any]) -> List[Dict[str, Any]]:
    log_path = Path(rules["logging"]["decision_log"])
    if not log_path.exists():
        return []

    events: List[Dict[str, Any]] = []
    for raw_line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _sanitize_surrogates(value: str) -> str:
    return value.encode("utf-8", errors="replace").decode("utf-8")


def to_jsonable(value: Any) -> Any:
    if isinstance(value, str):
        return _sanitize_surrogates(value)
    if is_dataclass(value):
        return {k: to_jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, pd.DataFrame):
        return value.to_dict(orient="records")
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return str(value)
    if hasattr(value, "value"):
        try:
            return value.value
        except Exception:
            return str(value)
    return value


def normalize_candles(candles: List[Dict[str, Any]]) -> pd.DataFrame:
    if not candles:
        raise TradingBridgeError("market_data is empty")
    df = pd.DataFrame(candles)
    for column in ["open", "high", "low", "close", "volume"]:
        if column not in df.columns:
            raise TradingBridgeError(f"market_data is missing '{column}'")
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    if df.empty:
        raise TradingBridgeError("market_data contains no usable candles")
    return df


def ema_series(closes: pd.Series, period: int) -> pd.Series:
    return closes.ewm(span=period, adjust=False).mean()


def sma_series(closes: pd.Series, period: int) -> pd.Series:
    return closes.rolling(window=period, min_periods=period).mean()


def rsi_series(closes: pd.Series, period: int) -> pd.Series:
    delta = closes.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    avg_gain = gains.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = losses.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    if len(rsi) and pd.isna(rsi.iloc[-1]) and avg_loss.iloc[-1] == 0:
        rsi.iloc[-1] = 100.0
    return rsi


def macd_bundle(closes: pd.Series) -> Dict[str, float]:
    fast = ema_series(closes, 12)
    slow = ema_series(closes, 26)
    macd = fast - slow
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = macd - signal
    return {
        "macd": float(macd.iloc[-1]),
        "macd_signal": float(signal.iloc[-1]),
        "macd_hist": float(hist.iloc[-1]),
    }


def bollinger_bundle(closes: pd.Series, period: int, stddev: float) -> Dict[str, Any]:
    middle = sma_series(closes, period)
    rolling_std = closes.rolling(window=period, min_periods=period).std(ddof=0)
    upper = middle + (rolling_std * stddev)
    lower = middle - (rolling_std * stddev)

    def latest(series: pd.Series) -> float | None:
        if series.empty:
            return None
        value = series.iloc[-1]
        if pd.isna(value):
            return None
        return float(value)

    def previous(series: pd.Series) -> float | None:
        if len(series) > 1:
            value = series.iloc[-2]
            if not pd.isna(value):
                return float(value)
        return latest(series)

    return {
        "bb_period": period,
        "bb_stddev": float(stddev),
        "bb_middle": latest(middle),
        "bb_middle_prev": previous(middle),
        "bb_upper": latest(upper),
        "bb_upper_prev": previous(upper),
        "bb_lower": latest(lower),
        "bb_lower_prev": previous(lower),
    }


def normalize_strategy_kind(value: Any) -> str:
    text = str(value or "").strip().lower()
    mapping = {
        "ema_rsi_guard": "trend_following",
        "ema_cross": "trend_following",
        "trend": "trend_following",
        "trend_following": "trend_following",
        "combo": "combined",
        "combined": "combined",
        "bollinger_rsi_reversion": "mean_reversion",
        "bollinger_mean_reversion": "mean_reversion",
        "mean": "mean_reversion",
        "mean_reversion": "mean_reversion",
    }
    return mapping.get(text, text or "trend_following")


def strategy_template_for_kind(kind: str) -> Dict[str, Any]:
    normalized = normalize_strategy_kind(kind)
    if normalized == "combined":
        return {
            "name": "combined",
            "kind": "combined",
            "fast": 5,
            "slow": 20,
            "rsi_period": 14,
            "rsi_upper": 70,
            "rsi_lower": 30,
            "atr_period": 14,
            "atr_multiplier": 2.0,
            "volume_multiplier": 1.5,
            "volume_lookback": 20,
            "trend_filter_ema_period": 200,
            "higher_timeframe": "4h",
            "higher_timeframe_ema_period": 50,
            "higher_timeframe_lookback": 120,
            "use_4h_filter": True,
            "move_stop_to_entry_on_profit": True,
            "breakeven_trigger_pct": 0.03,
            "sell_all_on_exit": True,
            "order_size": {
                "default": 0.001,
            },
        }
    if normalized == "mean_reversion":
        return {
            "name": "mean_reversion",
            "kind": "mean_reversion",
            "bb_period": 20,
            "bb_stddev": 2.0,
            "rsi_period": 14,
            "buy_rsi_below": 38,
            "sell_rsi_above": 68,
            "exit_on_midline": True,
            "midline_exit_rsi_above": 52,
            "sell_all_on_exit": True,
            "order_size": {
                "default": 0.05,
            },
        }
    return {
        "name": "trend_following",
        "kind": "trend_following",
        "ema_fast_period": 5,
        "ema_slow_period": 20,
        "rsi_period": 14,
        "buy_rsi_below": 70,
        "sell_rsi_above": 85,
        "use_higher_timeframe_filter": False,
        "higher_timeframe": "1wk",
        "higher_timeframe_ema_period": 13,
        "higher_timeframe_lookback": 80,
        "sell_all_on_exit": True,
        "order_size": {
            "default": 0.001,
        },
    }


def normalize_strategy_definition(definition: Dict[str, Any] | None, fallback_name: str) -> Dict[str, Any]:
    raw = dict(definition or {})
    requested_name = str(raw.get("name") or fallback_name or "").strip() or "trend_following"
    requested_kind = raw.get("kind") or raw.get("strategy") or requested_name
    normalized_kind = normalize_strategy_kind(requested_kind)
    base = strategy_template_for_kind(normalized_kind)
    merged = deep_merge(base, raw)
    merged["name"] = requested_name
    merged["kind"] = normalized_kind
    return merged


def strategy_registry_from_rules(rules: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    strategy_cfg = rules.get("strategy", {}) or {}
    definitions = strategy_cfg.get("definitions")
    registry: Dict[str, Dict[str, Any]] = {}

    if isinstance(definitions, dict) and definitions:
        for name, definition in definitions.items():
            registry[str(name)] = normalize_strategy_definition(
                definition if isinstance(definition, dict) else {},
                str(name),
            )
    else:
        legacy = {
            key: value
            for key, value in strategy_cfg.items()
            if key not in {"market_overrides", "symbol_overrides", "definitions", "default_strategy"}
        }
        if legacy:
            fallback_name = str(strategy_cfg.get("default_strategy") or legacy.get("name") or "trend_following")
            registry[fallback_name] = normalize_strategy_definition(legacy, fallback_name)

    if not registry:
        registry["trend_following"] = normalize_strategy_definition({}, "trend_following")
    return registry


def default_strategy_name_from_rules(rules: Dict[str, Any]) -> str:
    strategy_cfg = rules.get("strategy", {}) or {}
    registry = strategy_registry_from_rules(rules)
    configured = str(strategy_cfg.get("default_strategy") or "").strip()
    if configured and configured in registry:
        return configured
    return next(iter(registry.keys()))


def calculate_indicator_bundle(df: pd.DataFrame, rules: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    strategy_symbol = str(params.get("__symbol__") or params.get("symbol") or "")
    strategy_market = str(params.get("__market__") or params.get("market") or "")
    strategy = strategy_config_for_symbol(
        strategy_symbol,
        rules,
        {"symbol": strategy_symbol, "market": strategy_market},
    ) if (strategy_symbol or strategy_market) else rules.get("strategy", {})
    fast_period = int(
        params.get(
            "fast",
            params.get(
                "sma_fast_period",
                params.get("ema_fast_period", strategy.get("fast", strategy.get("sma_fast_period", strategy.get("ema_fast_period", 5)))),
            ),
        )
    )
    slow_period = int(
        params.get(
            "slow",
            params.get(
                "sma_slow_period",
                params.get("ema_slow_period", strategy.get("slow", strategy.get("sma_slow_period", strategy.get("ema_slow_period", 20)))),
            ),
        )
    )
    rsi_period = int(params.get("rsi_period", strategy.get("rsi_period", 14)))
    bb_period = int(params.get("bb_period", strategy.get("bb_period", 20)))
    bb_stddev = float(params.get("bb_stddev", strategy.get("bb_stddev", strategy.get("bb_std", 2.0))))

    closes = df["close"]
    fast_ema = ema_series(closes, fast_period)
    slow_ema = ema_series(closes, slow_period)
    fast_sma = sma_series(closes, fast_period)
    slow_sma = sma_series(closes, slow_period)
    rsi = rsi_series(closes, rsi_period)
    bb = bollinger_bundle(closes, bb_period, bb_stddev)

    bundle = {
        "strategy_name": str(strategy.get("name") or default_strategy_name_from_rules(rules)),
        "strategy_kind": str(strategy.get("kind") or normalize_strategy_kind(strategy.get("name"))),
        "ema_fast_period": fast_period,
        "ema_slow_period": slow_period,
        "rsi_period": rsi_period,
        "ema_fast": float(fast_ema.iloc[-1]),
        "ema_fast_prev": float(fast_ema.iloc[-2]) if len(fast_ema) > 1 else float(fast_ema.iloc[-1]),
        "ema_slow": float(slow_ema.iloc[-1]),
        "ema_slow_prev": float(slow_ema.iloc[-2]) if len(slow_ema) > 1 else float(slow_ema.iloc[-1]),
        "ma_fast": float(fast_sma.iloc[-1]) if not pd.isna(fast_sma.iloc[-1]) else float(fast_ema.iloc[-1]),
        "ma_fast_prev": float(fast_sma.iloc[-2]) if len(fast_sma) > 1 and not pd.isna(fast_sma.iloc[-2]) else (
            float(fast_sma.iloc[-1]) if not pd.isna(fast_sma.iloc[-1]) else float(fast_ema.iloc[-1])
        ),
        "ma_slow": float(slow_sma.iloc[-1]) if not pd.isna(slow_sma.iloc[-1]) else float(slow_ema.iloc[-1]),
        "ma_slow_prev": float(slow_sma.iloc[-2]) if len(slow_sma) > 1 and not pd.isna(slow_sma.iloc[-2]) else (
            float(slow_sma.iloc[-1]) if not pd.isna(slow_sma.iloc[-1]) else float(slow_ema.iloc[-1])
        ),
        "ma_fast_period": fast_period,
        "ma_slow_period": slow_period,
        "rsi": float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else None,
        "close": float(closes.iloc[-1]),
        "close_prev": float(closes.iloc[-2]) if len(closes) > 1 else float(closes.iloc[-1]),
    }
    bundle.update(bb)
    bundle.update(macd_bundle(closes))
    return bundle


def atr_series(df: pd.DataFrame, period: int) -> pd.Series:
    if df is None or df.empty:
        return pd.Series(dtype=float)
    window = max(int(period or 14), 1)
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(window, min_periods=window).mean()


def latest_atr_value(df: pd.DataFrame, period: int) -> float | None:
    try:
        value = float(atr_series(df, period).iloc[-1])
    except Exception:
        return None
    if pd.isna(value):
        return None
    return value


def extract_order_price_value(order_payload: Dict[str, Any], fallback: Any = None) -> float | None:
    for candidate in (
        order_payload.get("avg_price"),
        order_payload.get("average"),
        order_payload.get("price"),
        fallback,
    ):
        try:
            if candidate not in (None, ""):
                return float(candidate)
        except Exception:
            continue
    return None


def extract_order_quantity_value(order_payload: Dict[str, Any], fallback: Any = None) -> float | None:
    for candidate in (
        order_payload.get("filled_quantity"),
        order_payload.get("filled"),
        order_payload.get("amount"),
        order_payload.get("quantity"),
        fallback,
    ):
        try:
            if candidate not in (None, ""):
                return float(candidate)
        except Exception:
            continue
    return None


def order_size_for_symbol(symbol: str, rules: Dict[str, Any], payload: Dict[str, Any] | None = None) -> float:
    strategy_payload = dict(payload or {})
    strategy_payload.setdefault("symbol", symbol)
    order_size = strategy_config_for_symbol(symbol, rules, strategy_payload).get("order_size", {})
    per_symbol = order_size.get("per_symbol", {})
    return float(per_symbol.get(symbol, order_size.get("default", 0.001)))


def normalize_ratio_limit(value: Any, default_value: float) -> float:
    try:
        numeric = float(value)
    except Exception:
        return float(default_value)
    if numeric < 0:
        return float(default_value)
    if numeric > 1:
        return numeric / 100.0
    return numeric


def trading_runtime_config(rules: Dict[str, Any], root_cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    root_cfg = root_cfg or load_root_config()
    raw_cfg = root_cfg.get("trading", {}) or {}
    risk_cfg = rules.get("risk", {}) or {}
    return {
        "auto_approve": bool(raw_cfg.get("auto_approve", False)),
        "max_daily_loss_pct": normalize_ratio_limit(
            raw_cfg.get("max_daily_loss"),
            float(risk_cfg.get("max_daily_loss_pct", 0.05)),
        ),
        "max_position_pct": normalize_ratio_limit(
            raw_cfg.get("max_position_pct"),
            1.0,
        ),
        "futures": futures_runtime_config(rules, root_cfg),
    }


def _normalize_positive_int(value: Any, default: int) -> int:
    try:
        return max(int(value or default), 1)
    except Exception:
        return max(int(default), 1)


def futures_runtime_config(rules: Dict[str, Any], root_cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    root_cfg = root_cfg or load_root_config()
    trading_cfg = root_cfg.get("trading", {}) or {}
    futures_cfg = trading_cfg.get("futures", {}) or {}
    risk_cfg = rules.get("risk", {}) or {}
    defaults = {
        "enabled": bool(futures_cfg.get("enabled", True)),
        "default_leverage": _normalize_positive_int(futures_cfg.get("default_leverage"), 2),
        "max_leverage": _normalize_positive_int(futures_cfg.get("max_leverage"), 5),
        "max_position_pct": normalize_ratio_limit(futures_cfg.get("max_position_pct"), 0.05),
        "max_daily_loss_pct": normalize_ratio_limit(
            futures_cfg.get("max_daily_loss"),
            float(risk_cfg.get("max_daily_loss_pct", 0.05)),
        ),
        "mandatory_stop_loss_pct": normalize_ratio_limit(
            futures_cfg.get("mandatory_stop_loss"),
            float(risk_cfg.get("default_stop_loss_pct", 0.02)),
        ),
        "max_trades_per_day": max(int(futures_cfg.get("max_trades_per_day", 8) or 8), 0),
        "require_stop_price": bool(futures_cfg.get("require_stop_price", True)),
    }
    defaults["max_leverage"] = max(defaults["max_leverage"], defaults["default_leverage"])
    return defaults


def resolve_requested_leverage(payload: Dict[str, Any], futures_cfg: Dict[str, Any]) -> int:
    requested = payload.get("leverage")
    if requested in (None, ""):
        return int(futures_cfg.get("default_leverage", 2))
    try:
        return max(int(requested), 1)
    except Exception:
        return int(futures_cfg.get("default_leverage", 2))


def executed_trade_count_for_day(rules: Dict[str, Any], market: str, day_key: str) -> int:
    count = 0
    for event in load_decision_events(rules):
        if str(event.get("status") or "").strip().lower() != "executed":
            continue
        if str(event.get("timestamp") or "").strip()[:10] != day_key:
            continue
        event_market = str(
            event.get("market")
            or (event.get("details") or {}).get("market")
            or ""
        ).strip().lower()
        if event_market != str(market or "").strip().lower():
            continue
        count += 1
    return count


def symbol_position_scale(symbol: str, rules: Dict[str, Any], payload: Dict[str, Any] | None = None) -> float:
    payload = payload or {}
    strategy_cfg = strategy_config_for_symbol(symbol, rules, payload)
    try:
        return max(float(strategy_cfg.get("position_scale", 1.0) or 1.0), 0.0)
    except Exception:
        return 1.0


def effective_position_scale(
    symbol: str,
    market: str,
    rules: Dict[str, Any],
    root_cfg: Dict[str, Any] | None = None,
    payload: Dict[str, Any] | None = None,
) -> float:
    market_scale = position_scale_for_market(market, rules, root_cfg)
    symbol_scale_value = symbol_position_scale(symbol, rules, payload or {"symbol": symbol, "market": market})
    return max(float(market_scale) * float(symbol_scale_value), 0.0)


def suggested_buy_quantity(
    symbol: str,
    market: str,
    rules: Dict[str, Any],
    root_cfg: Dict[str, Any] | None = None,
    payload: Dict[str, Any] | None = None,
) -> float:
    merged_payload = dict(payload or {})
    merged_payload.setdefault("symbol", symbol)
    merged_payload.setdefault("market", market)
    scale = effective_position_scale(symbol, market, rules, root_cfg, merged_payload)
    base_quantity = order_size_for_symbol(symbol, rules, merged_payload)
    return normalize_order_quantity(merged_payload, rules, base_quantity * scale, "buy")


def root_execution_config() -> Dict[str, Any]:
    return load_root_config().get("execution", {})


def extract_cn_equity_code(symbol: str) -> str | None:
    text = str(symbol or "").strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered.startswith(("sh", "sz", "bj")) and len(lowered) >= 8 and lowered[2:8].isdigit():
        return lowered[2:8]
    if text.isdigit() and len(text) >= 6:
        return text[-6:]
    return None


def normalize_futures_symbol(symbol: str) -> str:
    text = str(symbol or "").strip()
    if not text or ":" in text or "/" not in text:
        return text
    base, quote = text.split("/", 1)
    if quote.upper() == "USDT":
        return f"{base}/{quote}:USDT"
    return text


def infer_market(payload: Dict[str, Any], rules: Dict[str, Any], symbol: str) -> str:
    explicit = str(payload.get("market") or payload.get("asset_class") or "").strip().lower()
    if explicit in {"crypto", "cryptocurrency"}:
        return "crypto"
    if explicit in {"futures", "future", "perpetual", "swap", "crypto_futures", "usdm"}:
        return "futures"
    if explicit in {"cn_equity", "a_share", "a-stock", "a_stock", "astock", "equity_cn"}:
        return "cn_equity"
    if "/" in str(symbol):
        if ":" in str(symbol):
            return "futures"
        return "crypto"
    if extract_cn_equity_code(symbol):
        return "cn_equity"
    default_source = payload.get("source") or rules.get("agent", {}).get("data_source") or ""
    if str(default_source).lower() == "akshare":
        return "cn_equity"
    return "crypto"


def symbol_groups_from_rules(rules: Dict[str, Any]) -> Dict[str, List[str]]:
    agent_cfg = rules.get("agent", {})
    groups = agent_cfg.get("symbol_groups") or agent_cfg.get("watchlists") or {}
    if not groups:
        return {"crypto": list(agent_cfg.get("symbols", []))}
    normalized: Dict[str, List[str]] = {}
    for market, symbols in groups.items():
        normalized[str(market)] = [str(symbol) for symbol in symbols or []]
    return normalized


def unique_preserve_order(items: List[Any]) -> List[Any]:
    seen: set[str] = set()
    ordered: List[Any] = []
    for item in items:
        key = str(item)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(item)
    return ordered


def core_symbols_from_rules(rules: Dict[str, Any]) -> List[str]:
    agent_cfg = rules.get("agent", {}) or {}
    configured = agent_cfg.get("core_symbols") or []
    symbols = [str(symbol).strip() for symbol in configured if str(symbol).strip()]
    if symbols:
        return unique_preserve_order(symbols)
    return ["BTC/USDT", "ETH/USDT", "510300", "159915"]


def dynamic_assets_registry(rules: Dict[str, Any]) -> Dict[str, Any]:
    agent_cfg = rules.setdefault("agent", {})
    registry = agent_cfg.get("dynamic_assets")
    if not isinstance(registry, dict):
        registry = {}
        agent_cfg["dynamic_assets"] = registry
    return registry


def removed_dynamic_assets_registry(rules: Dict[str, Any]) -> Dict[str, Any]:
    agent_cfg = rules.setdefault("agent", {})
    registry = agent_cfg.get("removed_dynamic_assets")
    if not isinstance(registry, dict):
        registry = {}
        agent_cfg["removed_dynamic_assets"] = registry
    return registry


def auto_discover_config(rules: Dict[str, Any], root_cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    root_cfg = root_cfg or load_root_config()
    trading_cfg = root_cfg.get("trading", {}) or {}
    return deep_merge(trading_cfg.get("auto_discover", {}) or {}, rules.get("auto_discover", {}) or {})


def default_dynamic_symbol_override(symbol: str, market: str, market_cfg: Dict[str, Any]) -> Dict[str, Any]:
    if market == "cn_equity":
        order_size = int(market_cfg.get("order_size_default", 100) or 100)
        return {
            "strategy": str(market_cfg.get("strategy") or "trend_following"),
            "ema_fast_period": int(market_cfg.get("ema_fast_period", 8) or 8),
            "ema_slow_period": int(market_cfg.get("ema_slow_period", 21) or 21),
            "rsi_period": int(market_cfg.get("rsi_period", 14) or 14),
            "buy_rsi_below": float(market_cfg.get("buy_rsi_below", 68) or 68),
            "sell_rsi_above": float(market_cfg.get("sell_rsi_above", 82) or 82),
            "use_higher_timeframe_filter": bool(market_cfg.get("use_higher_timeframe_filter", True)),
            "higher_timeframe": str(market_cfg.get("higher_timeframe") or "1wk"),
            "higher_timeframe_ema_period": int(market_cfg.get("higher_timeframe_ema_period", 13) or 13),
            "higher_timeframe_lookback": int(market_cfg.get("higher_timeframe_lookback", 80) or 80),
            "position_scale": float(market_cfg.get("position_scale", 0.3) or 0.3),
            "order_size": {
                "default": order_size,
                "per_symbol": {
                    symbol: order_size,
                },
            },
        }

    order_size = float(market_cfg.get("order_size_default", 0.001) or 0.001)
    return {
        "strategy": str(market_cfg.get("strategy") or "combined"),
        "fast": int(market_cfg.get("fast", 5) or 5),
        "slow": int(market_cfg.get("slow", 20) or 20),
        "rsi_period": int(market_cfg.get("rsi_period", 14) or 14),
        "rsi_upper": float(market_cfg.get("rsi_upper", 70) or 70),
        "rsi_lower": float(market_cfg.get("rsi_lower", 30) or 30),
        "atr_period": int(market_cfg.get("atr_period", 14) or 14),
        "atr_multiplier": float(market_cfg.get("atr_multiplier", 2.0) or 2.0),
        "volume_multiplier": float(market_cfg.get("volume_multiplier", 1.5) or 1.5),
        "volume_lookback": int(market_cfg.get("volume_lookback", 20) or 20),
        "trend_filter_ema_period": int(market_cfg.get("trend_filter_ema_period", 200) or 200),
        "higher_timeframe": str(market_cfg.get("higher_timeframe") or "4h"),
        "higher_timeframe_ema_period": int(market_cfg.get("higher_timeframe_ema_period", 50) or 50),
        "higher_timeframe_lookback": int(market_cfg.get("higher_timeframe_lookback", 120) or 120),
        "use_4h_filter": bool(market_cfg.get("use_4h_filter", True)),
        "move_stop_to_entry_on_profit": bool(market_cfg.get("move_stop_to_entry_on_profit", True)),
        "breakeven_trigger_pct": float(market_cfg.get("breakeven_trigger_pct", 0.03) or 0.03),
        "position_scale": float(market_cfg.get("position_scale", 0.2) or 0.2),
        "order_size": {
            "default": order_size,
            "per_symbol": {
                symbol: order_size,
            },
        },
    }


def is_valid_auto_discover_symbol(symbol: str, market: str) -> bool:
    text = str(symbol or "").strip()
    if not text:
        return False
    if market == "cn_equity":
        return extract_cn_equity_code(text) is not None
    if market == "crypto":
        if ":" in text or "/" not in text:
            return False
        base, quote = text.split("/", 1)
        base = base.strip().upper()
        quote = quote.strip().upper()
        if quote != "USDT" or len(base) < 2:
            return False
        normalized_base = base.replace("-", "").replace("_", "")
        return normalized_base.isalnum()
    return True


def cn_equity_discovery_universe(cfg: Dict[str, Any]) -> List[str]:
    raw_candidates = cfg.get("candidate_universe") or [
        "510300",
        "159915",
        "510050",
        "588000",
        "512100",
        "159949",
        "600519",
        "000858",
        "601318",
        "300750",
        "600036",
        "000001",
    ]
    cleaned: List[str] = []
    for candidate in raw_candidates:
        code = extract_cn_equity_code(str(candidate or ""))
        if code and code not in cleaned:
            cleaned.append(code)
    return cleaned


def add_symbols_to_rules(
    rules: Dict[str, Any],
    candidates: List[Dict[str, Any] | str],
    market: str,
    market_cfg: Dict[str, Any],
    discovered_at: datetime | None = None,
) -> List[str]:
    discovered_ts = (discovered_at or now_local(rules)).isoformat()
    agent_cfg = rules.setdefault("agent", {})
    symbols = [str(symbol) for symbol in agent_cfg.get("symbols", [])]
    symbol_groups = agent_cfg.setdefault("symbol_groups", {})
    market_symbols = [str(symbol) for symbol in symbol_groups.get(market, [])]
    watchlists = agent_cfg.get("watchlists")
    watchlist_market_symbols = []
    if isinstance(watchlists, dict):
        watchlist_market_symbols = [str(symbol) for symbol in watchlists.get(market, [])]
    strategy_cfg = rules.setdefault("strategy", {})
    symbol_overrides = strategy_cfg.setdefault("symbol_overrides", {})
    dynamic_assets = dynamic_assets_registry(rules)
    added: List[str] = []

    for candidate in candidates:
        if isinstance(candidate, dict):
            symbol = str(candidate.get("symbol") or "").strip()
            selection = to_jsonable(candidate)
        else:
            symbol = str(candidate).strip()
            selection = {"symbol": symbol}
        if not symbol:
            continue
        if not is_valid_auto_discover_symbol(symbol, market):
            continue

        if symbol not in symbols:
            symbols.append(symbol)
            added.append(symbol)
        if symbol not in market_symbols:
            market_symbols.append(symbol)
        if isinstance(watchlists, dict) and symbol not in watchlist_market_symbols:
            watchlist_market_symbols.append(symbol)
        if symbol not in symbol_overrides:
            symbol_overrides[symbol] = default_dynamic_symbol_override(symbol, market, market_cfg)

        dynamic_assets[symbol] = {
            "symbol": symbol,
            "market": market,
            "status": "active",
            "source": "auto_discover",
            "added_at": dynamic_assets.get(symbol, {}).get("added_at") or discovered_ts,
            "last_discovered_at": discovered_ts,
            "strategy": str(symbol_overrides[symbol].get("strategy") or ""),
            "position_scale": symbol_overrides[symbol].get("position_scale"),
            "selection": selection,
        }

    agent_cfg["symbols"] = unique_preserve_order(symbols)
    symbol_groups[market] = unique_preserve_order(market_symbols)
    if isinstance(watchlists, dict):
        watchlists[market] = unique_preserve_order(watchlist_market_symbols)
    return added


def remove_symbol_from_rules(
    rules: Dict[str, Any],
    symbol: str,
    reason: str,
    removed_at: datetime | None = None,
) -> bool:
    symbol = str(symbol or "").strip()
    if not symbol or symbol in set(core_symbols_from_rules(rules)):
        return False

    agent_cfg = rules.setdefault("agent", {})
    changed = False
    original_symbols = list(agent_cfg.get("symbols", []))
    agent_cfg["symbols"] = [item for item in original_symbols if str(item) != symbol]
    if len(agent_cfg["symbols"]) != len(original_symbols):
        changed = True
    for group_key in ("symbol_groups", "watchlists"):
        groups = agent_cfg.get(group_key)
        if not isinstance(groups, dict):
            continue
        for market_name, market_symbols in list(groups.items()):
            filtered = [item for item in market_symbols or [] if str(item) != symbol]
            if len(filtered) != len(market_symbols or []):
                changed = True
            groups[market_name] = filtered

    strategy_cfg = rules.setdefault("strategy", {})
    symbol_overrides = strategy_cfg.setdefault("symbol_overrides", {})
    removed_override = symbol_overrides.pop(symbol, None)
    dynamic_assets = dynamic_assets_registry(rules)
    removed_dynamic_assets = removed_dynamic_assets_registry(rules)
    dynamic_meta = dynamic_assets.pop(symbol, {}) if symbol in dynamic_assets else {}
    removed_dynamic_assets[symbol] = {
        **to_jsonable(dynamic_meta),
        "symbol": symbol,
        "status": "removed",
        "removed_at": (removed_at or now_local(rules)).isoformat(),
        "removal_reason": reason,
        "previous_override": to_jsonable(removed_override),
    }
    return changed or removed_override is not None or bool(dynamic_meta)


def trim_excess_symbols(rules: Dict[str, Any], max_total: int) -> List[str]:
    max_total = max(int(max_total or 0), 0)
    symbols = [str(symbol) for symbol in rules.get("agent", {}).get("symbols", [])]
    if not max_total or len(symbols) <= max_total:
        return []

    core = set(core_symbols_from_rules(rules))
    dynamic_assets = dynamic_assets_registry(rules)
    keep = [symbol for symbol in symbols if symbol in core]
    dynamic_symbols = [symbol for symbol in symbols if symbol not in core]
    dynamic_symbols.sort(
        key=lambda symbol: str(
            (dynamic_assets.get(symbol) or {}).get("last_discovered_at")
            or (dynamic_assets.get(symbol) or {}).get("added_at")
            or ""
        ),
        reverse=True,
    )
    keep.extend(dynamic_symbols[: max(max_total - len(keep), 0)])
    keep_set = set(keep)
    removed: List[str] = []
    for symbol in symbols:
        if symbol in keep_set:
            continue
        if remove_symbol_from_rules(rules, symbol, "max_total_symbols exceeded during auto discovery"):
            removed.append(symbol)
    rules.setdefault("agent", {})["symbols"] = [symbol for symbol in symbols if symbol in keep_set]
    return removed


def auto_discover_summary(rules: Dict[str, Any], root_cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    cfg = auto_discover_config(rules, root_cfg)
    dynamic_assets = dynamic_assets_registry(rules)
    removed_assets = removed_dynamic_assets_registry(rules)
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "max_total_symbols": int(cfg.get("max_total_symbols", 15) or 15),
        "core_symbols": core_symbols_from_rules(rules),
        "dynamic_asset_count": len(dynamic_assets),
        "dynamic_symbols": sorted(dynamic_assets.keys()),
        "removed_dynamic_asset_count": len(removed_assets),
        "crypto": cfg.get("crypto", {}),
        "cn_equity": cfg.get("cn_equity", {}),
    }


def review_markets_from_rules(rules: Dict[str, Any]) -> List[str]:
    agent_cfg = rules.get("agent", {})
    configured = agent_cfg.get("review_markets")
    if configured:
        return [str(item) for item in configured]
    return list(symbol_groups_from_rules(rules).keys())


def auto_execution_markets_from_rules(rules: Dict[str, Any]) -> List[str]:
    agent_cfg = rules.get("agent", {})
    configured = agent_cfg.get("auto_execution_markets") or agent_cfg.get("execution_markets")
    if configured:
        return [str(item) for item in configured]
    return ["crypto"]


def market_settings_for_market(market: str, rules: Dict[str, Any]) -> Dict[str, Any]:
    agent_cfg = rules.get("agent", {})
    defaults = {
        "timeframe": agent_cfg.get("timeframe", "1h"),
        "lookback": agent_cfg.get("lookback", 100),
        "source": agent_cfg.get("data_source", "ccxt"),
    }
    return deep_merge(defaults, agent_cfg.get("market_settings", {}).get(market, {}))


def market_settings_for_symbol(symbol: str, rules: Dict[str, Any], payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    payload = payload or {}
    market = infer_market(payload, rules, symbol)
    settings = market_settings_for_market(market, rules)
    symbol_settings = (rules.get("agent", {}).get("symbol_settings", {}) or {}).get(symbol, {})
    return deep_merge(settings, symbol_settings)


def strategy_config_for_symbol(symbol: str, rules: Dict[str, Any], payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    payload = payload or {}
    strategy_cfg = rules.get("strategy", {}) or {}
    registry = strategy_registry_from_rules(rules)
    market = infer_market(payload, rules, symbol) if (symbol or payload) else "crypto"
    market_override = (strategy_cfg.get("market_overrides", {}) or {}).get(market, {}) or {}
    symbol_override = (strategy_cfg.get("symbol_overrides", {}) or {}).get(symbol, {}) or {}
    requested_name = (
        str(payload.get("strategy") or payload.get("strategy_name") or strategy_cfg.get("default_strategy") or "").strip()
        or default_strategy_name_from_rules(rules)
    )
    resolved_name = (
        str(symbol_override.get("strategy") or symbol_override.get("strategy_name") or "").strip()
        or str(market_override.get("strategy") or market_override.get("strategy_name") or "").strip()
        or requested_name
        or default_strategy_name_from_rules(rules)
    )
    base = registry.get(resolved_name) or registry.get(default_strategy_name_from_rules(rules)) or next(iter(registry.values()))
    merged = deep_merge(base, {k: v for k, v in market_override.items() if k not in {"strategy", "strategy_name"}})
    merged = deep_merge(merged, {k: v for k, v in symbol_override.items() if k not in {"strategy", "strategy_name"}})
    merged["name"] = resolved_name
    merged["kind"] = normalize_strategy_kind(merged.get("kind") or resolved_name)
    return merged


def strategy_name_for_symbol(symbol: str, rules: Dict[str, Any], payload: Dict[str, Any] | None = None) -> str:
    return str(strategy_config_for_symbol(symbol, rules, payload).get("name") or default_strategy_name_from_rules(rules))


def default_source_for_market(market: str, payload: Dict[str, Any], rules: Dict[str, Any], root_cfg: Dict[str, Any]) -> str:
    if payload.get("source"):
        return str(payload["source"])
    rule_source = market_settings_for_market(market, rules).get("source")
    if rule_source:
        return str(rule_source)
    sensory_cfg = root_cfg.get("sensory", {})
    market_sources = sensory_cfg.get("market_sources", {})
    if market == "cn_equity":
        return str(market_sources.get("cn_equity") or "akshare")
    if market == "futures":
        return str(
            market_sources.get("futures")
            or market_sources.get("crypto")
            or rules.get("agent", {}).get("data_source")
            or sensory_cfg.get("default_source")
            or "ccxt"
        )
    return str(
        market_sources.get("crypto")
        or rules.get("agent", {}).get("data_source")
        or sensory_cfg.get("default_source")
        or "ccxt"
    )


def source_candidates_for_market(market: str, requested_source: str) -> List[str]:
    source = str(requested_source or "").strip().lower() or "auto"
    if source == "auto":
        if market == "cn_equity":
            candidates = ["sina", "akshare", "yfinance"]
        elif market == "futures":
            candidates = ["ccxt"]
        else:
            candidates = ["ccxt", "yfinance"]
    else:
        candidates = [source]
        if market == "crypto" and source == "ccxt":
            candidates.append("yfinance")
        if market == "cn_equity" and source in {"akshare", "tushare"}:
            candidates.append("sina")
            candidates.append("yfinance")

    deduped: List[str] = []
    for candidate in candidates:
        if candidate not in deduped:
            deduped.append(candidate)
    return deduped


def execution_mode_for_market(market: str, payload: Dict[str, Any], rules: Dict[str, Any], execution_cfg: Dict[str, Any]) -> str:
    explicit = str(payload.get("execution_mode") or "").strip()
    if explicit:
        return explicit

    rule_modes = rules.get("account", {}).get("market_modes", {})
    config_modes = execution_cfg.get("market_modes", {})
    if market in rule_modes:
        return str(rule_modes[market])
    if market in config_modes:
        return str(config_modes[market])

    return str(rules.get("account", {}).get("execution_mode") or execution_cfg.get("default", "ccxt"))


def normalize_market_symbol(symbol: str, source: str, market: str | None = None) -> str:
    market = market or ("cn_equity" if extract_cn_equity_code(symbol) else "crypto")
    if source == "akshare":
        return extract_cn_equity_code(symbol) or symbol
    if source == "sina":
        return extract_cn_equity_code(symbol) or symbol
    if source != "yfinance":
        if market == "futures" and source == "ccxt":
            return normalize_futures_symbol(symbol)
        return symbol
    if market == "cn_equity":
        code = extract_cn_equity_code(symbol)
        if not code:
            return symbol
        lowered = str(symbol).lower()
        if lowered.startswith("sh"):
            suffix = ".SS"
        elif lowered.startswith(("sz", "bj")):
            suffix = ".SZ"
        elif code.startswith(("5", "6", "9")):
            suffix = ".SS"
        else:
            suffix = ".SZ"
        return f"{code}{suffix}"
    if "/" not in symbol:
        return symbol
    base, quote = symbol.split("/", 1)
    if quote.upper() in {"USD", "USDT"}:
        return f"{base}-USD"
    return f"{base}-{quote}"


def build_data_manager(payload: Dict[str, Any], rules: Dict[str, Any], source_override: str | None = None):
    root_cfg = load_root_config()
    config = root_cfg.get("sensory", {})
    execution_cfg = root_cfg.get("execution", {})
    market = infer_market(payload, rules, payload.get("symbol", ""))
    source = source_override or default_source_for_market(market, payload, rules, root_cfg)
    kwargs: Dict[str, Any] = {}
    if source == "ccxt":
        kwargs["exchange_id"] = (
            config.get("sources", {})
            .get("ccxt", {})
            .get("exchange", rules.get("account", {}).get("exchange", "binance"))
        )
        if market == "futures":
            futures_cfg = execution_cfg.get("futures", {})
            kwargs["market_type"] = "swap"
            kwargs["testnet"] = bool(
                payload.get("testnet")
                if payload.get("testnet") is not None
                else futures_cfg.get("testnet", True)
            )
    elif source == "tushare":
        token = config.get("sources", {}).get("tushare", {}).get("token")
        if token:
            kwargs["token"] = token
    return create_data_manager(source, **kwargs)


def fetch_market_dataframe(payload: Dict[str, Any], rules: Dict[str, Any], symbol: str, interval: str, lookback: int):
    root_cfg = load_root_config()
    market = infer_market(payload, rules, symbol)
    requested_source = default_source_for_market(market, payload, rules, root_cfg)
    candidates = source_candidates_for_market(market, requested_source)

    last_error = None
    for source in candidates:
        manager = build_data_manager(payload, rules, source_override=source)
        source_symbol = normalize_market_symbol(symbol, source, market=market)
        try:
            df = manager.get_data(
                symbol=source_symbol,
                timeframe=interval,
                limit=lookback,
                use_cache=True,
            )
            if df is not None and not df.empty:
                return df, source
        except Exception as exc:
            last_error = exc

    if last_error is not None:
        raise last_error
    raise TradingBridgeError(f"No market data returned for {symbol}")


def macro_config(rules: Dict[str, Any], root_cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    root_cfg = root_cfg or load_root_config()
    return deep_merge(root_cfg.get("macro", {}), rules.get("macro", {}))


def macro_state_path(rules: Dict[str, Any], root_cfg: Dict[str, Any] | None = None) -> Path:
    config = macro_config(rules, root_cfg)
    return resolve_workspace_path(config.get("state_file"), DEFAULT_MACRO_STATE_PATH)


def optimization_config(rules: Dict[str, Any], root_cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    root_cfg = root_cfg or load_root_config()
    return deep_merge(root_cfg.get("optimization", {}), rules.get("optimization", {}))


def optimization_report_path(rules: Dict[str, Any], root_cfg: Dict[str, Any] | None = None) -> Path:
    cfg = optimization_config(rules, root_cfg)
    return resolve_workspace_path(cfg.get("report_file"), DEFAULT_OPTIMIZATION_REPORT_PATH)


def default_macro_state() -> Dict[str, Any]:
    return {
        "updated_at": None,
        "preferred_market": None,
        "risk_mode": "balanced",
        "risk_regime": "balanced",
        "position_scale_overrides": {},
        "benchmarks": {},
        "markets": {},
        "notes": [],
    }


def load_macro_state(rules: Dict[str, Any], root_cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    path = macro_state_path(rules, root_cfg)
    if not path.exists():
        return default_macro_state()
    with path.open("r", encoding="utf-8") as handle:
        raw_payload = json.load(handle)
    payload = deep_merge(default_macro_state(), raw_payload)
    if isinstance(raw_payload, dict) and "risk_regime" not in raw_payload:
        payload["risk_regime"] = payload.get("risk_mode", payload.get("risk_regime"))
    return payload


def save_macro_state(state: Dict[str, Any], rules: Dict[str, Any], root_cfg: Dict[str, Any] | None = None) -> Path:
    path = macro_state_path(rules, root_cfg)
    ensure_parent(path)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)
    return path


def annualization_factor(interval: str, market: str) -> float:
    text = str(interval or "1d").strip().lower()
    if text.endswith("d"):
        return 365.0 if market == "crypto" else 252.0
    if text.endswith("h"):
        try:
            hours = max(int(text[:-1] or "1"), 1)
        except ValueError:
            hours = 1
        base = 365.0 * 24.0 if market == "crypto" else 252.0 * 4.0
        return max(base / hours, 1.0)
    return 365.0 if market == "crypto" else 252.0


def summarize_macro_market(
    symbol: str,
    market: str,
    source: str,
    interval: str,
    df: pd.DataFrame,
    volatility_window: int,
) -> Dict[str, Any]:
    closes = pd.to_numeric(df["close"], errors="coerce").dropna()
    if len(closes) < 3:
        raise TradingBridgeError(f"Not enough candles to summarize macro state for {symbol}")

    returns = closes.pct_change().dropna()
    trailing = returns.tail(max(volatility_window, 2))
    rolling = returns.rolling(max(volatility_window, 2)).std().dropna()
    annual_factor = annualization_factor(interval, market) ** 0.5
    realized_vol = float(trailing.std() * annual_factor) if not trailing.empty else 0.0
    latest_rolling = float(rolling.iloc[-1]) if not rolling.empty else float(trailing.std() or 0.0)
    volatility_percentile = (
        float((rolling <= rolling.iloc[-1]).mean())
        if not rolling.empty
        else 0.5
    )

    change_5 = float(closes.iloc[-1] / closes.iloc[max(len(closes) - 6, 0)] - 1.0) if len(closes) > 5 else 0.0
    change_20 = float(closes.iloc[-1] / closes.iloc[max(len(closes) - 21, 0)] - 1.0) if len(closes) > 20 else change_5
    stability_score = change_20 - realized_vol

    return {
        "symbol": symbol,
        "market": market,
        "source": source,
        "interval": interval,
        "close": float(closes.iloc[-1]),
        "change_5": change_5,
        "change_20": change_20,
        "realized_volatility": realized_vol,
        "rolling_volatility": latest_rolling,
        "volatility_percentile": volatility_percentile,
        "stability_score": stability_score,
        "count": int(len(closes)),
    }


def macro_state_summary(state: Dict[str, Any], rules: Dict[str, Any], root_cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    cfg = macro_config(rules, root_cfg)
    refresh_minutes = int(cfg.get("refresh_interval_minutes", 15))
    updated_at = state.get("updated_at")
    stale = True
    if updated_at:
        try:
            stale = now_local(rules) - datetime.fromisoformat(updated_at) > timedelta(minutes=refresh_minutes)
        except Exception:
            stale = True
    return {
        "updated_at": updated_at,
        "stale": stale,
        "refresh_interval_minutes": refresh_minutes,
        "preferred_market": state.get("preferred_market"),
        "risk_mode": state.get("risk_mode"),
        "risk_regime": state.get("risk_regime") or state.get("risk_mode"),
        "position_scale_overrides": state.get("position_scale_overrides", {}),
        "benchmarks": state.get("benchmarks", {}),
        "markets": state.get("markets", {}),
        "state_file": str(macro_state_path(rules, root_cfg)),
    }


def optimization_report_summary(rules: Dict[str, Any], root_cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    cfg = optimization_config(rules, root_cfg)
    report_path = optimization_report_path(rules, root_cfg)
    summary = {
        "enabled": bool(cfg.get("enabled", True)),
        "report_path": str(report_path),
        "exists": report_path.exists(),
    }
    if not report_path.exists():
        return summary

    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
        return summary

    summary.update(
        {
            "last_run_at": payload.get("generated_at"),
            "changed": payload.get("changed", False),
            "change_count": len(payload.get("changes") or []),
            "analyzed_symbols": payload.get("summary", {}).get("analyzed_symbols", 0),
            "executed_trades": payload.get("summary", {}).get("executed_trades", 0),
            "pnl_samples": payload.get("summary", {}).get("pnl_samples", 0),
        }
    )
    return summary


def position_scale_for_market(market: str, rules: Dict[str, Any], root_cfg: Dict[str, Any] | None = None) -> float:
    state = load_macro_state(rules, root_cfg)
    scale = state.get("position_scale_overrides", {}).get(market, 1.0)
    try:
        return float(scale)
    except Exception:
        return 1.0


def review_universe_items(rules: Dict[str, Any], root_cfg: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    root_cfg = root_cfg or load_root_config()
    macro_state = load_macro_state(rules, root_cfg)
    preferred_market = str(macro_state.get("preferred_market") or "")
    auto_execution_markets = set(auto_execution_markets_from_rules(rules))
    groups = symbol_groups_from_rules(rules)
    items: List[Dict[str, Any]] = []

    for market in review_markets_from_rules(rules):
        settings = market_settings_for_market(market, rules)
        market_scale = position_scale_for_market(market, rules, root_cfg)
        session_status = market_session_status(market, rules, root_cfg)
        configured_execution_allowed = market in auto_execution_markets
        execution_allowed = configured_execution_allowed and bool(session_status.get("execution_allowed", True))
        execution_block_reason = ""
        if not configured_execution_allowed:
            execution_block_reason = "Auto execution is disabled for this market in auto_execution_markets"
        elif not execution_allowed:
            execution_block_reason = str(session_status.get("reason") or "Market session does not allow execution")
        execution_mode = execution_mode_for_market(
            market,
            {"market": market},
            rules,
            root_execution_config(),
        )
        for symbol in groups.get(market, []):
            strategy_cfg = strategy_config_for_symbol(symbol, rules, {"symbol": symbol, "market": market})
            base_quantity = order_size_for_symbol(symbol, rules, {"symbol": symbol, "market": market})
            symbol_scale_value = symbol_position_scale(symbol, rules, {"symbol": symbol, "market": market})
            scale = effective_position_scale(symbol, market, rules, root_cfg, {"symbol": symbol, "market": market})
            scaled_quantity = suggested_buy_quantity(symbol, market, rules, root_cfg, {"symbol": symbol, "market": market})
            items.append(
                {
                    "symbol": symbol,
                    "market": market,
                    "timeframe": settings.get("timeframe", "1h"),
                    "lookback": int(settings.get("lookback", 100)),
                    "source": settings.get("source"),
                    "execution_allowed": execution_allowed,
                    "configured_execution_allowed": configured_execution_allowed,
                    "market_session": session_status,
                    "execution_block_reason": execution_block_reason,
                    "execution_mode": execution_mode,
                    "position_scale": scale,
                    "market_position_scale": market_scale,
                    "symbol_position_scale": symbol_scale_value,
                    "base_quantity": base_quantity,
                    "suggested_buy_quantity": scaled_quantity,
                    "strategy_name": strategy_cfg.get("name", default_strategy_name_from_rules(rules)),
                    "strategy_kind": strategy_cfg.get("kind", strategy_cfg.get("name", "trend_following")),
                }
            )

    items.sort(key=lambda item: (0 if item["market"] == preferred_market else 1, item["market"], item["symbol"]))
    return items


def build_executor(payload: Dict[str, Any], rules: Dict[str, Any]):
    execution_cfg = root_execution_config()
    market = infer_market(payload, rules, payload.get("symbol", ""))
    mode = execution_mode_for_market(market, payload, rules, execution_cfg)
    kwargs: Dict[str, Any] = {}
    if mode in {"ccxt", "ccxt_futures"}:
        ccxt_cfg = execution_cfg.get("ccxt", {})
        if market == "futures" or mode == "ccxt_futures":
            futures_cfg = execution_cfg.get("futures", {})
            trading_cfg = trading_runtime_config(rules, load_root_config())
            futures_runtime = trading_cfg.get("futures", {})
            kwargs["exchange_id"] = futures_cfg.get("exchange", ccxt_cfg.get("exchange", "binance"))
            kwargs["testnet"] = bool(payload.get("testnet") if payload.get("testnet") is not None else futures_cfg.get("testnet", True))
            kwargs["market_type"] = "swap"
            kwargs["default_leverage"] = resolve_requested_leverage(payload, futures_runtime)
            kwargs["max_leverage"] = int(futures_runtime.get("max_leverage", kwargs["default_leverage"]))
            kwargs["quote_asset"] = str(futures_cfg.get("quote_asset") or "USDT")
            if futures_cfg.get("api_key"):
                kwargs["api_key"] = futures_cfg.get("api_key")
            if futures_cfg.get("secret"):
                kwargs["secret"] = futures_cfg.get("secret")
            mode = "ccxt_futures"
        else:
            kwargs["exchange_id"] = rules.get("account", {}).get("exchange", ccxt_cfg.get("exchange", "binance"))
            kwargs["testnet"] = bool(rules.get("account", {}).get("testnet", ccxt_cfg.get("testnet", True)))
            if ccxt_cfg.get("api_key"):
                kwargs["api_key"] = ccxt_cfg.get("api_key")
            if ccxt_cfg.get("secret"):
                kwargs["secret"] = ccxt_cfg.get("secret")
    elif mode == "easytrader":
        easy_cfg = execution_cfg.get("easytrader", {})
        kwargs["client"] = str(payload.get("broker_client") or easy_cfg.get("client") or "tonghuashun")
        if easy_cfg.get("username"):
            kwargs["username"] = easy_cfg.get("username")
        if easy_cfg.get("password"):
            kwargs["password"] = easy_cfg.get("password")
        if easy_cfg.get("host"):
            kwargs["host"] = easy_cfg.get("host")
        if easy_cfg.get("port") is not None:
            kwargs["port"] = int(easy_cfg.get("port"))
        if easy_cfg.get("prepare_path"):
            kwargs["prepare_path"] = easy_cfg.get("prepare_path")
    elif mode == "ths_bridge":
        ths_cfg = execution_cfg.get("ths_bridge", {})
        kwargs["workspace"] = str(payload.get("bridge_workspace") or ths_cfg.get("workspace") or OPENCLAW_ROOT / "workspace")
        if payload.get("ths_exe_path") or ths_cfg.get("exe_path"):
            kwargs["exe_path"] = str(payload.get("ths_exe_path") or ths_cfg.get("exe_path"))
        if ths_cfg.get("timeout_sec") is not None:
            kwargs["timeout_sec"] = int(ths_cfg.get("timeout_sec"))
        if ths_cfg.get("poll_interval_sec") is not None:
            kwargs["poll_interval_sec"] = float(ths_cfg.get("poll_interval_sec"))
        if ths_cfg.get("heartbeat_max_age_sec") is not None:
            kwargs["heartbeat_max_age_sec"] = int(ths_cfg.get("heartbeat_max_age_sec"))
        kwargs["require_interactive_session"] = bool(ths_cfg.get("require_interactive_session", True))
    return create_executor(mode, **kwargs)


def resolve_price(payload: Dict[str, Any], rules: Dict[str, Any], market_data: pd.DataFrame | None = None) -> float:
    if payload.get("price") is not None:
        return float(payload["price"])
    if market_data is not None and not market_data.empty:
        return float(market_data["close"].iloc[-1])
    root_cfg = load_root_config()
    market = infer_market(payload, rules, payload["symbol"])
    requested_source = default_source_for_market(market, payload, rules, root_cfg)
    candidates = [requested_source]
    if "yfinance" not in candidates:
        candidates.append("yfinance")
    for source in candidates:
        try:
            manager = build_data_manager(payload, rules, source_override=source)
            quote = manager.get_quote(normalize_market_symbol(payload["symbol"], source, market=market))
            for key in ["last", "price", "close"]:
                if quote.get(key) is not None:
                    return float(quote[key])
        except Exception:
            continue
    market_result = action_get_market_data({
        "symbol": payload["symbol"],
        "interval": rules.get("agent", {}).get("timeframe", "1h"),
        "lookback": 2,
        "source": candidates[-1],
        "market": market,
        "rules_file": payload.get("rules_file"),
    })
    return float(market_result["market_data"][-1]["close"])


def lot_size_for_market(market: str) -> int:
    if market != "cn_equity":
        return 1
    easy_cfg = root_execution_config().get("easytrader", {})
    return max(int(easy_cfg.get("lot_size", 100) or 100), 1)


def normalize_order_quantity(payload: Dict[str, Any], rules: Dict[str, Any], quantity: Any, side: str) -> float:
    market = infer_market(payload, rules, payload.get("symbol", ""))
    value = float(quantity)
    if market != "cn_equity":
        return value

    whole_shares = max(int(value), 0)
    if side.lower() == "buy":
        lot_size = lot_size_for_market(market)
        return float((whole_shares // lot_size) * lot_size)
    return float(whole_shares)


def assumed_equity_for_market(payload: Dict[str, Any], rules: Dict[str, Any]) -> float:
    market = infer_market(payload, rules, payload.get("symbol", ""))
    by_market = rules.get("account", {}).get("assumed_equity_by_market", {})
    if market in by_market:
        return float(by_market[market])
    return float(rules["account"].get("assumed_equity_usdt", 10000))


def parse_blackout(raw_range: str):
    start_raw, end_raw = raw_range.split("-", 1)
    return start_raw.strip(), end_raw.strip()


def in_blackout_window(symbol: str, rules: Dict[str, Any]) -> bool:
    if "/" in symbol:
        return False
    restrictions = load_root_config().get("gatekeeper", {}).get("restrictions", {})
    windows = restrictions.get("blacklist_hours", [])
    if not windows:
        return False

    current = now_local(rules).strftime("%H:%M")
    for item in windows:
        start_text, end_text = parse_blackout(item)
        if start_text <= current <= end_text:
            return True
    return False


def update_failure_state(state: Dict[str, Any], rules: Dict[str, Any], message: str) -> Dict[str, Any]:
    risk = rules.get("risk", {})
    threshold = int(risk.get("consecutive_failures_to_pause", 3))
    pause_minutes = int(risk.get("pause_minutes_after_failures", 30))
    state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1
    if state["consecutive_failures"] >= threshold:
        state["paused_until"] = (now_local(rules) + timedelta(minutes=pause_minutes)).isoformat()
        send_feishu_text(
            f"[Trading] Consecutive failures reached {state['consecutive_failures']}."
            f" Trading is paused until {state['paused_until']}. Last error: {message}"
        )
    return state


def reset_failure_state(state: Dict[str, Any]) -> Dict[str, Any]:
    state["consecutive_failures"] = 0
    state["paused_until"] = None
    return state


def notification_enabled(root_cfg: Dict[str, Any], flag: str, default: bool = True) -> bool:
    notification_cfg = root_cfg.get("notification", {})
    if not notification_cfg.get("enabled", True):
        return False
    return bool(notification_cfg.get(flag, default))


def error_notification_cooldown_minutes(root_cfg: Dict[str, Any]) -> int:
    notification_cfg = root_cfg.get("notification", {})
    try:
        return max(int(notification_cfg.get("error_cooldown_minutes", 30) or 30), 0)
    except Exception:
        return 30


def error_notification_key(kind: str, symbol: str = "", side: str = "", message: str = "") -> str:
    message_text = " ".join(str(message or "").strip().lower().split())
    return "|".join(
        [
            str(kind or "").strip().lower(),
            str(symbol or "").strip().upper(),
            str(side or "").strip().lower(),
            message_text[:240],
        ]
    )


def should_send_error_notification(state: Dict[str, Any], root_cfg: Dict[str, Any], key: str, now_ts: datetime) -> bool:
    cooldown_minutes = error_notification_cooldown_minutes(root_cfg)
    cache_root = state.setdefault("notification_state", {})
    error_cache = cache_root.setdefault("error_notifications", {})
    if cooldown_minutes <= 0:
        error_cache[key] = now_ts.isoformat()
        return True

    cutoff = now_ts - timedelta(minutes=cooldown_minutes)
    stale_keys: List[str] = []
    for cache_key, cache_value in list(error_cache.items()):
        try:
            cached_at = datetime.fromisoformat(str(cache_value))
        except Exception:
            stale_keys.append(cache_key)
            continue
        if cached_at < cutoff:
            stale_keys.append(cache_key)
    for stale_key in stale_keys:
        error_cache.pop(stale_key, None)

    last_sent_raw = error_cache.get(key)
    if last_sent_raw:
        try:
            last_sent_at = datetime.fromisoformat(str(last_sent_raw))
            if last_sent_at >= cutoff:
                return False
        except Exception:
            pass

    error_cache[key] = now_ts.isoformat()
    return True


def send_operator_notification(message: str, root_cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    config = root_cfg or load_root_config()
    gatekeeper_cfg = config.get("gatekeeper", {})
    attempts: List[Dict[str, Any]] = []

    feishu_targets = collect_feishu_targets(gatekeeper_cfg)
    if feishu_targets:
        target = feishu_targets[0]
        result = send_native_feishu_text(target, message)
        payload = result.get("payload", {}) if isinstance(result, dict) else {}
        message_id = (
            payload.get("payload", {})
            .get("result", {})
            .get("messageId")
        )
        attempts.append(
            {
                "channel": "feishu",
                "target": target,
                "ok": bool(result.get("ok")),
                "message_id": message_id or "",
            }
        )
        if result.get("ok"):
            return {
                "ok": True,
                "channel": "feishu",
                "target": target,
                "attempts": attempts,
            }

    webhook = str(gatekeeper_cfg.get("feishu", {}).get("webhook", "")).strip()
    if webhook:
        try:
            import requests

            first_line = message.split("\n", 1)[0] if message else ""
            body_lines = message.split("\n")[1:] if "\n" in message else []
            body_text = "\n".join(body_lines).strip()
            card = {
                "elements": [],
            }
            if first_line:
                color = "red" if "异常" in first_line or "错误" in first_line else "blue"
                card["header"] = {
                    "title": {"tag": "plain_text", "content": first_line},
                    "template": color,
                }
            if body_text:
                card["elements"].append({
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": body_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"),
                    },
                })
            response = requests.post(
                webhook,
                json={"msg_type": "interactive", "card": card},
                timeout=8,
            )
            attempts.append(
                {
                    "channel": "feishu_webhook",
                    "target": webhook,
                    "ok": response.status_code == 200,
                    "status_code": response.status_code,
                }
            )
            if response.status_code == 200:
                return {
                    "ok": True,
                    "channel": "feishu_webhook",
                    "target": webhook,
                    "attempts": attempts,
                }
        except Exception as exc:
            attempts.append(
                {
                    "channel": "feishu_webhook",
                    "target": webhook,
                    "ok": False,
                    "error": str(exc),
                }
            )

    return {"ok": False, "attempts": attempts}


def send_feishu_text(message: str) -> None:
    send_operator_notification(message)


def format_decimal(value: Any, digits: int = 6) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return str(value)


def build_order_result_message(
    *,
    title: str,
    symbol: str,
    side: str,
    quantity: Any,
    price: Any,
    request_id: str = "",
    strategy_name: str = "",
    reason: str = "",
    order: Dict[str, Any] | None = None,
) -> str:
    action_text = "买入" if side == "buy" else "卖出"
    order_payload = order or {}
    order_id = (
        order_payload.get("id")
        or order_payload.get("order_id")
        or order_payload.get("orderId")
        or ""
    )
    order_status = (
        order_payload.get("status")
        or order_payload.get("order_status")
        or order_payload.get("orderStatus")
        or ""
    )
    average_price = (
        order_payload.get("avg_price")
        or order_payload.get("average")
        or order_payload.get("avgPrice")
        or price
    )
    filled_quantity = (
        order_payload.get("filled_quantity")
        or order_payload.get("filled")
        or order_payload.get("executedQty")
        or quantity
    )

    header = f"{title} {symbol} {action_text} {format_decimal(quantity)} @ {format_decimal(price)}"
    lines = [
        header,
        f"请求ID: {request_id}" if request_id else "",
        f"标的: {symbol}",
        f"操作: {action_text}",
        f"数量: {format_decimal(quantity)}",
        f"参考价格: {format_decimal(price)}",
        f"策略: {strategy_name}" if strategy_name else "",
        f"原因: {reason}" if reason else "",
        f"订单号: {order_id}" if order_id else "",
        f"成交均价: {format_decimal(average_price)}" if average_price not in ("", None) else "",
        f"成交数量: {format_decimal(filled_quantity)}" if filled_quantity not in ("", None) else "",
        f"订单状态: {order_status}" if order_status else "",
    ]
    return "\n".join(line for line in lines if line)


def approval_recovery_config(root_cfg: Dict[str, Any]) -> Dict[str, Any]:
    gatekeeper_cfg = root_cfg.get("gatekeeper", {})
    recovery_cfg = gatekeeper_cfg.get("recovery", {})
    if not isinstance(recovery_cfg, dict):
        recovery_cfg = {}
    return {
        "enabled": bool(recovery_cfg.get("enabled", True)),
        "max_age_minutes": int(recovery_cfg.get("max_age_minutes", 720)),
        "grace_seconds": int(recovery_cfg.get("grace_seconds", 30)),
        "mark_timeouts": bool(recovery_cfg.get("mark_timeouts", True)),
    }


def load_approval_requests() -> Dict[str, Dict[str, Any]]:
    state = load_approval_json(APPROVAL_STATE_PATH)
    requests_payload = state.get("requests")
    if isinstance(requests_payload, dict):
        return requests_payload
    return {}


def update_approval_request(request_id: str, **updates: Any) -> Dict[str, Any]:
    state = load_approval_json(APPROVAL_STATE_PATH)
    requests_payload = state.get("requests")
    if not isinstance(requests_payload, dict):
        requests_payload = {}
    current = dict(requests_payload.get(request_id) or {})
    current["request_id"] = request_id
    current.update({key: value for key, value in updates.items() if value is not None})
    current["updated_at"] = approval_now_ts()
    requests_payload[request_id] = current
    state["requests"] = requests_payload
    save_approval_json(APPROVAL_STATE_PATH, state)
    return current


def parse_approval_state_timestamp(value: Any, rules: Dict[str, Any]) -> datetime | None:
    parsed = parse_iso_timestamp(value)
    if parsed is not None:
        return parsed.astimezone(resolve_timezone(rules))
    text = str(value or "").strip()
    if not text:
        return None
    try:
        local_dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None
    return local_dt.replace(tzinfo=resolve_timezone(rules))


def summarize_pending_approvals(root_cfg: Dict[str, Any], rules: Dict[str, Any]) -> Dict[str, Any]:
    requests_payload = load_approval_requests()
    timeout_seconds = int(root_cfg.get("gatekeeper", {}).get("timeout_seconds", 300))
    now_ts_local = now_local(rules)
    total_pending = 0
    stale_pending = 0

    for record in requests_payload.values():
        if str(record.get("status", "")).strip().lower() != "pending":
            continue
        total_pending += 1
        created_at = parse_approval_state_timestamp(record.get("created_at"), rules)
        if created_at and (now_ts_local - created_at).total_seconds() > timeout_seconds:
            stale_pending += 1

    return {
        "total_pending": total_pending,
        "stale_pending": stale_pending,
    }


def approval_record_to_payload(record: Dict[str, Any], rules_file: str = "") -> Dict[str, Any]:
    metadata = record.get("metadata") or {}
    payload = {
        "symbol": record.get("symbol"),
        "side": record.get("side"),
        "quantity": record.get("quantity"),
        "price": record.get("price"),
        "stop_price": record.get("stop_price") or metadata.get("stop_price"),
        "leverage": record.get("leverage") or metadata.get("leverage"),
        "order_type": record.get("order_type") or "market",
        "reason": record.get("reason") or "",
        "strategy": record.get("strategy") or "",
        "market": metadata.get("market") or record.get("market"),
        "execution_mode": metadata.get("execution_mode") or record.get("execution_mode"),
        "broker_client": metadata.get("broker_client") or record.get("broker_client"),
        "bridge_workspace": metadata.get("bridge_workspace") or record.get("bridge_workspace"),
        "ths_exe_path": metadata.get("ths_exe_path") or record.get("ths_exe_path"),
    }
    if rules_file:
        payload["rules_file"] = rules_file
    return payload


def persist_rejected_result(
    *,
    payload: Dict[str, Any],
    rules: Dict[str, Any],
    root_cfg: Dict[str, Any],
    request_id: str,
    status: str,
    message: str,
    details: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    event = {
        "timestamp": now_local(rules).isoformat(),
        "status": status,
        "request_id": request_id,
        "symbol": payload.get("symbol"),
        "side": payload.get("side"),
        "message": message,
        "details": details or {},
    }
    with runtime_lock(rules):
        append_decision(event, rules)
        state = reset_failure_state(load_state(rules))
        save_state(state, rules)
    if notification_enabled(root_cfg, "on_order_rejected", True):
        send_operator_notification(
            build_order_result_message(
                title="[OpenClaw 交易结果]",
                request_id=request_id,
                symbol=payload.get("symbol", ""),
                side=str(payload.get("side", "")),
                quantity=payload.get("quantity"),
                price=payload.get("price"),
                strategy_name=str(payload.get("strategy", "")),
                reason=message,
            ),
            root_cfg=root_cfg,
        )
    return {
        "request_id": request_id,
        "status": status,
        "message": message,
    }


def execute_approved_order(
    *,
    payload: Dict[str, Any],
    rules: Dict[str, Any],
    root_cfg: Dict[str, Any],
    request_id: str,
    risk_result: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    symbol = payload["symbol"]
    side = str(payload["side"]).lower()
    market = infer_market(payload, rules, symbol)
    reason = payload.get("reason", "")
    strategy_name = str(payload.get("strategy") or strategy_name_for_symbol(symbol, rules, payload))
    order_type = payload.get("order_type", "market")
    price = resolve_price(payload, rules)
    quantity = normalize_order_quantity(payload, rules, payload["quantity"], side)
    leverage = payload.get("leverage")
    risk_result = risk_result or action_check_risk(payload)

    executor = build_executor(payload, rules)
    try:
        if not executor.connect():
            raise TradingBridgeError(f"Executor connection failed for {symbol}")
        if side == "buy":
            order = executor.buy(symbol, quantity, price=price, order_type=order_type)
        else:
            order = executor.sell(symbol, quantity, price=price, order_type=order_type)
    finally:
        try:
            executor.disconnect()
        except Exception:
            pass

    order_payload = to_jsonable(order)
    fill_price = extract_order_price_value(order_payload, fallback=price)
    filled_quantity = extract_order_quantity_value(order_payload, fallback=quantity)
    event = {
        "timestamp": now_local(rules).isoformat(),
        "status": "executed",
        "request_id": request_id,
        "symbol": symbol,
        "market": market,
        "side": side,
        "quantity": quantity,
        "price": price,
        "leverage": leverage,
        "message": reason or "Order executed after approval",
        "order": order_payload,
        "risk": risk_result,
    }
    with runtime_lock(rules):
        state = load_state(rules)
        committed = state.setdefault("daily_risk_committed", {})
        open_positions = state.setdefault("open_positions", {})
        day = today_key(rules)
        committed[day] = float(committed.get(day, 0.0)) + float(risk_result["metrics"]["estimated_loss"])
        if market == "crypto":
            if side == "buy":
                strategy_cfg = strategy_config_for_symbol(symbol, rules, payload)
                atr_period = max(int(strategy_cfg.get("atr_period", 14) or 14), 1)
                atr_multiplier = float(strategy_cfg.get("atr_multiplier", 2.0) or 2.0)
                settings = market_settings_for_symbol(symbol, rules, payload)
                interval = payload.get("interval") or settings.get("timeframe") or rules.get("agent", {}).get("timeframe", "1h")
                lookback = max(
                    int(payload.get("lookback") or settings.get("lookback") or rules.get("agent", {}).get("lookback", 100)),
                    atr_period + 20,
                )
                atr_value = None
                try:
                    stop_df, _ = fetch_market_dataframe(payload, rules, symbol, interval, lookback)
                    atr_value = latest_atr_value(stop_df, atr_period)
                except Exception:
                    atr_value = None
                resolved_fill_price = fill_price if fill_price is not None else price
                resolved_quantity = filled_quantity if filled_quantity is not None else quantity
                if resolved_fill_price not in (None, "") and resolved_quantity not in (None, ""):
                    stop_price = (
                        max(float(resolved_fill_price) - (atr_multiplier * atr_value), 0.0)
                        if atr_value is not None
                        else max(
                            float(resolved_fill_price)
                            * (1.0 - float(rules.get("risk", {}).get("default_stop_loss_pct", 0.02))),
                            0.0,
                        )
                    )
                    open_positions[symbol] = {
                        "entry_price": float(resolved_fill_price),
                        "quantity": float(resolved_quantity),
                        "stop_loss": float(stop_price),
                        "atr": atr_value,
                        "atr_period": atr_period,
                        "atr_multiplier": atr_multiplier,
                        "strategy_name": strategy_name,
                        "updated_at": now_local(rules).isoformat(),
                    }
            else:
                open_positions.pop(symbol, None)
        state = reset_failure_state(state)
        append_decision(event, rules)
        save_state(state, rules)

    if str(request_id or "").strip():
        update_approval_request(
            request_id,
            status="executed",
            executed_at=approval_now_ts(),
            order=order_payload,
        )
    if notification_enabled(root_cfg, "on_order_filled", True):
        send_operator_notification(
            build_order_result_message(
                title="[OpenClaw 交易结果]",
                request_id=request_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
                strategy_name=strategy_name,
                reason=reason or "审批通过后已执行",
                order=order_payload,
            ),
            root_cfg=root_cfg,
        )

    return {
        "ok": True,
        "approved": True,
        "executed": True,
        "request_id": request_id,
        "quantity": quantity,
        "price": price,
        "leverage": leverage,
        "order": order_payload,
        "risk": risk_result,
    }


def get_position_quantity(symbol: str, positions: List[Any]) -> float:
    base_asset = symbol.split("/")[0]
    cn_code = extract_cn_equity_code(symbol)
    futures_symbol = normalize_futures_symbol(symbol)
    for position in positions:
        position_symbol = getattr(position, "symbol", "")
        normalized_position = str(position_symbol)
        if cn_code and extract_cn_equity_code(normalized_position) == cn_code:
            return float(getattr(position, "quantity", 0) or 0)
        if normalized_position in {symbol, base_asset, futures_symbol}:
            return float(getattr(position, "quantity", 0) or 0)
        if normalized_position.startswith(f"{symbol}:") or normalized_position.startswith(f"{futures_symbol}:"):
            return float(getattr(position, "quantity", 0) or 0)
        if ":" in normalized_position and normalized_position.split(":", 1)[0] in {symbol, futures_symbol}:
            return float(getattr(position, "quantity", 0) or 0)
    return 0.0


def default_snapshot_symbol_for_market(market: str, rules: Dict[str, Any]) -> str:
    preferred = {
        "crypto": "BTC/USDT",
        "futures": "BTC/USDT",
        "cn_equity": "510300",
    }
    for symbol in symbol_groups_from_rules(rules).get(market, []) or []:
        text = str(symbol or "").strip()
        if text:
            return text
    for symbol in rules.get("agent", {}).get("symbols", []) or []:
        text = str(symbol or "").strip()
        if text and infer_market({"market": market, "symbol": text}, rules, text) == market:
            return text
    return preferred.get(market, "BTC/USDT")


def tracked_assets_for_market(market: str, rules: Dict[str, Any]) -> set[str]:
    assets: set[str] = set()
    if market in {"crypto", "futures"}:
        for symbol in symbol_groups_from_rules(rules).get("crypto", []) or []:
            text = str(symbol or "").strip().upper()
            if not text or "/" not in text:
                continue
            base, quote = text.split("/", 1)
            assets.add(base)
            assets.add(quote.split(":", 1)[0])
        assets.add("USDT")
    elif market == "cn_equity":
        for symbol in symbol_groups_from_rules(rules).get("cn_equity", []) or []:
            code = extract_cn_equity_code(str(symbol or ""))
            if code:
                assets.add(code)
    return assets


def serialize_position_snapshot(position: Any) -> Dict[str, Any]:
    return {
        "symbol": str(getattr(position, "symbol", "") or ""),
        "quantity": float(getattr(position, "quantity", 0) or 0),
        "avg_price": float(getattr(position, "avg_price", 0) or 0),
        "current_price": float(getattr(position, "current_price", 0) or 0),
        "unrealized_pnl": float(getattr(position, "unrealized_pnl", 0) or 0),
        "realized_pnl": float(getattr(position, "realized_pnl", 0) or 0),
    }


def action_get_portfolio_snapshot(payload: Dict[str, Any]) -> Dict[str, Any]:
    rules = load_rules(payload)
    root_cfg = load_root_config()
    requested_market = str(payload.get("market") or "crypto").strip().lower()
    market_aliases = {
        "spot": "crypto",
        "cryptocurrency": "crypto",
        "stock": "cn_equity",
        "a_share": "cn_equity",
        "a_shares": "cn_equity",
        "ashare": "cn_equity",
        "ashares": "cn_equity",
    }
    market = market_aliases.get(requested_market, requested_market)
    if market not in {"crypto", "cn_equity", "futures"}:
        market = infer_market(payload, rules, str(payload.get("symbol") or ""))
    symbol = str(payload.get("symbol") or default_snapshot_symbol_for_market(market, rules)).strip()
    include_positions = bool(payload.get("include_positions", True))
    include_zero_positions = bool(payload.get("include_zero_positions", False))
    max_positions = max(int(payload.get("max_positions", 10) or 10), 1)

    executor_payload = dict(payload)
    executor_payload["market"] = market
    if symbol:
        executor_payload["symbol"] = symbol

    balance: Dict[str, float] = {}
    positions: List[Any] = []
    connected = False
    error_message = ""
    executor = build_executor(executor_payload, rules)
    try:
        connected = bool(executor.connect())
        if not connected:
            error_message = "Executor connection failed"
        else:
            balance = executor.get_balance() or {}
            if include_positions:
                positions = executor.get_positions() or []
    except Exception as exc:
        error_message = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            executor.disconnect()
        except Exception:
            pass

    serialized_positions = [serialize_position_snapshot(position) for position in positions]
    if not include_zero_positions:
        serialized_positions = [
            position for position in serialized_positions if abs(float(position.get("quantity") or 0)) > 0
        ]
    tracked_assets = tracked_assets_for_market(market, rules)
    serialized_positions.sort(
        key=lambda item: (
            1 if str(item.get("symbol") or "").strip().upper() in tracked_assets else 0,
            abs(float(item.get("current_price") or 0) * float(item.get("quantity") or 0)),
            abs(float(item.get("quantity") or 0)),
        ),
        reverse=True,
    )

    balance_payload = {
        "total_asset": float(balance.get("total_asset") or 0),
        "cash": float(balance.get("cash") or 0),
        "market_value": float(balance.get("market_value") or 0),
        "pnl": float(balance.get("pnl") or 0),
    }
    if not connected and not balance_payload["total_asset"]:
        balance_payload["total_asset"] = float(assumed_equity_for_market(executor_payload, rules))
        if market != "cn_equity":
            balance_payload["cash"] = balance_payload["total_asset"]

    return {
        "ok": True,
        "market": market,
        "symbol_hint": symbol,
        "connected": connected,
        "error": error_message,
        "execution_mode": execution_mode_for_market(market, executor_payload, rules, root_execution_config()),
        "balance": balance_payload,
        "positions": serialized_positions[:max_positions],
        "tracked_positions": [
            position
            for position in serialized_positions
            if str(position.get("symbol") or "").strip().upper() in tracked_assets
        ][:max_positions],
        "position_count": len(serialized_positions),
        "trading": trading_runtime_config(rules, root_cfg),
        "market_session": market_session_status(market, rules, root_cfg),
        "rules_path": str(get_rules_path(payload)),
        "as_of": now_local(rules).isoformat(),
    }


def action_get_trading_state(payload: Dict[str, Any]) -> Dict[str, Any]:
    rules = load_rules(payload)
    root_cfg = load_root_config()
    state = load_state(rules)
    macro_state = load_macro_state(rules, root_cfg)
    market_sessions = {
        market: market_session_status(market, rules, root_cfg)
        for market in review_markets_from_rules(rules)
    }
    paused_until = state.get("paused_until")
    paused = False
    if paused_until:
        paused = now_local(rules) < datetime.fromisoformat(paused_until)

    return {
        "ok": True,
        "state": state,
        "paused": paused,
        "paused_until": paused_until,
        "trading": trading_runtime_config(rules, root_cfg),
        "symbols": rules.get("agent", {}).get("symbols", []),
        "symbol_groups": symbol_groups_from_rules(rules),
        "review_markets": review_markets_from_rules(rules),
        "auto_execution_markets": auto_execution_markets_from_rules(rules),
        "market_sessions": market_sessions,
        "market_settings": rules.get("agent", {}).get("market_settings", {}),
        "timeframe": rules.get("agent", {}).get("timeframe", "1h"),
        "lookback": rules.get("agent", {}).get("lookback", 100),
        "rules_path": str(get_rules_path(payload)),
        "approval_recovery": {
            **approval_recovery_config(root_cfg),
            **summarize_pending_approvals(root_cfg, rules),
        },
        "macro_state": macro_state_summary(macro_state, rules, root_cfg),
        "auto_discover": auto_discover_summary(rules, root_cfg),
        "optimization": optimization_report_summary(rules, root_cfg),
    }


def action_get_review_universe(payload: Dict[str, Any]) -> Dict[str, Any]:
    rules = load_rules(payload)
    root_cfg = load_root_config()
    macro_state = load_macro_state(rules, root_cfg)
    items = review_universe_items(rules, root_cfg)
    market_sessions = {
        market: market_session_status(market, rules, root_cfg)
        for market in review_markets_from_rules(rules)
    }
    return {
        "ok": True,
        "preferred_market": macro_state.get("preferred_market"),
        "risk_mode": macro_state.get("risk_mode"),
        "risk_regime": macro_state.get("risk_regime") or macro_state.get("risk_mode"),
        "trading": trading_runtime_config(rules, root_cfg),
        "review_markets": review_markets_from_rules(rules),
        "auto_execution_markets": auto_execution_markets_from_rules(rules),
        "market_sessions": market_sessions,
        "universe": items,
    }


def action_update_macro_state(payload: Dict[str, Any]) -> Dict[str, Any]:
    rules = load_rules(payload)
    root_cfg = load_root_config()
    cfg = macro_config(rules, root_cfg)
    lookback = int(payload.get("lookback") or cfg.get("lookback", 120))
    volatility_window = int(payload.get("volatility_window") or cfg.get("volatility_window", 20))
    high_vol_percentile = float(payload.get("high_vol_percentile") or cfg.get("high_vol_percentile", 0.8))
    reduction = float(
        payload.get("position_scale_reduction_on_high_vol")
        or cfg.get("position_scale_reduction_on_high_vol", 0.5)
    )

    cn_symbol = str(payload.get("cn_equity_symbol") or cfg.get("cn_equity_symbol") or "510300")
    cn_interval = str(payload.get("cn_equity_interval") or cfg.get("cn_equity_interval") or "1d")
    cn_source = str(payload.get("cn_equity_source") or cfg.get("cn_equity_source") or "akshare")
    crypto_symbol = str(payload.get("crypto_symbol") or cfg.get("crypto_symbol") or "BTC/USDT")
    crypto_interval = str(payload.get("crypto_interval") or cfg.get("crypto_interval") or "1d")
    crypto_source = str(payload.get("crypto_source") or cfg.get("crypto_source") or "ccxt")

    cn_df, resolved_cn_source = fetch_market_dataframe(
        {
            "symbol": cn_symbol,
            "interval": cn_interval,
            "lookback": lookback,
            "source": cn_source,
            "market": "cn_equity",
        },
        rules,
        cn_symbol,
        cn_interval,
        lookback,
    )
    crypto_df, resolved_crypto_source = fetch_market_dataframe(
        {
            "symbol": crypto_symbol,
            "interval": crypto_interval,
            "lookback": lookback,
            "source": crypto_source,
            "market": "crypto",
        },
        rules,
        crypto_symbol,
        crypto_interval,
        lookback,
    )

    cn_market = summarize_macro_market(
        symbol=cn_symbol,
        market="cn_equity",
        source=resolved_cn_source,
        interval=cn_interval,
        df=cn_df,
        volatility_window=volatility_window,
    )
    crypto_market = summarize_macro_market(
        symbol=crypto_symbol,
        market="crypto",
        source=resolved_crypto_source,
        interval=crypto_interval,
        df=crypto_df,
        volatility_window=volatility_window,
    )

    cn_high_vol = cn_market["volatility_percentile"] >= high_vol_percentile
    crypto_high_vol = crypto_market["volatility_percentile"] >= high_vol_percentile
    preferred_market = (
        "cn_equity"
        if cn_market["stability_score"] >= crypto_market["stability_score"]
        else "crypto"
    )
    risk_mode = "defensive" if cn_high_vol or crypto_high_vol else "balanced"
    position_scale_overrides = {
        "cn_equity": reduction if cn_high_vol else 1.0,
        "crypto": reduction if crypto_high_vol else 1.0,
    }

    notes: List[str] = []
    if cn_high_vol:
        notes.append(
            f"CN equity volatility percentile {cn_market['volatility_percentile']:.2f} is above the {high_vol_percentile:.2f} threshold."
        )
    if crypto_high_vol:
        notes.append(
            f"Crypto volatility percentile {crypto_market['volatility_percentile']:.2f} is above the {high_vol_percentile:.2f} threshold."
        )
    if not notes:
        notes.append("Both benchmark markets are inside the normal volatility regime.")

    state = {
        "updated_at": now_local(rules).isoformat(),
        "preferred_market": preferred_market,
        "risk_mode": risk_mode,
        "risk_regime": risk_mode,
        "position_scale_overrides": position_scale_overrides,
        "benchmarks": {
            "cn_equity": cn_symbol,
            "crypto": crypto_symbol,
        },
        "markets": {
            "cn_equity": cn_market,
            "crypto": crypto_market,
        },
        "notes": notes,
    }
    path = save_macro_state(state, rules, root_cfg)
    return {
        "ok": True,
        "macro_state": state,
        "summary": macro_state_summary(state, rules, root_cfg),
        "state_file": str(path),
    }


def action_get_macro_state(payload: Dict[str, Any]) -> Dict[str, Any]:
    rules = load_rules(payload)
    root_cfg = load_root_config()
    state = load_macro_state(rules, root_cfg)
    return {
        "ok": True,
        "macro_state": state,
        "summary": macro_state_summary(state, rules, root_cfg),
    }


def action_get_market_data(payload: Dict[str, Any]) -> Dict[str, Any]:
    rules = load_rules(payload)
    symbol = payload["symbol"]
    market = infer_market(payload, rules, symbol)
    settings = market_settings_for_symbol(symbol, rules, payload)
    interval = payload.get("interval") or settings.get("timeframe") or rules.get("agent", {}).get("timeframe", "1h")
    lookback = int(payload.get("lookback") or settings.get("lookback") or rules.get("agent", {}).get("lookback", 100))
    compact = bool(payload.get("compact", False))
    compact_candles = max(int(payload.get("compact_candles") or 8), 1)

    df, source = fetch_market_dataframe(payload, rules, symbol, interval, lookback)
    full_candles = to_jsonable(df.tail(lookback))
    candles = full_candles if not compact else to_jsonable(df.tail(min(compact_candles, lookback)))
    latest_row = candles[-1] if candles else {}
    return {
        "ok": True,
        "symbol": symbol,
        "market": market,
        "interval": interval,
        "lookback": lookback,
        "count": len(full_candles),
        "returned_count": len(candles),
        "compact": compact,
        "market_data": candles,
        "latest_close": latest_row.get("close"),
        "latest_timestamp": latest_row.get("date"),
        "source": source,
    }


def action_calculate_indicator(payload: Dict[str, Any]) -> Dict[str, Any]:
    rules = load_rules(payload)
    indicator = (payload.get("indicator") or "all").lower()
    params = dict(payload.get("params") or {})
    symbol = str(payload.get("symbol") or "")
    market = str(payload.get("market") or "")
    if symbol:
        params.setdefault("__symbol__", symbol)
    if market:
        params.setdefault("__market__", market)

    candles = payload.get("market_data") or []
    if candles and len(candles) >= 30:
        df = normalize_candles(candles)
    elif symbol:
        settings = market_settings_for_symbol(symbol, rules, payload)
        interval = payload.get("interval") or settings.get("timeframe") or rules.get("agent", {}).get("timeframe", "1h")
        lookback = int(payload.get("lookback") or settings.get("lookback") or rules.get("agent", {}).get("lookback", 100))
        df, _ = fetch_market_dataframe(payload, rules, symbol, interval, lookback)
    else:
        df = normalize_candles(candles)
    bundle = calculate_indicator_bundle(df, rules, params)

    if indicator == "ema":
        result = {
            "ema_fast": bundle["ema_fast"],
            "ema_fast_prev": bundle["ema_fast_prev"],
            "ema_slow": bundle["ema_slow"],
            "ema_slow_prev": bundle["ema_slow_prev"],
            "periods": [bundle["ema_fast_period"], bundle["ema_slow_period"]],
        }
    elif indicator in {"bollinger", "bbands", "bb"}:
        result = {
            "bb_middle": bundle["bb_middle"],
            "bb_middle_prev": bundle["bb_middle_prev"],
            "bb_upper": bundle["bb_upper"],
            "bb_upper_prev": bundle["bb_upper_prev"],
            "bb_lower": bundle["bb_lower"],
            "bb_lower_prev": bundle["bb_lower_prev"],
            "period": bundle["bb_period"],
            "stddev": bundle["bb_stddev"],
        }
    elif indicator == "rsi":
        result = {"rsi": bundle["rsi"], "period": bundle["rsi_period"]}
    elif indicator == "macd":
        result = {
            "macd": bundle["macd"],
            "macd_signal": bundle["macd_signal"],
            "macd_hist": bundle["macd_hist"],
        }
    else:
        result = bundle

    return {"ok": True, "indicator": indicator, "indicators": result}


def evaluate_trend_following_signal(
    strategy_cfg: Dict[str, Any],
    indicators: Dict[str, Any],
    quantity: float | str,
    scale: float,
    df: pd.DataFrame | None = None,
    higher_tf_df: pd.DataFrame | None = None,
    market: str = "",
) -> tuple[str, str, float | str]:
    fast_prev = float(indicators["ema_fast_prev"])
    fast_now = float(indicators["ema_fast"])
    slow_prev = float(indicators["ema_slow_prev"])
    slow_now = float(indicators["ema_slow"])
    rsi_value = float(indicators["rsi"]) if indicators.get("rsi") is not None else 50.0
    buy_rsi_below = float(strategy_cfg.get("buy_rsi_below", 70))
    sell_rsi_above = float(strategy_cfg.get("sell_rsi_above", 85))
    golden_cross = fast_prev <= slow_prev and fast_now > slow_now
    death_cross = fast_prev >= slow_prev and fast_now < slow_now
    higher_trend_ok = True
    higher_tf_ema = None
    use_higher_tf_filter = market == "cn_equity" and bool(strategy_cfg.get("use_higher_timeframe_filter", False))
    higher_tf_period = max(int(strategy_cfg.get("higher_timeframe_ema_period", 13) or 13), 1)

    if use_higher_tf_filter and higher_tf_df is not None and not higher_tf_df.empty and len(higher_tf_df) >= higher_tf_period:
        higher_close = pd.to_numeric(higher_tf_df["close"], errors="coerce")
        higher_ema_series = higher_close.ewm(span=higher_tf_period, adjust=False).mean()
        higher_tf_ema = float(higher_ema_series.iloc[-1])
        higher_trend_ok = float(higher_close.iloc[-1]) > higher_tf_ema

    indicators["higher_trend_ok"] = higher_trend_ok
    indicators["higher_tf_ema"] = higher_tf_ema

    if golden_cross and rsi_value < buy_rsi_below and higher_trend_ok:
        if float(quantity) <= 0:
            return (
                "hold",
                f"Trend-following buy signal fired but scaled quantity rounded to zero after macro position scale {scale:.2f}",
                quantity,
            )
        return (
            "buy",
            f"Trend-following entry: EMA{strategy_cfg['ema_fast_period']} crossed above EMA{strategy_cfg['ema_slow_period']}, RSI {rsi_value:.2f} < {buy_rsi_below:.2f}, higher timeframe aligned",
            quantity,
        )
    if death_cross or rsi_value > sell_rsi_above:
        if strategy_cfg.get("sell_all_on_exit", True):
            quantity = "ALL"
        return (
            "sell",
            f"Trend-following exit: death_cross={death_cross} or RSI {rsi_value:.2f} > {sell_rsi_above:.2f}",
            quantity,
        )
    return (
        "hold",
        f"No configured rule fired; EMA{strategy_cfg['ema_fast_period']}={indicators['ema_fast']:.2f}, EMA{strategy_cfg['ema_slow_period']}={indicators['ema_slow']:.2f}, RSI {rsi_value:.2f}, higher timeframe ok={higher_trend_ok}",
        quantity,
    )


def evaluate_mean_reversion_signal(
    strategy_cfg: Dict[str, Any],
    indicators: Dict[str, Any],
    quantity: float | str,
    scale: float,
) -> tuple[str, str, float | str]:
    close = float(indicators["close"])
    rsi_value = float(indicators["rsi"]) if indicators.get("rsi") is not None else 50.0
    bb_lower = safe_float(indicators.get("bb_lower"))
    bb_upper = safe_float(indicators.get("bb_upper"))
    bb_middle = safe_float(indicators.get("bb_middle"))
    if bb_lower is None or bb_upper is None or bb_middle is None:
        return ("hold", "Mean-reversion signal skipped because Bollinger bands are incomplete", quantity)

    buy_rsi_below = float(strategy_cfg.get("buy_rsi_below", 38))
    sell_rsi_above = float(strategy_cfg.get("sell_rsi_above", 68))
    midline_exit_rsi_above = float(strategy_cfg.get("midline_exit_rsi_above", 52))
    touched_lower = close <= bb_lower
    touched_upper = close >= bb_upper
    crossed_midline = bool(strategy_cfg.get("exit_on_midline", True)) and close >= bb_middle

    if touched_lower and rsi_value <= buy_rsi_below:
        if float(quantity) <= 0:
            return (
                "hold",
                f"Mean-reversion buy signal fired but scaled quantity rounded to zero after macro position scale {scale:.2f}",
                quantity,
            )
        return (
            "buy",
            f"Mean-reversion entry: close {close:.2f} touched lower band {bb_lower:.2f} and RSI {rsi_value:.2f} <= {buy_rsi_below:.2f}",
            quantity,
        )
    if touched_upper and rsi_value >= sell_rsi_above:
        if strategy_cfg.get("sell_all_on_exit", True):
            quantity = "ALL"
        return (
            "sell",
            f"Mean-reversion exit: close {close:.2f} reached upper band {bb_upper:.2f} and RSI {rsi_value:.2f} >= {sell_rsi_above:.2f}",
            quantity,
        )
    if crossed_midline and rsi_value >= midline_exit_rsi_above:
        if strategy_cfg.get("sell_all_on_exit", True):
            quantity = "ALL"
        return (
            "sell",
            f"Mean-reversion take-profit: close {close:.2f} recovered above middle band {bb_middle:.2f} and RSI {rsi_value:.2f} >= {midline_exit_rsi_above:.2f}",
            quantity,
        )
    return (
        "hold",
        f"No configured rule fired; close {close:.2f}, BB[{bb_lower:.2f}, {bb_middle:.2f}, {bb_upper:.2f}], RSI {rsi_value:.2f}",
        quantity,
    )


def stop_loss_signal_for_symbol(
    symbol: str,
    market: str,
    df: pd.DataFrame,
    rules: Dict[str, Any],
) -> tuple[str, str, float | str] | None:
    if market != "crypto" or df is None or df.empty:
        return None
    state = load_state(rules)
    position = (state.get("open_positions") or {}).get(symbol)
    if not isinstance(position, dict):
        return None

    stop_loss = safe_float(position.get("stop_loss"))
    quantity = safe_float(position.get("quantity"))
    entry_price = safe_float(position.get("entry_price"))
    current_price = safe_float(df["close"].iloc[-1]) if "close" in df.columns else None
    if stop_loss is None or quantity in (None, 0) or current_price is None:
        return None
    strategy_cfg = strategy_config_for_symbol(symbol, rules, {"symbol": symbol, "market": market})
    move_stop_to_entry = bool(strategy_cfg.get("move_stop_to_entry_on_profit", True))
    breakeven_trigger_pct = max(float(strategy_cfg.get("breakeven_trigger_pct", 0.03) or 0.03), 0.0)
    if (
        move_stop_to_entry
        and entry_price not in (None, 0)
        and current_price >= float(entry_price) * (1.0 + breakeven_trigger_pct)
        and stop_loss < float(entry_price)
    ):
        with runtime_lock(rules):
            locked_state = load_state(rules)
            open_positions = locked_state.setdefault("open_positions", {})
            locked_position = open_positions.get(symbol)
            if isinstance(locked_position, dict):
                locked_entry_price = safe_float(locked_position.get("entry_price"))
                locked_stop_loss = safe_float(locked_position.get("stop_loss"))
                if (
                    locked_entry_price not in (None, 0)
                    and locked_stop_loss is not None
                    and current_price >= float(locked_entry_price) * (1.0 + breakeven_trigger_pct)
                    and locked_stop_loss < float(locked_entry_price)
                ):
                    locked_position["stop_loss"] = float(locked_entry_price)
                    locked_position["breakeven_armed_at"] = now_local(rules).isoformat()
                    open_positions[symbol] = locked_position
                    save_state(locked_state, rules)
                    stop_loss = float(locked_entry_price)
    if current_price > stop_loss:
        return None
    return (
        "sell",
        f"ATR stop loss triggered: current price {current_price:.2f} <= stop loss {stop_loss:.2f}",
        float(quantity),
    )


def evaluate_combined_signal(
    strategy_cfg: Dict[str, Any],
    indicators: Dict[str, Any],
    quantity: float | str,
    scale: float,
    df: pd.DataFrame | None = None,
    higher_tf_df: pd.DataFrame | None = None,
) -> tuple[str, str, float | str]:
    fast_prev = float(indicators["ma_fast_prev"])
    fast_now = float(indicators["ma_fast"])
    slow_prev = float(indicators["ma_slow_prev"])
    slow_now = float(indicators["ma_slow"])
    rsi_value = float(indicators["rsi"]) if indicators.get("rsi") is not None else 50.0
    rsi_upper = float(strategy_cfg.get("rsi_upper", 70))
    rsi_lower = float(strategy_cfg.get("rsi_lower", 30))
    golden_cross = fast_prev <= slow_prev and fast_now > slow_now
    death_cross = fast_prev >= slow_prev and fast_now < slow_now

    trend_filter_period = max(int(strategy_cfg.get("trend_filter_ema_period", 200) or 200), 1)
    volume_lookback = max(int(strategy_cfg.get("volume_lookback", 20) or 20), 1)
    volume_multiplier = float(strategy_cfg.get("volume_multiplier", 1.5) or 1.5)
    higher_tf_period = max(int(strategy_cfg.get("higher_timeframe_ema_period", 50) or 50), 1)

    above_200ema = True
    ema200_value = None
    if df is not None and not df.empty and len(df) >= trend_filter_period:
        ema200 = pd.to_numeric(df["close"], errors="coerce").ewm(span=trend_filter_period, adjust=False).mean()
        ema200_value = float(ema200.iloc[-1])
        above_200ema = float(df["close"].iloc[-1]) > ema200_value

    volume_ok = True
    average_volume = None
    current_volume = None
    if df is not None and not df.empty and golden_cross and len(df) >= volume_lookback:
        volume_series = pd.to_numeric(df["volume"], errors="coerce")
        average_volume = float(volume_series.tail(volume_lookback).mean())
        current_volume = float(volume_series.iloc[-1])
        volume_ok = current_volume > (average_volume * volume_multiplier)

    higher_trend_ok = True
    higher_ema_value = None
    if higher_tf_df is not None and not higher_tf_df.empty and len(higher_tf_df) >= higher_tf_period:
        higher_close = pd.to_numeric(higher_tf_df["close"], errors="coerce")
        higher_ema = higher_close.ewm(span=higher_tf_period, adjust=False).mean()
        higher_ema_value = float(higher_ema.iloc[-1])
        higher_trend_ok = float(higher_close.iloc[-1]) > higher_ema_value

    indicators["trend_filter_above_ema"] = above_200ema
    indicators["trend_filter_ema_value"] = ema200_value
    indicators["volume_confirmation"] = volume_ok
    indicators["avg_volume"] = average_volume
    indicators["current_volume"] = current_volume
    indicators["higher_trend_ok"] = higher_trend_ok
    indicators["higher_tf_ema"] = higher_ema_value

    if golden_cross and rsi_value <= rsi_upper and above_200ema and volume_ok and higher_trend_ok:
        if float(quantity) <= 0:
            return (
                "hold",
                f"Combined-strategy buy signal fired but scaled quantity rounded to zero after position scale {scale:.2f}",
                quantity,
            )
        return (
            "buy",
            f"Enhanced combined entry: MA{strategy_cfg['fast']} crossed above MA{strategy_cfg['slow']}, RSI {rsi_value:.2f} <= {rsi_upper:.2f}, price above EMA{trend_filter_period}, volume confirmed, higher timeframe aligned",
            quantity,
        )
    if death_cross or rsi_value >= rsi_upper:
        if strategy_cfg.get("sell_all_on_exit", True):
            quantity = "ALL"
        trigger = "death_cross" if death_cross else f"RSI {rsi_value:.2f} >= {rsi_upper:.2f}"
        return ("sell", f"Enhanced combined exit: {trigger}", quantity)
    return (
        "hold",
        f"No configured rule fired; MA{strategy_cfg['fast']}={indicators['ma_fast']:.2f}, MA{strategy_cfg['slow']}={indicators['ma_slow']:.2f}, RSI {rsi_value:.2f}, oversold guard {rsi_lower:.2f}",
        quantity,
    )


def action_generate_signal(payload: Dict[str, Any]) -> Dict[str, Any]:
    rules = load_rules(payload)
    root_cfg = load_root_config()
    symbol = str(payload.get("symbol") or "").strip()
    if not symbol:
        reason = "generate_signal skipped because payload.symbol is missing; defaulting to HOLD."
        return {
            "ok": True,
            "status": "hold",
            "signal": "HOLD",
            "decision": "HOLD",
            "action": "hold",
            "side": "hold",
            "symbol": None,
            "market": payload.get("market"),
            "strategy_name": str(payload.get("strategy_name") or payload.get("strategy") or "unknown"),
            "strategy_kind": "unknown",
            "quantity": 0,
            "position_scale": 1.0,
            "execution_allowed": False,
            "configured_execution_allowed": False,
            "market_session": {},
            "price": None,
            "reason": reason,
            "indicators": {
                "guarded_missing_symbol": True,
            },
        }
    market = infer_market(payload, rules, symbol)
    strategy_cfg = strategy_config_for_symbol(symbol, rules, {"symbol": symbol, "market": market})
    settings = market_settings_for_symbol(symbol, rules, payload)
    interval = payload.get("interval") or settings.get("timeframe") or rules.get("agent", {}).get("timeframe", "1h")
    lookback = int(payload.get("lookback") or settings.get("lookback") or rules.get("agent", {}).get("lookback", 100))
    strategy_kind = str(strategy_cfg.get("kind") or "trend_following")
    required_lookback = lookback
    if strategy_kind == "combined":
        required_lookback = max(
            required_lookback,
            int(strategy_cfg.get("trend_filter_ema_period", 200) or 200) + 5,
            int(strategy_cfg.get("volume_lookback", 20) or 20) + 5,
            int(strategy_cfg.get("atr_period", 14) or 14) + 10,
        )

    candles = payload.get("market_data")
    if candles and len(candles) >= min(required_lookback, 30):
        df = normalize_candles(candles)
        if len(df) < required_lookback:
            df, _ = fetch_market_dataframe(payload, rules, symbol, interval, required_lookback)
    else:
        df, _ = fetch_market_dataframe(payload, rules, symbol, interval, required_lookback)

    calc_params = {"__symbol__": symbol, "__market__": market}
    indicators = payload.get("indicators") or calculate_indicator_bundle(df, rules, calc_params)
    scale = effective_position_scale(symbol, market, rules, root_cfg, {"symbol": symbol, "market": market})
    quantity: float | str = suggested_buy_quantity(symbol, market, rules, root_cfg, {"symbol": symbol, "market": market})
    session_status = market_session_status(market, rules, root_cfg)
    configured_execution_allowed = market in auto_execution_markets_from_rules(rules)
    execution_allowed = configured_execution_allowed and bool(session_status.get("execution_allowed", True))
    strategy_name = str(strategy_cfg.get("name") or strategy_kind)
    higher_tf_df = None
    if strategy_kind == "combined" and bool(strategy_cfg.get("use_4h_filter", False)):
        higher_payload = dict(payload)
        higher_payload["symbol"] = symbol
        higher_payload["market"] = market
        higher_interval = str(strategy_cfg.get("higher_timeframe") or "4h")
        higher_lookback = max(int(strategy_cfg.get("higher_timeframe_lookback", 120) or 120), int(strategy_cfg.get("higher_timeframe_ema_period", 50) or 50) + 10)
        try:
            higher_tf_df, _ = fetch_market_dataframe(higher_payload, rules, symbol, higher_interval, higher_lookback)
        except Exception:
            higher_tf_df = None
    elif market == "cn_equity" and bool(strategy_cfg.get("use_higher_timeframe_filter", False)):
        higher_payload = dict(payload)
        higher_payload["symbol"] = symbol
        higher_payload["market"] = market
        higher_interval = str(strategy_cfg.get("higher_timeframe") or "1wk")
        higher_lookback = max(
            int(strategy_cfg.get("higher_timeframe_lookback", 80) or 80),
            int(strategy_cfg.get("higher_timeframe_ema_period", 13) or 13) + 10,
        )
        try:
            higher_tf_df, _ = fetch_market_dataframe(higher_payload, rules, symbol, higher_interval, higher_lookback)
        except Exception:
            higher_tf_df = None

    stop_loss_override = stop_loss_signal_for_symbol(symbol, market, df, rules)
    if stop_loss_override is not None:
        signal, reason, quantity = stop_loss_override
    elif strategy_kind == "mean_reversion":
        signal, reason, quantity = evaluate_mean_reversion_signal(strategy_cfg, indicators, quantity, scale)
    elif strategy_kind == "combined":
        signal, reason, quantity = evaluate_combined_signal(
            strategy_cfg,
            indicators,
            quantity,
            scale,
            df=df,
            higher_tf_df=higher_tf_df,
        )
    else:
        signal, reason, quantity = evaluate_trend_following_signal(
            strategy_cfg,
            indicators,
            quantity,
            scale,
            df=df,
            higher_tf_df=higher_tf_df,
            market=market,
        )

    if market == "futures" and signal == "sell" and str(quantity).strip().upper() == "ALL":
        quantity = suggested_buy_quantity(symbol, market, rules, root_cfg, {"symbol": symbol, "market": market})
        reason = f"{reason} Futures routing treats a sell signal as a short entry, so the order size is reset to the configured base quantity."

    return {
        "ok": True,
        "symbol": symbol,
        "market": market,
        "strategy_name": strategy_name,
        "strategy_kind": strategy_kind,
        "signal": signal,
        "reason": reason,
        "quantity": quantity,
        "position_scale": scale,
        "execution_allowed": execution_allowed,
        "configured_execution_allowed": configured_execution_allowed,
        "market_session": session_status,
        "price": float(df["close"].iloc[-1]),
        "indicators": {
            "ma_fast": indicators.get("ma_fast"),
            "ma_slow": indicators.get("ma_slow"),
            "ema_fast": indicators.get("ema_fast"),
            "ema_slow": indicators.get("ema_slow"),
            "rsi": indicators.get("rsi"),
            "bb_middle": indicators.get("bb_middle"),
            "bb_upper": indicators.get("bb_upper"),
            "bb_lower": indicators.get("bb_lower"),
            "trend_filter_above_ema": indicators.get("trend_filter_above_ema"),
            "trend_filter_ema_value": indicators.get("trend_filter_ema_value"),
            "volume_confirmation": indicators.get("volume_confirmation"),
            "avg_volume": indicators.get("avg_volume"),
            "current_volume": indicators.get("current_volume"),
            "higher_trend_ok": indicators.get("higher_trend_ok"),
            "higher_tf_ema": indicators.get("higher_tf_ema"),
            "stop_loss": ((load_state(rules).get("open_positions") or {}).get(symbol, {}) or {}).get("stop_loss"),
            "ma_golden_cross": (
                float(indicators["ma_fast_prev"]) <= float(indicators["ma_slow_prev"])
                and float(indicators["ma_fast"]) > float(indicators["ma_slow"])
            ),
            "ma_death_cross": (
                float(indicators["ma_fast_prev"]) >= float(indicators["ma_slow_prev"])
                and float(indicators["ma_fast"]) < float(indicators["ma_slow"])
            ),
            "golden_cross": (
                float(indicators["ema_fast_prev"]) <= float(indicators["ema_slow_prev"])
                and float(indicators["ema_fast"]) > float(indicators["ema_slow"])
            ),
            "death_cross": (
                float(indicators["ema_fast_prev"]) >= float(indicators["ema_slow_prev"])
                and float(indicators["ema_fast"]) < float(indicators["ema_slow"])
            ),
        },
    }


def resolve_requested_quantity(
    payload: Dict[str, Any],
    rules: Dict[str, Any],
    side: str,
    root_cfg: Dict[str, Any] | None = None,
) -> float:
    symbol = payload["symbol"]
    market = infer_market(payload, rules, symbol)
    quantity = payload.get("quantity")

    if quantity not in (None, "", "ALL"):
        return normalize_order_quantity(payload, rules, quantity, side)

    if side == "buy":
        return suggested_buy_quantity(symbol, market, rules, root_cfg, payload)

    executor = build_executor(payload, rules)
    try:
        if not executor.connect():
            raise TradingBridgeError(f"Executor connection failed for {symbol}")
        positions = executor.get_positions()
    finally:
        try:
            executor.disconnect()
        except Exception:
            pass
    return normalize_order_quantity(payload, rules, get_position_quantity(symbol, positions), side)


def action_check_risk(payload: Dict[str, Any]) -> Dict[str, Any]:
    rules = load_rules(payload)
    root_cfg = load_root_config()
    trading_cfg = trading_runtime_config(rules, root_cfg)
    state = load_state(rules)
    symbol = payload["symbol"]
    side = payload["side"].lower()
    market = infer_market(payload, rules, symbol)
    session_status = market_session_status(market, rules, root_cfg)

    if state.get("paused_until"):
        paused_until = datetime.fromisoformat(state["paused_until"])
        if now_local(rules) < paused_until:
            return {
                "ok": True,
                "allowed": False,
                "reasons": [f"Trading is paused until {state['paused_until']}"],
                "state": state,
                "market_session": session_status,
            }

    if in_blackout_window(symbol, rules):
        return {
            "ok": True,
            "allowed": False,
            "reasons": ["Current market window is blocked by restrictions"],
            "state": state,
            "market_session": session_status,
        }

    if market == "cn_equity" and not session_status.get("execution_allowed", True):
        return {
            "ok": True,
            "allowed": False,
            "reasons": [str(session_status.get("reason") or "China A-share market is outside trading hours")],
            "state": state,
            "market_session": session_status,
        }

    price = resolve_price(payload, rules)
    quantity = resolve_requested_quantity(payload, rules, side, root_cfg)
    if quantity <= 0:
        return {
            "ok": True,
            "allowed": False,
            "reasons": ["Resolved quantity is zero after market lot-size normalization"],
            "state": state,
            "market_session": session_status,
        }

    futures_cfg = trading_cfg.get("futures", {})
    leverage = 1
    if market == "futures":
        leverage = resolve_requested_leverage(payload, futures_cfg)

    estimated_loss = payload.get("estimated_loss")
    if estimated_loss is None:
        stop_price = payload.get("stop_price")
        if stop_price is not None:
            estimated_loss = abs(price - float(stop_price)) * quantity
        else:
            default_stop_loss_pct = (
                float(futures_cfg.get("mandatory_stop_loss_pct", rules["risk"].get("default_stop_loss_pct", 0.02)))
                if market == "futures"
                else float(rules["risk"].get("default_stop_loss_pct", 0.02))
            )
            estimated_loss = price * quantity * default_stop_loss_pct

    executor = build_executor(payload, rules)
    balance: Dict[str, float] = {}
    positions: List[Any] = []
    try:
        if not executor.connect():
            raise TradingBridgeError("Executor connection failed during risk check")
        balance = executor.get_balance()
        if side == "sell":
            positions = executor.get_positions()
    except Exception:
        balance = {}
        positions = []
    finally:
        try:
            executor.disconnect()
        except Exception:
            pass

    equity = float(balance.get("total_asset") or assumed_equity_for_market(payload, rules))
    cash = float(balance.get("cash") or equity)
    market_value = float(balance.get("market_value") or 0)
    order_value = price * quantity
    capital_required = order_value if market != "futures" else (order_value / max(float(leverage), 1.0))
    today = today_key(rules)
    committed_today = float(state.get("daily_risk_committed", {}).get(today, 0.0))
    futures_trades_today = executed_trade_count_for_day(rules, "futures", today) if market == "futures" else 0

    reasons: List[str] = []
    max_position_pct = float(
        futures_cfg.get("max_position_pct", trading_cfg.get("max_position_pct", 1.0))
        if market == "futures"
        else trading_cfg.get("max_position_pct", 1.0)
        or 1.0
    )
    max_daily_loss_pct = float(
        futures_cfg.get("max_daily_loss_pct", trading_cfg.get("max_daily_loss_pct", rules["risk"].get("max_daily_loss_pct", 0.05)))
        if market == "futures"
        else trading_cfg.get("max_daily_loss_pct", rules["risk"].get("max_daily_loss_pct", 0.05))
    )
    if order_value > float(rules["risk"].get("max_order_value_usdt", 50000)):
        reasons.append("Order value exceeds max_order_value_usdt")
    if market == "futures" and not futures_cfg.get("enabled", True):
        reasons.append("Futures trading is disabled in trading.futures.enabled")
    if market == "futures" and leverage > int(futures_cfg.get("max_leverage", leverage)):
        reasons.append("Requested leverage exceeds trading.futures.max_leverage")
    if market == "futures" and futures_cfg.get("require_stop_price", True) and payload.get("stop_price") is None and payload.get("estimated_loss") is None:
        reasons.append("Futures orders require stop_price or estimated_loss to enforce mandatory_stop_loss")
    if market == "futures" and max_position_pct > 0 and capital_required > equity * max_position_pct:
        reasons.append("Futures initial margin exceeds trading.futures.max_position_pct budget")
    if market != "futures" and max_position_pct > 0 and order_value > equity * max_position_pct:
        reasons.append("Order value exceeds trading.max_position_pct budget")
    if estimated_loss > equity * float(rules["risk"].get("max_single_loss_pct", 0.02)):
        reasons.append("Estimated single-order loss exceeds max_single_loss_pct budget")
    if committed_today + estimated_loss > equity * max_daily_loss_pct:
        reasons.append(
            "Daily risk budget would exceed trading.futures.max_daily_loss"
            if market == "futures"
            else "Daily risk budget would exceed trading.max_daily_loss / max_daily_loss_pct"
        )
    if side == "buy" and (market_value + order_value) > float(rules["risk"].get("max_position_value_usdt", 100000)):
        reasons.append("Position value would exceed max_position_value_usdt")
    if market == "futures" and futures_cfg.get("max_trades_per_day", 0) > 0 and futures_trades_today >= int(futures_cfg.get("max_trades_per_day", 0)):
        reasons.append("Futures daily trade count would exceed trading.futures.max_trades_per_day")
    if market == "futures" and cash > 0 and (cash - capital_required) / equity < float(rules["risk"].get("min_cash_ratio", 0.10)):
        reasons.append("Available collateral would fall below min_cash_ratio")
    if market != "futures" and side == "buy" and cash > 0 and (cash - order_value) / equity < float(rules["risk"].get("min_cash_ratio", 0.10)):
        reasons.append("Cash reserve would fall below min_cash_ratio")

    return {
        "ok": True,
        "allowed": not reasons,
        "reasons": reasons,
        "metrics": {
            "market": market,
            "market_session": session_status,
            "equity": equity,
            "cash": cash,
            "market_value": market_value,
            "order_value": order_value,
            "capital_required": capital_required,
            "estimated_loss": estimated_loss,
            "committed_daily_risk": committed_today,
            "leverage": leverage,
            "futures_trades_today": futures_trades_today,
        },
        "market_session": session_status,
        "position_quantity": get_position_quantity(symbol, positions),
    }


def action_place_order(payload: Dict[str, Any]) -> Dict[str, Any]:
    rules = load_rules(payload)
    root_cfg = load_root_config()
    trading_cfg = trading_runtime_config(rules, root_cfg)
    symbol = payload["symbol"]
    side = payload["side"].lower()
    market = infer_market(payload, rules, symbol)
    reason = payload.get("reason", "")
    strategy_name = str(payload.get("strategy") or strategy_name_for_symbol(symbol, rules, payload))
    order_type = payload.get("order_type", "market")

    risk_result = action_check_risk(payload)
    if not risk_result["allowed"]:
        event = {
            "timestamp": now_local(rules).isoformat(),
            "status": "blocked",
            "symbol": symbol,
            "side": side,
            "message": "Risk check blocked order",
            "details": risk_result,
        }
        with runtime_lock(rules):
            append_decision(event, rules)
        return {
            "ok": True,
            "approved": False,
            "executed": False,
            "risk": risk_result,
            "message": "Risk check blocked order",
        }

    price = resolve_price(payload, rules)
    quantity = resolve_requested_quantity(payload, rules, side, root_cfg)
    if quantity <= 0:
        raise TradingBridgeError(f"Resolved quantity for {symbol} is {quantity}, cannot place order")

    if trading_cfg.get("auto_approve", False):
        auto_payload = dict(payload)
        auto_payload["quantity"] = quantity
        auto_payload["price"] = price
        if not auto_payload.get("reason"):
            auto_payload["reason"] = "Order auto-approved by trading.auto_approve"
        result = execute_approved_order(
            payload=auto_payload,
            rules=rules,
            root_cfg=root_cfg,
            request_id="",
            risk_result=risk_result,
        )
        result["auto_approved"] = True
        result["message"] = "Order auto-approved and executed"
        return result

    executor = build_executor(payload, rules)
    gatekeeper = None
    try:
        if not executor.connect():
            raise TradingBridgeError(f"Executor connection failed for {symbol}")

        gatekeeper_cfg = root_cfg.get("gatekeeper", {})
        if not gatekeeper_cfg.get("manual_approval", True):
            raise TradingBridgeError("manual_approval is disabled in config; refusing to bypass approval")

        gatekeeper = GateKeeperSkill(str(OPENCLAW_ROOT / "gatekeeper" / "config.yaml"))
        execution_cfg = root_execution_config()
        resolved_mode = execution_mode_for_market(market, payload, rules, execution_cfg)
        approved = gatekeeper.approve_before_execute(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            order_type=order_type,
            reason=reason,
            strategy=strategy_name,
            executor=executor,
            metadata={
                "market": market,
                "execution_mode": resolved_mode,
                "leverage": payload.get("leverage"),
                "stop_price": payload.get("stop_price"),
                "broker_client": execution_cfg.get("easytrader", {}).get("client", ""),
                "bridge_workspace": execution_cfg.get("ths_bridge", {}).get("workspace", ""),
                "ths_exe_path": execution_cfg.get("ths_bridge", {}).get("exe_path", ""),
            },
        )

        if not approved:
            request_id = str(getattr(gatekeeper, "last_request_id", "") or "").strip()
            event = {
                "timestamp": now_local(rules).isoformat(),
                "status": "rejected",
                "request_id": request_id,
                "symbol": symbol,
                "side": side,
                "message": "Approval rejected or timed out",
            }
            with runtime_lock(rules):
                append_decision(event, rules)
                state = reset_failure_state(load_state(rules))
                save_state(state, rules)
            if notification_enabled(root_cfg, "on_order_rejected", True):
                send_operator_notification(
                    build_order_result_message(
                        title="[OpenClaw 交易结果]",
                        request_id=request_id,
                        symbol=symbol,
                        side=side,
                        quantity=quantity,
                        price=price,
                        strategy_name=strategy_name,
                        reason="审批被拒绝或超时，订单未执行",
                    ),
                    root_cfg=root_cfg,
                )
            return {
                "ok": True,
                "approved": False,
                "executed": False,
                "request_id": request_id,
                "message": "Approval rejected or timed out",
            }

        request_id = str(getattr(gatekeeper, "last_request_id", "") or "").strip()
        payload["quantity"] = quantity
        return execute_approved_order(
            payload=payload,
            rules=rules,
            root_cfg=root_cfg,
            request_id=request_id,
            risk_result=risk_result,
        )
    finally:
        if gatekeeper is not None:
            try:
                gatekeeper.gatekeeper.stop_server()
            except Exception:
                pass
        try:
            executor.disconnect()
        except Exception:
            pass


def safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def parse_event_timestamp(raw_value: Any, rules: Dict[str, Any]) -> datetime | None:
    if not raw_value:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw_value))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=resolve_timezone(rules))
    return parsed.astimezone(resolve_timezone(rules))


def infer_market_from_event(event: Dict[str, Any], rules: Dict[str, Any]) -> str:
    details = event.get("details") or {}
    market = str(details.get("market") or "").strip()
    if market:
        return market
    symbol = str(event.get("symbol") or "").strip()
    return infer_market({"symbol": symbol}, rules, symbol)


def pnl_ratio_from_event(event: Dict[str, Any]) -> float | None:
    details = event.get("details") or {}
    ratio_candidates = [
        event.get("pnl_ratio"),
        details.get("pnl_ratio"),
        details.get("return_ratio"),
        details.get("return_pct"),
    ]
    for candidate in ratio_candidates:
        ratio = safe_float(candidate)
        if ratio is None:
            continue
        if abs(ratio) > 1.5:
            ratio /= 100.0
        return ratio

    pnl_candidates = [
        event.get("pnl"),
        details.get("pnl"),
        details.get("realized_pnl"),
        details.get("profit_loss"),
    ]
    notional_candidates = [
        details.get("order_value"),
        details.get("notional"),
        (details.get("risk_check") or {}).get("order_value"),
    ]
    pnl = next((safe_float(item) for item in pnl_candidates if safe_float(item) is not None), None)
    notional = next((safe_float(item) for item in notional_candidates if safe_float(item) is not None), None)
    if pnl is None or notional in (None, 0):
        return None
    return pnl / notional


def order_size_bounds_for_market(market: str, cfg: Dict[str, Any]) -> tuple[float, float]:
    floors = cfg.get("min_order_size_floor", {}) or {}
    caps = cfg.get("max_order_size_cap", {}) or {}
    floor = safe_float(floors.get(market))
    cap = safe_float(caps.get(market))
    if floor is None:
        floor = 0.001 if market == "crypto" else 100.0
    if cap is None:
        cap = 0.05 if market == "crypto" else 5000.0
    return floor, cap


def normalize_optimized_order_size(value: float, market: str, cfg: Dict[str, Any]) -> float:
    floor, cap = order_size_bounds_for_market(market, cfg)
    clamped = max(min(value, cap), floor)
    if market == "cn_equity":
        lot_size = max(lot_size_for_market(market), 1)
        return float(max(int(clamped // lot_size) * lot_size, lot_size))
    return float(round(clamped, 6))


def set_order_size_override(rules: Dict[str, Any], symbol: str, market: str, new_size: float) -> None:
    strategy = rules.setdefault("strategy", {})
    symbol_overrides = strategy.setdefault("symbol_overrides", {})
    symbol_cfg = symbol_overrides.setdefault(symbol, {})
    order_size = symbol_cfg.setdefault("order_size", {})
    per_symbol = order_size.setdefault("per_symbol", {})
    if market == "cn_equity":
        per_symbol[symbol] = int(new_size)
    else:
        per_symbol[symbol] = float(new_size)


def set_combined_strategy_params_override(
    rules: Dict[str, Any],
    symbol: str,
    fast: int,
    slow: int,
    rsi_upper: float,
) -> None:
    strategy = rules.setdefault("strategy", {})
    symbol_overrides = strategy.setdefault("symbol_overrides", {})
    symbol_cfg = symbol_overrides.setdefault(symbol, {})
    symbol_cfg["strategy"] = "combined"
    symbol_cfg["fast"] = int(fast)
    symbol_cfg["slow"] = int(slow)
    symbol_cfg["rsi_upper"] = float(rsi_upper)


def backtest_combined(
    df: pd.DataFrame,
    fast: int,
    slow: int,
    rsi_upper: float,
    *,
    rsi_period: int = 14,
    trend_filter_ema_period: int = 200,
    volume_multiplier: float = 1.5,
    volume_lookback: int = 20,
) -> Dict[str, Any]:
    if df is None or df.empty:
        return {"total_return": -999.0, "win_rate": 0.0, "trade_count": 0}
    minimum_rows = max(int(slow), int(rsi_period), int(trend_filter_ema_period), int(volume_lookback)) + 2
    if len(df) < minimum_rows:
        return {"total_return": -999.0, "win_rate": 0.0, "trade_count": 0}

    working = df.copy()
    close = pd.to_numeric(working["close"], errors="coerce")
    volume = pd.to_numeric(working["volume"], errors="coerce")
    ma_fast = close.rolling(int(fast)).mean()
    ma_slow = close.rolling(int(slow)).mean()
    rsi = rsi_series(close, int(rsi_period))
    trend_ema = close.ewm(span=int(trend_filter_ema_period), adjust=False).mean()
    avg_volume = volume.rolling(int(volume_lookback)).mean()

    equity = 1.0
    entry_price = None
    trade_returns: List[float] = []

    for idx in range(1, len(working)):
        if any(
            pd.isna(value)
            for value in (
                ma_fast.iloc[idx - 1],
                ma_fast.iloc[idx],
                ma_slow.iloc[idx - 1],
                ma_slow.iloc[idx],
                rsi.iloc[idx],
                close.iloc[idx],
                trend_ema.iloc[idx],
            )
        ):
            continue

        golden_cross = float(ma_fast.iloc[idx - 1]) <= float(ma_slow.iloc[idx - 1]) and float(ma_fast.iloc[idx]) > float(ma_slow.iloc[idx])
        death_cross = float(ma_fast.iloc[idx - 1]) >= float(ma_slow.iloc[idx - 1]) and float(ma_fast.iloc[idx]) < float(ma_slow.iloc[idx])
        above_trend = float(close.iloc[idx]) > float(trend_ema.iloc[idx])
        volume_ok = True
        if not pd.isna(avg_volume.iloc[idx]) and not pd.isna(volume.iloc[idx]):
            volume_ok = float(volume.iloc[idx]) > float(avg_volume.iloc[idx]) * float(volume_multiplier)

        if entry_price is None:
            if golden_cross and float(rsi.iloc[idx]) <= float(rsi_upper) and above_trend and volume_ok:
                entry_price = float(close.iloc[idx])
            continue

        if death_cross or float(rsi.iloc[idx]) >= float(rsi_upper):
            exit_price = float(close.iloc[idx])
            trade_return = (exit_price - entry_price) / entry_price if entry_price else 0.0
            trade_returns.append(trade_return)
            equity *= 1.0 + trade_return
            entry_price = None

    if entry_price is not None:
        exit_price = float(close.iloc[-1])
        trade_return = (exit_price - entry_price) / entry_price if entry_price else 0.0
        trade_returns.append(trade_return)
        equity *= 1.0 + trade_return

    wins = len([item for item in trade_returns if item > 0])
    win_rate = float(wins / len(trade_returns)) if trade_returns else 0.0
    return {
        "total_return": float(equity - 1.0),
        "win_rate": win_rate,
        "trade_count": len(trade_returns),
    }


def summarize_symbol_performance(
    symbol: str,
    events: List[Dict[str, Any]],
    rules: Dict[str, Any],
) -> Dict[str, Any]:
    market = infer_market({"symbol": symbol}, rules, symbol)
    strategy_name = strategy_name_for_symbol(symbol, rules, {"symbol": symbol, "market": market})
    stats: Dict[str, Any] = {
        "symbol": symbol,
        "market": market,
        "strategy_name": strategy_name,
        "event_count": 0,
        "actionable": 0,
        "executed_trades": 0,
        "approved_orders": 0,
        "rejected_orders": 0,
        "timed_out_orders": 0,
        "error_events": 0,
        "pnl_samples": 0,
        "wins": 0,
        "losses": 0,
        "avg_pnl_ratio": None,
        "win_rate": None,
        "current_order_size": order_size_for_symbol(symbol, rules, {"symbol": symbol, "market": market}),
    }
    pnl_ratios: List[float] = []

    for event in events:
        if str(event.get("symbol") or "").strip() != symbol:
            continue
        stats["event_count"] += 1
        status = str(event.get("status") or "").strip().lower()
        message = str(event.get("message") or "").strip().lower()
        details = event.get("details") or {}
        event_market = infer_market_from_event(event, rules)
        if event_market:
            stats["market"] = event_market

        if status in {"buy", "sell", "blocked", "rejected", "timed_out", "approved", "executed", "filled"}:
            stats["actionable"] += 1
        if status in {"error", "failed"}:
            stats["error_events"] += 1
        if status in {"rejected"} or "rejected" in message:
            stats["rejected_orders"] += 1
        if status in {"timed_out"} or "timed out" in message:
            stats["timed_out_orders"] += 1
        if details.get("order_approved") or status == "approved":
            stats["approved_orders"] += 1
        if details.get("order_executed") or status in {"executed", "filled"}:
            stats["executed_trades"] += 1

        pnl_ratio = pnl_ratio_from_event(event)
        if pnl_ratio is None:
            continue
        pnl_ratios.append(pnl_ratio)
        if pnl_ratio > 0:
            stats["wins"] += 1
        elif pnl_ratio < 0:
            stats["losses"] += 1

    stats["pnl_samples"] = len(pnl_ratios)
    if pnl_ratios:
        stats["avg_pnl_ratio"] = float(sum(pnl_ratios) / len(pnl_ratios))
        non_flat = stats["wins"] + stats["losses"]
        if non_flat:
            stats["win_rate"] = float(stats["wins"] / non_flat)
    return stats


def cumulative_pnl_ratio_for_symbol(symbol: str, events: List[Dict[str, Any]]) -> float | None:
    pnl_ratios = [pnl_ratio_from_event(event) for event in events if str(event.get("symbol") or "").strip() == symbol]
    filtered = [ratio for ratio in pnl_ratios if ratio is not None]
    if not filtered:
        return None
    return float(sum(filtered))


def actionable_timestamps_for_symbol(symbol: str, events: List[Dict[str, Any]], rules: Dict[str, Any]) -> List[datetime]:
    actionable_statuses = {"buy", "sell", "blocked", "rejected", "timed_out", "approved", "executed", "filled"}
    timestamps: List[datetime] = []
    for event in events:
        if str(event.get("symbol") or "").strip() != symbol:
            continue
        status = str(event.get("status") or "").strip().lower()
        if status not in actionable_statuses:
            continue
        parsed = parse_event_timestamp(event.get("timestamp"), rules)
        if parsed is not None:
            timestamps.append(parsed)
    return timestamps


def business_days_between(start: datetime, end: datetime) -> int:
    if end <= start:
        return 0
    current = start.date()
    end_date = end.date()
    count = 0
    while current < end_date:
        current += timedelta(days=1)
        if current.weekday() < 5:
            count += 1
    return count


def cleanup_dynamic_assets(
    rules: Dict[str, Any],
    root_cfg: Dict[str, Any] | None = None,
    events: List[Dict[str, Any]] | None = None,
    now_ts: datetime | None = None,
) -> List[Dict[str, Any]]:
    root_cfg = root_cfg or load_root_config()
    cfg = auto_discover_config(rules, root_cfg)
    event_list = events if events is not None else load_decision_events(rules)
    current_time = now_ts or now_local(rules)
    removed: List[Dict[str, Any]] = []

    for symbol, metadata in list(dynamic_assets_registry(rules).items()):
        symbol_text = str(symbol or "").strip()
        if not symbol_text or symbol_text in set(core_symbols_from_rules(rules)):
            continue

        market = str((metadata or {}).get("market") or infer_market({"symbol": symbol_text}, rules, symbol_text)).strip()
        market_cfg = cfg.get(market, {}) or {}
        symbol_events = [event for event in event_list if str(event.get("symbol") or "").strip() == symbol_text]
        added_at = parse_event_timestamp((metadata or {}).get("added_at"), rules) or current_time
        actionable_times = actionable_timestamps_for_symbol(symbol_text, symbol_events, rules)
        last_actionable = max(actionable_times) if actionable_times else None
        cumulative_ratio = cumulative_pnl_ratio_for_symbol(symbol_text, symbol_events)
        reasons: List[str] = []

        if not is_valid_auto_discover_symbol(symbol_text, market):
            reasons.append("symbol failed discovery format validation")

        if market == "cn_equity":
            inactive_limit = max(int(market_cfg.get("max_inactive_trading_days", 10) or 10), 1)
            reference_time = last_actionable or added_at
            if business_days_between(reference_time, current_time) >= inactive_limit:
                reasons.append(f"no actionable signal for {inactive_limit} trading days")
        else:
            inactive_limit = max(int(market_cfg.get("max_inactive_days", 7) or 7), 1)
            reference_time = last_actionable or added_at
            inactivity_days = max(int((current_time - reference_time).total_seconds() // 86400), 0)
            if inactivity_days >= inactive_limit:
                reasons.append(f"no actionable trade for {inactive_limit} days")

        drawdown_limit = normalize_ratio_limit(market_cfg.get("max_drawdown_pct"), 0.15 if market == "crypto" else 0.10)
        if cumulative_ratio is not None and cumulative_ratio <= -abs(drawdown_limit):
            reasons.append(f"cumulative pnl ratio {cumulative_ratio:.4f} breached -{abs(drawdown_limit):.4f}")

        if not reasons:
            continue

        if remove_symbol_from_rules(rules, symbol_text, "; ".join(reasons), removed_at=current_time):
            removed.append(
                {
                    "symbol": symbol_text,
                    "market": market,
                    "reasons": reasons,
                    "cumulative_pnl_ratio": cumulative_ratio,
                    "last_actionable_at": last_actionable.isoformat() if last_actionable else None,
                }
            )

    return removed


def write_optimization_report(report: Dict[str, Any], rules: Dict[str, Any], root_cfg: Dict[str, Any]) -> Path:
    report_path = optimization_report_path(rules, root_cfg)
    ensure_parent(report_path)
    tmp_path = report_path.with_suffix(f"{report_path.suffix}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, report_path)
    return report_path


def save_rules(rules: Dict[str, Any], path: Path) -> None:
    ensure_parent(path)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(rules, handle, sort_keys=False, allow_unicode=False)
    os.replace(tmp_path, path)


def discover_crypto_candidates(rules: Dict[str, Any], root_cfg: Dict[str, Any], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    import ccxt

    exchange = ccxt.binance(
        {
            "apiKey": os.getenv("BINANCE_TESTNET_API_KEY") or os.getenv("BINANCE_API_KEY") or "",
            "secret": os.getenv("BINANCE_TESTNET_SECRET") or os.getenv("BINANCE_SECRET") or "",
            "enableRateLimit": True,
            "options": {
                "defaultType": "spot",
            },
        }
    )
    if bool((rules.get("account", {}) or {}).get("testnet", True)):
        try:
            exchange.set_sandbox_mode(True)
        except Exception:
            pass  # sandbox mode may not be supported for all market types

    min_volume = float(cfg.get("min_volume_usdt", 1000) or 1000)
    min_price = float(cfg.get("min_price", 0.01) or 0.01)
    min_volatility = float(cfg.get("min_volatility_pct", 3) or 3)
    max_new = max(int(cfg.get("max_new_per_scan", 3) or 3), 0)
    leveraged_suffixes = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S", "5L", "5S")
    candidates: List[Dict[str, Any]] = []

    for symbol, ticker in (exchange.fetch_tickers() or {}).items():
        if not isinstance(symbol, str) or ":" in symbol or not symbol.endswith("/USDT"):
            continue
        base = symbol.split("/", 1)[0].upper()
        if len(base) < 2:
            continue
        if any(base.endswith(suffix) for suffix in leveraged_suffixes):
            continue
        quote_volume = safe_float((ticker or {}).get("quoteVolume"))
        last_price = safe_float((ticker or {}).get("last"))
        volatility = safe_float((ticker or {}).get("percentage"))
        if quote_volume is None or quote_volume < min_volume:
            continue
        if last_price is None or last_price < min_price:
            continue
        if volatility is None or abs(volatility) < min_volatility:
            continue
        candidates.append(
            {
                "symbol": symbol,
                "market": "crypto",
                "last": last_price,
                "quote_volume_usdt": quote_volume,
                "volatility_pct": abs(volatility),
            }
        )

    candidates.sort(key=lambda item: (safe_float(item.get("volatility_pct")) or 0.0, safe_float(item.get("quote_volume_usdt")) or 0.0), reverse=True)
    return candidates[:max_new]


def discover_cn_equity_candidates_from_seed_universe(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    import yfinance as yf

    min_price = float(cfg.get("min_price", 1.0) or 1.0)
    min_amount = safe_float(cfg.get("min_amount"))
    min_change_pct = float(cfg.get("fallback_min_change_pct", 1.0) or 1.0)
    max_new = max(int(cfg.get("max_new_per_scan", 3) or 3), 0)
    candidates: List[Dict[str, Any]] = []

    for symbol in cn_equity_discovery_universe(cfg):
        ticker = normalize_market_symbol(symbol, "yfinance", "cn_equity")
        try:
            history = yf.download(
                ticker,
                period="15d",
                interval="1d",
                auto_adjust=False,
                progress=False,
                threads=False,
            )
        except Exception:
            continue
        if history is None or history.empty:
            continue

        close_series = history["Close"] if "Close" in history.columns else None
        volume_series = history["Volume"] if "Volume" in history.columns else None
        if close_series is None or volume_series is None:
            continue
        close_series = pd.to_numeric(close_series, errors="coerce").dropna()
        volume_series = pd.to_numeric(volume_series, errors="coerce").fillna(0)
        if close_series.empty or len(close_series) < 2:
            continue

        last_price = safe_float(close_series.iloc[-1])
        prev_price = safe_float(close_series.iloc[-2])
        if last_price is None or last_price < min_price:
            continue
        change_pct = None
        if prev_price not in (None, 0):
            change_pct = abs((float(last_price) - float(prev_price)) / float(prev_price) * 100.0)
        if change_pct is None or change_pct < min_change_pct:
            continue

        notional_series = (close_series.tail(5) * volume_series.tail(5)).dropna()
        avg_amount = safe_float(notional_series.mean()) if not notional_series.empty else None
        if min_amount is not None and (avg_amount is None or avg_amount < min_amount):
            continue

        candidates.append(
            {
                "symbol": symbol,
                "market": "cn_equity",
                "name": ticker,
                "last": last_price,
                "amount": avg_amount,
                "turnover_pct": None,
                "change_pct": change_pct,
                "selection_source": "candidate_universe_yfinance",
            }
        )

    candidates.sort(
        key=lambda item: (
            safe_float(item.get("amount")) or 0.0,
            safe_float(item.get("change_pct")) or 0.0,
        ),
        reverse=True,
    )
    return candidates[:max_new]


def discover_cn_equity_candidates(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    import akshare as ak

    fetchers = [
        getattr(ak, "stock_zh_a_spot_em", None),
        getattr(ak, "stock_zh_a_spot", None),
    ]
    df = None
    fetch_errors: List[str] = []
    with temporarily_clear_proxy_env():
        for fetcher in fetchers:
            if callable(fetcher):
                try:
                    df = fetcher()
                except Exception as exc:
                    fetch_errors.append(f"{type(exc).__name__}: {exc}")
                    df = None
                    continue
                if df is not None and not df.empty:
                    break
    if df is None or df.empty:
        fallback_candidates = discover_cn_equity_candidates_from_seed_universe(cfg)
        if fallback_candidates:
            return fallback_candidates
        if fetch_errors:
            raise RuntimeError(fetch_errors[0])
        return []

    min_turnover = float(cfg.get("min_turnover", 2.0) or 2.0)
    min_price = float(cfg.get("min_price", 1.0) or 1.0)
    min_amount = safe_float(cfg.get("min_amount"))
    min_volume = safe_float(cfg.get("min_volume"))
    max_new = max(int(cfg.get("max_new_per_scan", 3) or 3), 0)

    working = df.copy()
    for column in ("最新价", "成交额", "成交量", "换手率"):
        if column in working.columns:
            working[column] = pd.to_numeric(working[column], errors="coerce")
    if "名称" not in working.columns or "代码" not in working.columns:
        return []
    working = working.dropna(subset=["代码", "名称", "最新价"])
    working = working[working["最新价"] >= min_price]
    if "换手率" in working.columns:
        working = working[working["换手率"].fillna(0) >= min_turnover]
    if min_amount is not None and "成交额" in working.columns:
        working = working[working["成交额"].fillna(0) >= min_amount]
    if min_volume is not None and "成交量" in working.columns:
        working = working[working["成交量"].fillna(0) >= min_volume]
    working = working[~working["名称"].astype(str).str.contains("ST|退", regex=True, na=False)]

    sort_column = "成交额" if "成交额" in working.columns else ("成交量" if "成交量" in working.columns else "最新价")
    working = working.sort_values(sort_column, ascending=False)
    candidates: List[Dict[str, Any]] = []
    for _, row in working.head(max_new).iterrows():
        symbol = extract_cn_equity_code(str(row.get("代码") or ""))
        if not symbol:
            continue
        candidates.append(
            {
                "symbol": symbol,
                "market": "cn_equity",
                "name": str(row.get("名称") or ""),
                "last": safe_float(row.get("最新价")),
                "amount": safe_float(row.get("成交额")),
                "turnover_pct": safe_float(row.get("换手率")),
            }
        )
    return candidates


def action_cleanup_assets(payload: Dict[str, Any]) -> Dict[str, Any]:
    rules = load_rules(payload)
    root_cfg = load_root_config()
    dry_run = bool(payload.get("dry_run", False))
    if dry_run:
        rules = load_yaml(get_rules_path(payload))
        rules = deep_merge(default_rules(), rules)
    removed = cleanup_dynamic_assets(rules, root_cfg=root_cfg, events=load_decision_events(rules))
    if removed and not dry_run:
        save_rules(rules, get_rules_path(payload))
    return {
        "ok": True,
        "changed": bool(removed),
        "dry_run": dry_run,
        "removed": removed,
        "removed_count": len(removed),
        "total_symbols": len(rules.get("agent", {}).get("symbols", [])),
        "auto_discover": auto_discover_summary(rules, root_cfg),
    }


def action_discover_assets(payload: Dict[str, Any]) -> Dict[str, Any]:
    rules = load_rules(payload)
    root_cfg = load_root_config()
    dry_run = bool(payload.get("dry_run", False))
    if dry_run:
        rules = load_yaml(get_rules_path(payload))
        rules = deep_merge(default_rules(), rules)

    cfg = auto_discover_config(rules, root_cfg)
    if not cfg.get("enabled", True):
        return {"ok": True, "enabled": False, "message": "auto_discover disabled", "action": "discover_assets"}

    now_ts = now_local(rules)
    existing_symbols = set(str(symbol) for symbol in rules.get("agent", {}).get("symbols", []))
    removed = cleanup_dynamic_assets(rules, root_cfg=root_cfg, events=load_decision_events(rules), now_ts=now_ts) if cfg.get("cleanup_on_discovery", True) else []
    added_crypto: List[str] = []
    added_cn: List[str] = []
    errors: List[Dict[str, str]] = []

    crypto_cfg = cfg.get("crypto", {}) or {}
    if crypto_cfg.get("enabled", True):
        try:
            crypto_candidates = [candidate for candidate in discover_crypto_candidates(rules, root_cfg, crypto_cfg) if candidate.get("symbol") not in existing_symbols]
            added_crypto = add_symbols_to_rules(rules, crypto_candidates, market="crypto", market_cfg=crypto_cfg, discovered_at=now_ts)
            existing_symbols.update(added_crypto)
        except Exception as exc:
            errors.append({"market": "crypto", "error": f"{type(exc).__name__}: {exc}"})

    cn_cfg = cfg.get("cn_equity", {}) or {}
    if cn_cfg.get("enabled", True):
        try:
            cn_candidates = [candidate for candidate in discover_cn_equity_candidates(cn_cfg) if candidate.get("symbol") not in existing_symbols]
            added_cn = add_symbols_to_rules(rules, cn_candidates, market="cn_equity", market_cfg=cn_cfg, discovered_at=now_ts)
            existing_symbols.update(added_cn)
        except Exception as exc:
            errors.append({"market": "cn_equity", "error": f"{type(exc).__name__}: {exc}"})

    trimmed = trim_excess_symbols(rules, int(cfg.get("max_total_symbols", 15) or 15))
    if trimmed:
        removed.extend(
            {
                "symbol": symbol,
                "market": (removed_dynamic_assets_registry(rules).get(symbol) or {}).get("market"),
                "reasons": ["max_total_symbols exceeded during auto discovery"],
            }
            for symbol in trimmed
        )

    if (added_crypto or added_cn or removed) and not dry_run:
        save_rules(rules, get_rules_path(payload))

    return {
        "ok": True,
        "enabled": True,
        "dry_run": dry_run,
        "added_crypto": added_crypto,
        "added_cn": added_cn,
        "removed": removed,
        "errors": errors,
        "partial_failure": bool(errors),
        "total_symbols": len(rules.get("agent", {}).get("symbols", [])),
        "auto_discover": auto_discover_summary(rules, root_cfg),
    }


def action_optimize_strategy(payload: Dict[str, Any]) -> Dict[str, Any]:
    rules = load_rules(payload)
    root_cfg = load_root_config()
    cfg = optimization_config(rules, root_cfg)
    dry_run = bool(payload.get("dry_run", False))
    lookback_events = int(payload.get("lookback_events") or cfg.get("lookback_events", 200))
    lookback_days = int(payload.get("lookback_days") or cfg.get("lookback_days", 30))

    if not cfg.get("enabled", True):
        report = {
            "generated_at": now_local(rules).isoformat(),
            "enabled": False,
            "changed": False,
            "changes": [],
            "summary": {
                "reason": "optimization disabled",
                "analyzed_symbols": 0,
                "executed_trades": 0,
                "pnl_samples": 0,
            },
        }
        report_path = write_optimization_report(report, rules, root_cfg)
        return {
            "ok": True,
            "enabled": False,
            "changed": False,
            "changes": [],
            "report_path": str(report_path),
        }

    cutoff = now_local(rules) - timedelta(days=lookback_days)
    filtered_events = [
        event
        for event in load_decision_events(rules)
        if (parse_event_timestamp(event.get("timestamp"), rules) or now_local(rules)) >= cutoff
    ]
    recent_events = filtered_events[-lookback_events:]
    symbols = sorted(
        {
            *{
                str(event.get("symbol") or "").strip()
                for event in recent_events
                if str(event.get("symbol") or "").strip()
            },
            *{
                str(symbol or "").strip()
                for symbol in rules.get("agent", {}).get("symbols", [])
                if str(symbol or "").strip()
            },
        }
    )

    min_executed = int(cfg.get("min_executed_trades", 3))
    min_pnl_samples = int(cfg.get("min_pnl_samples", 3))
    low_win_rate = float(cfg.get("low_win_rate", 0.4))
    high_win_rate = float(cfg.get("high_win_rate", 0.6))
    negative_avg = float(cfg.get("negative_avg_pnl_ratio", -0.002))
    positive_avg = float(cfg.get("positive_avg_pnl_ratio", 0.003))
    reduce_factor = float(cfg.get("reduce_order_size_factor", 0.9))
    increase_factor = float(cfg.get("increase_order_size_factor", 1.05))
    combined_fast_candidates = [int(item) for item in (cfg.get("combined_fast_candidates") or [3, 5, 8])]
    combined_slow_candidates = [int(item) for item in (cfg.get("combined_slow_candidates") or [10, 15, 20, 30])]
    combined_rsi_upper_candidates = [float(item) for item in (cfg.get("combined_rsi_upper_candidates") or [65, 70, 75])]
    combined_backtest_lookback = int(cfg.get("combined_backtest_lookback", 320) or 320)
    min_backtest_trades = int(cfg.get("combined_min_backtest_trades", 3) or 3)

    stats_by_symbol: Dict[str, Dict[str, Any]] = {}
    changes: List[Dict[str, Any]] = []
    for symbol in symbols:
        stats = summarize_symbol_performance(symbol, recent_events, rules)
        stats_by_symbol[symbol] = stats
        strategy_cfg = strategy_config_for_symbol(symbol, rules, {"symbol": symbol, "market": stats.get("market")})
        strategy_kind = str(strategy_cfg.get("kind") or "")
        market = str(stats["market"])

        if strategy_kind == "combined":
            try:
                settings = market_settings_for_symbol(symbol, rules, {"symbol": symbol, "market": market})
                interval = str(settings.get("timeframe") or "1h")
                lookback = max(
                    combined_backtest_lookback,
                    int(strategy_cfg.get("trend_filter_ema_period", 200) or 200) + 20,
                )
                history_df, _ = fetch_market_dataframe({"symbol": symbol, "market": market}, rules, symbol, interval, lookback)
            except Exception:
                history_df = None
            if history_df is not None and not history_df.empty:
                current_fast = int(strategy_cfg.get("fast", 5) or 5)
                current_slow = int(strategy_cfg.get("slow", 20) or 20)
                current_rsi_upper = float(strategy_cfg.get("rsi_upper", 70) or 70)
                current_backtest = backtest_combined(
                    history_df,
                    current_fast,
                    current_slow,
                    current_rsi_upper,
                    rsi_period=int(strategy_cfg.get("rsi_period", 14) or 14),
                    trend_filter_ema_period=int(strategy_cfg.get("trend_filter_ema_period", 200) or 200),
                    volume_multiplier=float(strategy_cfg.get("volume_multiplier", 1.5) or 1.5),
                    volume_lookback=int(strategy_cfg.get("volume_lookback", 20) or 20),
                )
                best = dict(current_backtest)
                best_params = (current_fast, current_slow, current_rsi_upper)
                for fast_candidate in combined_fast_candidates:
                    for slow_candidate in combined_slow_candidates:
                        if slow_candidate <= fast_candidate:
                            continue
                        for rsi_upper_candidate in combined_rsi_upper_candidates:
                            candidate = backtest_combined(
                                history_df,
                                fast_candidate,
                                slow_candidate,
                                rsi_upper_candidate,
                                rsi_period=int(strategy_cfg.get("rsi_period", 14) or 14),
                                trend_filter_ema_period=int(strategy_cfg.get("trend_filter_ema_period", 200) or 200),
                                volume_multiplier=float(strategy_cfg.get("volume_multiplier", 1.5) or 1.5),
                                volume_lookback=int(strategy_cfg.get("volume_lookback", 20) or 20),
                            )
                            if int(candidate.get("trade_count", 0)) < min_backtest_trades:
                                continue
                            better_return = float(candidate.get("total_return", -999.0)) > float(best.get("total_return", -999.0)) + 1e-9
                            better_win_rate = (
                                abs(float(candidate.get("total_return", -999.0)) - float(best.get("total_return", -999.0))) <= 1e-9
                                and float(candidate.get("win_rate", 0.0)) > float(best.get("win_rate", 0.0))
                            )
                            if better_return or better_win_rate:
                                best = candidate
                                best_params = (fast_candidate, slow_candidate, float(rsi_upper_candidate))
                stats["combined_backtest"] = {
                    "current": {
                        "fast": current_fast,
                        "slow": current_slow,
                        "rsi_upper": current_rsi_upper,
                        **current_backtest,
                    },
                    "best": {
                        "fast": best_params[0],
                        "slow": best_params[1],
                        "rsi_upper": best_params[2],
                        **best,
                    },
                }
                if best_params != (current_fast, current_slow, current_rsi_upper):
                    param_change = {
                        "symbol": symbol,
                        "market": market,
                        "change_type": "strategy_params",
                        "strategy_name": stats.get("strategy_name"),
                        "from_params": {
                            "fast": current_fast,
                            "slow": current_slow,
                            "rsi_upper": current_rsi_upper,
                        },
                        "to_params": {
                            "fast": best_params[0],
                            "slow": best_params[1],
                            "rsi_upper": best_params[2],
                        },
                        "backtest_total_return": best.get("total_return"),
                        "backtest_win_rate": best.get("win_rate"),
                        "backtest_trade_count": best.get("trade_count"),
                        "reason": "combined strategy parameter search found a stronger historical configuration",
                    }
                    changes.append(param_change)
                    if not dry_run:
                        set_combined_strategy_params_override(rules, symbol, best_params[0], best_params[1], best_params[2])

        avg_ratio = stats.get("avg_pnl_ratio")
        win_rate = stats.get("win_rate")
        if (
            stats["executed_trades"] < min_executed
            or stats["pnl_samples"] < min_pnl_samples
            or avg_ratio is None
            or win_rate is None
        ):
            continue

        current_size = float(stats["current_order_size"])
        factor = None
        reason = ""
        if win_rate < low_win_rate or avg_ratio < negative_avg:
            factor = reduce_factor
            reason = "recent executed trades underperformed; reducing order size"
        elif win_rate > high_win_rate and avg_ratio > positive_avg:
            factor = increase_factor
            reason = "recent executed trades performed well; increasing order size slightly"

        if factor is None:
            continue

        proposed_size = normalize_optimized_order_size(current_size * factor, market, cfg)
        if abs(proposed_size - current_size) < (1e-9 if market == "crypto" else 1.0):
            continue

        change = {
            "symbol": symbol,
            "market": market,
            "change_type": "order_size",
            "strategy_name": stats.get("strategy_name"),
            "from_order_size": current_size,
            "to_order_size": proposed_size,
            "avg_pnl_ratio": avg_ratio,
            "win_rate": win_rate,
            "executed_trades": stats["executed_trades"],
            "pnl_samples": stats["pnl_samples"],
            "reason": reason,
        }
        changes.append(change)
        if not dry_run:
            set_order_size_override(rules, symbol, market, proposed_size)

    rules_path = get_rules_path(payload)
    if changes and not dry_run:
        save_rules(rules, rules_path)

    executed_total = sum(int(stats["executed_trades"]) for stats in stats_by_symbol.values())
    pnl_sample_total = sum(int(stats["pnl_samples"]) for stats in stats_by_symbol.values())
    report = {
        "generated_at": now_local(rules).isoformat(),
        "enabled": True,
        "dry_run": dry_run,
        "changed": bool(changes),
        "changes": changes,
        "summary": {
            "analyzed_symbols": len(symbols),
            "executed_trades": executed_total,
            "pnl_samples": pnl_sample_total,
            "lookback_events": lookback_events,
            "lookback_days": lookback_days,
        },
        "stats_by_symbol": stats_by_symbol,
        "rules_path": str(rules_path),
    }
    report_path = write_optimization_report(report, rules, root_cfg)

    if changes and notification_enabled(root_cfg, "on_order_filled", True):
        lines = [
            "[OpenClaw 绛栫暐浼樺寲]",
            f"鍙樻洿鏁伴噺: {len(changes)}",
        ]
        for change in changes[:5]:
            if change.get("change_type") == "strategy_params":
                lines.append(
                    f"{change['symbol']}: params {change['from_params']} -> {change['to_params']} "
                    f"(ret={float(change.get('backtest_total_return') or 0.0):.4f}, win_rate={float(change.get('backtest_win_rate') or 0.0):.2f})"
                )
            else:
                lines.append(
                    f"{change['symbol']}: {change['from_order_size']} -> {change['to_order_size']} "
                    f"(win_rate={change['win_rate']:.2f}, avg_pnl_ratio={change['avg_pnl_ratio']:.4f})"
                )
        send_operator_notification("\n".join(lines), root_cfg=root_cfg)

    return {
        "ok": True,
        "changed": bool(changes),
        "changes": changes,
        "stats_by_symbol": stats_by_symbol,
        "report_path": str(report_path),
        "rules_path": str(rules_path),
        "dry_run": dry_run,
    }


def action_record_trading_decision(payload: Dict[str, Any]) -> Dict[str, Any]:
    rules = load_rules(payload)
    root_cfg = load_root_config()
    status = str(payload["status"]).lower()
    send_error_notification = False
    event = {
        "timestamp": now_local(rules).isoformat(),
        "status": status,
        "symbol": payload.get("symbol"),
        "side": payload.get("side"),
        "message": payload.get("message", ""),
        "details": payload.get("details", {}),
    }
    with runtime_lock(rules):
        state = load_state(rules)
        append_decision(event, rules)

        if status in {"error", "failed"}:
            state = update_failure_state(state, rules, event["message"])
            send_error_notification = should_send_error_notification(
                state,
                root_cfg,
                error_notification_key(
                    status,
                    symbol=str(payload.get("symbol") or ""),
                    side=str(payload.get("side") or ""),
                    message=str(payload.get("message") or ""),
                ),
                now_local(rules),
            )
        else:
            state = reset_failure_state(state)

        recent_events = state.setdefault("recent_events", [])
        recent_events.append(event)
        state["recent_events"] = recent_events[-20:]
        save_state(state, rules)

    if (
        status in {"error", "failed"}
        and send_error_notification
        and notification_enabled(root_cfg, "on_error", True)
    ):
        symbol = payload.get("symbol") or "N/A"
        side = payload.get("side") or "N/A"
        err_msg = payload.get('message', '') or '未提供'
        send_operator_notification(
            "\n".join(
                [
                    f"[OpenClaw 交易异常] {symbol} {err_msg[:60]}",
                    f"状态: {status}",
                    f"标的: {symbol}",
                    f"方向: {side}",
                    f"原因: {err_msg}",
                ]
            ),
            root_cfg=root_cfg,
        )

    return {"ok": True, "state": state, "recorded": event}


def action_reconcile_approvals(payload: Dict[str, Any]) -> Dict[str, Any]:
    rules = load_rules(payload)
    root_cfg = load_root_config()
    recovery_cfg = approval_recovery_config(root_cfg)
    if not recovery_cfg["enabled"]:
        return {
            "ok": True,
            "recovery_enabled": False,
            "scanned": 0,
            "recovered": 0,
            "results": [],
        }

    timeout_seconds = int(root_cfg.get("gatekeeper", {}).get("timeout_seconds", 300))
    grace_seconds = int(payload.get("grace_seconds", recovery_cfg["grace_seconds"]))
    max_age_minutes = int(payload.get("max_age_minutes", recovery_cfg["max_age_minutes"]))
    cutoff_local = now_local(rules) - timedelta(minutes=max_age_minutes)
    stale_before = now_local(rules) - timedelta(seconds=timeout_seconds + grace_seconds)
    approver_targets = collect_feishu_targets(root_cfg.get("gatekeeper", {}))

    results: List[Dict[str, Any]] = []
    scanned = 0
    recovered = 0

    for request_id, record in load_approval_requests().items():
        status = str(record.get("status", "")).strip().lower()
        if status != "pending":
            continue

        created_at = parse_approval_state_timestamp(record.get("created_at"), rules)
        if created_at is None or created_at < cutoff_local or created_at > stale_before:
            continue

        scanned += 1
        decision, reply_text = find_feishu_reply(
            request_id,
            approver_targets,
            created_at.astimezone(timezone.utc),
        )
        order_payload = approval_record_to_payload(record, rules_file=payload.get("rules_file", ""))

        if decision is True:
            update_approval_request(
                request_id,
                status="recovering",
                response_text=reply_text,
                recovered_at=approval_now_ts(),
            )
            risk_result = action_check_risk(order_payload)
            if not risk_result.get("allowed"):
                update_approval_request(
                    request_id,
                    status="risk_blocked",
                    risk=risk_result,
                )
                results.append(
                    persist_rejected_result(
                        payload=order_payload,
                        rules=rules,
                        root_cfg=root_cfg,
                        request_id=request_id,
                        status="blocked",
                        message="审批恢复后风控拦截，订单未执行",
                        details=risk_result,
                    )
                )
                continue

            recovered += 1
            execution_result = execute_approved_order(
                payload=order_payload,
                rules=rules,
                root_cfg=root_cfg,
                request_id=request_id,
                risk_result=risk_result,
            )
            execution_result["recovered"] = True
            results.append(execution_result)
            continue

        if decision is False:
            update_approval_request(
                request_id,
                status="rejected",
                response_text=reply_text,
            )
            results.append(
                persist_rejected_result(
                    payload=order_payload,
                    rules=rules,
                    root_cfg=root_cfg,
                    request_id=request_id,
                    status="rejected",
                    message="审批恢复扫描识别为拒绝，订单未执行",
                )
            )
            continue

        if recovery_cfg["mark_timeouts"]:
            update_approval_request(request_id, status="timed_out")
            results.append(
                {
                    "request_id": request_id,
                    "status": "timed_out",
                    "message": "Pending approval remained unanswered after timeout window",
                }
            )

    return {
        "ok": True,
        "recovery_enabled": True,
        "scanned": scanned,
        "recovered": recovered,
        "results": results,
    }


ACTIONS = {
    "get_portfolio_snapshot": action_get_portfolio_snapshot,
    "get_trading_state": action_get_trading_state,
    "get_review_universe": action_get_review_universe,
    "discover_assets": action_discover_assets,
    "cleanup_assets": action_cleanup_assets,
    "update_macro_state": action_update_macro_state,
    "get_macro_state": action_get_macro_state,
    "get_market_data": action_get_market_data,
    "calculate_indicator": action_calculate_indicator,
    "generate_signal": action_generate_signal,
    "check_risk": action_check_risk,
    "place_order": action_place_order,
    "reconcile_approvals": action_reconcile_approvals,
    "optimize_strategy": action_optimize_strategy,
    "record_trading_decision": action_record_trading_decision,
}


def main() -> int:
    load_runtime_env()
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "Missing action name"}, ensure_ascii=False))
        return 1

    action = sys.argv[1]
    if action not in ACTIONS:
        print(json.dumps({"ok": False, "error": f"Unsupported action: {action}"}, ensure_ascii=False))
        return 1

    payload = read_payload()
    try:
        result = ACTIONS[action](payload)
        result.setdefault("ok", True)
        result["action"] = action
        print(json.dumps(to_jsonable(result), ensure_ascii=False))
        return 0
    except Exception as exc:
        rules = load_rules(payload)
        root_cfg = load_root_config()
        send_error_notification = False
        with runtime_lock(rules):
            state = load_state(rules)
            state = update_failure_state(state, rules, f"{type(exc).__name__}: {exc}")
            send_error_notification = should_send_error_notification(
                state,
                root_cfg,
                error_notification_key(
                    f"action:{action}",
                    message=f"{type(exc).__name__}: {exc}",
                ),
                now_local(rules),
            )
            save_state(state, rules)
        if send_error_notification and notification_enabled(root_cfg, "on_error", True):
            send_operator_notification(
                "\n".join(
                    [
                        f"[OpenClaw 交易异常] {action} {type(exc).__name__}: {exc}"[:80],
                        f"动作: {action}",
                        f"错误: {type(exc).__name__}: {exc}",
                    ]
                ),
                root_cfg=root_cfg,
            )
        print(
            json.dumps(
                {
                    "ok": False,
                    "action": action,
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                },
                ensure_ascii=False,
            )
        )
        return 1


def _install_generate_signal_missing_indicator_guard():
    original = globals().get("action_generate_signal")
    if not callable(original):
        return
    if getattr(original, "_openclaw_missing_indicator_guard", False):
        return

    def guarded_action_generate_signal(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except KeyError as exc:
            missing = str(exc).strip("'\"")
            guarded_keys = {
                "ema_fast_prev",
                "ema_slow_prev",
                "ma_fast_prev",
                "ma_slow_prev",
                "bb_upper",
                "bb_lower",
                "bb_middle",
                "rsi",
            }
            if missing not in guarded_keys:
                raise

            payload = {}
            for candidate in args:
                if isinstance(candidate, dict):
                    payload = candidate
                    break
            if not payload and isinstance(kwargs.get("payload"), dict):
                payload = kwargs.get("payload") or {}

            symbol = payload.get("symbol")
            market = payload.get("market")
            try:
                if not market and callable(globals().get("infer_market")):
                    market = globals()["infer_market"](symbol)
            except Exception:
                market = market or None

            strategy_name = (
                payload.get("strategy_name")
                or payload.get("strategy")
                or payload.get("strategy_id")
                or "unknown"
            )
            quantity = payload.get("quantity") or 0
            position_scale = payload.get("position_scale")
            try:
                position_scale = float(position_scale if position_scale is not None else 1.0)
            except Exception:
                position_scale = 1.0

            reason = (
                f"Indicator snapshot incomplete ({missing}); defaulting to HOLD to avoid false trade."
            )
            result = {
                "ok": True,
                "status": "hold",
                "signal": "HOLD",
                "decision": "HOLD",
                "action": "hold",
                "side": "hold",
                "symbol": symbol,
                "market": market,
                "strategy_name": strategy_name,
                "quantity": quantity,
                "order_quantity": 0,
                "position_scale": position_scale,
                "reason": reason,
                "reasons": [reason],
                "meta": {
                    "guarded_missing_indicator": missing,
                    "guardrail": "missing_indicator_defaults_to_hold",
                },
            }
            return result

    guarded_action_generate_signal._openclaw_missing_indicator_guard = True
    globals()["action_generate_signal"] = guarded_action_generate_signal

    for registry_name in (
        "ACTION_MAP",
        "ACTION_HANDLERS",
        "ACTIONS",
        "ACTION_DISPATCH",
        "ACTION_REGISTRY",
    ):
        registry = globals().get(registry_name)
        if isinstance(registry, dict) and "generate_signal" in registry:
            registry["generate_signal"] = guarded_action_generate_signal


_install_generate_signal_missing_indicator_guard()


def _install_optimize_strategy_oserror_guard():
    original = globals().get("action_optimize_strategy")
    if not callable(original):
        return
    if getattr(original, "_openclaw_oserror_guard", False):
        return

    def guarded_action_optimize_strategy(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except OSError as exc:
            payload = {}
            for candidate in args:
                if isinstance(candidate, dict):
                    payload = candidate
                    break
            if not payload and isinstance(kwargs.get("payload"), dict):
                payload = kwargs.get("payload") or {}

            try:
                rules = load_rules(payload)
            except Exception:
                rules = default_rules()
            try:
                root_cfg = load_root_config()
            except Exception:
                root_cfg = {}

            report = {
                "generated_at": now_local(rules).isoformat(),
                "enabled": True,
                "dry_run": bool(payload.get("dry_run", False)),
                "changed": False,
                "changes": [],
                "summary": {
                    "reason": f"optimization skipped because of os error: {exc}",
                    "analyzed_symbols": 0,
                    "executed_trades": 0,
                    "pnl_samples": 0,
                },
                "error": str(exc),
            }
            report_path = None
            try:
                report_path = write_optimization_report(report, rules, root_cfg)
            except Exception:
                report_path = None
            return {
                "ok": True,
                "enabled": True,
                "changed": False,
                "changes": [],
                "skipped": True,
                "error": str(exc),
                "report_path": str(report_path) if report_path else None,
            }

    guarded_action_optimize_strategy._openclaw_oserror_guard = True
    globals()["action_optimize_strategy"] = guarded_action_optimize_strategy

    for registry_name in (
        "ACTION_MAP",
        "ACTION_HANDLERS",
        "ACTIONS",
        "ACTION_DISPATCH",
        "ACTION_REGISTRY",
    ):
        registry = globals().get(registry_name)
        if isinstance(registry, dict) and "optimize_strategy" in registry:
            registry["optimize_strategy"] = guarded_action_optimize_strategy


_install_optimize_strategy_oserror_guard()


if __name__ == "__main__":
    raise SystemExit(main())
