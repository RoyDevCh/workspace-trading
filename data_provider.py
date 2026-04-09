"""
OpenClaw 感官模块 - 行情数据获取层
统一接口：get_market_data(symbol, timeframe, start_date, end_date)
支持多源：A股(Tushare/AKShare)、加密货币(CCXT)、美股(Yahoo Finance)、TradingView
"""
import os
import json
import re
import time
import pandas as pd
from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union
import logging

logger = logging.getLogger(__name__)


_MARKET_CACHE_ROOT = Path(os.getenv("OPENCLAW_MARKET_CACHE", r"C:\Users\Roy\.openclaw\cache\market_data"))


def _resolve_http_proxies() -> Dict[str, str]:
    proxies: Dict[str, str] = {}
    http_proxy = os.getenv("HTTP_PROXY") or os.getenv("http_proxy")
    https_proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or http_proxy

    if http_proxy:
        proxies["http"] = http_proxy
    if https_proxy:
        proxies["https"] = https_proxy

    return proxies


def _normalize_ccxt_market_type(value: Optional[str]) -> str:
    lowered = str(value or "spot").strip().lower()
    if lowered in {"future", "futures", "perpetual", "swap", "usdm", "usd-m"}:
        return "future"
    return "spot"


def _resolve_exchange_secret(
    exchange_id: str,
    field: str,
    testnet: bool = False,
    market_type: str = "spot",
) -> Optional[str]:
    exchange_key = str(exchange_id or "").strip().upper()
    market_type = _normalize_ccxt_market_type(market_type)
    candidates: List[str] = []

    if exchange_key == "BINANCE" and market_type == "future":
        if testnet:
            candidates.extend(
                [
                    f"{exchange_key}_FUTURES_TESTNET_{field}",
                    f"{exchange_key}_FUTURE_TESTNET_{field}",
                    f"{exchange_key}_USDM_TESTNET_{field}",
                ]
            )
        candidates.extend(
            [
                f"{exchange_key}_FUTURES_{field}",
                f"{exchange_key}_FUTURE_{field}",
                f"{exchange_key}_USDM_{field}",
            ]
        )

    if testnet:
        candidates.extend(
            [
                f"{exchange_key}_TESTNET_{field}",
                f"{exchange_key}_SANDBOX_{field}",
            ]
        )

    candidates.append(f"{exchange_key}_{field}")

    for candidate in candidates:
        value = os.getenv(candidate)
        if value:
            return value
    return None


def _normalize_ccxt_symbol(symbol: str, market_type: str = "spot", exchange_id: str = "") -> str:
    text = str(symbol or "").strip()
    if not text:
        return text
    if _normalize_ccxt_market_type(market_type) != "future":
        return text
    if exchange_id.lower() != "binance":
        return text
    if ":" in text or "/" not in text:
        return text

    base, quote = text.split("/", 1)
    if quote.upper() == "USDT":
        return f"{base}/{quote}:USDT"
    return text


def _apply_binance_futures_testnet_urls(exchange: object) -> None:
    try:
        urls = getattr(exchange, "urls", {})
        test_urls = urls.get("test", {}) if isinstance(urls, dict) else {}
        api_urls = urls.get("api", {}) if isinstance(urls, dict) else {}
        if not isinstance(test_urls, dict) or not isinstance(api_urls, dict):
            return
        for key in (
            "fapiPublic",
            "fapiPublicV2",
            "fapiPublicV3",
            "fapiPrivate",
            "fapiPrivateV2",
            "fapiPrivateV3",
            "fapiData",
        ):
            if test_urls.get(key):
                api_urls[key] = test_urls[key]
        exchange.urls["api"] = api_urls
    except Exception:
        return


_PROXY_ENV_KEYS = [
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
]


def _market_cache_path(
    provider_name: str,
    symbol: str,
    timeframe: str,
    start_date: Optional[str],
    end_date: Optional[str],
) -> Path:
    raw_key = f"{symbol}_{timeframe}_{start_date or 'auto'}_{end_date or 'auto'}"
    safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw_key).strip("_") or "market"
    return _MARKET_CACHE_ROOT / provider_name / f"{safe_key}.json"


def _write_market_cache(path: Path, df: pd.DataFrame) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "saved_at": datetime.utcnow().isoformat(),
            "records": json.loads(df.reset_index(drop=True).to_json(orient="records", date_format="iso")),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed to write market cache %s: %s", path, exc)


def _read_market_cache(path: Path) -> Optional[pd.DataFrame]:
    try:
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        records = payload.get("records") or []
        if not records:
            return None
        df = pd.DataFrame(records)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
        return df
    except Exception as exc:
        logger.warning("Failed to read market cache %s: %s", path, exc)
        return None


@contextmanager
def _without_http_proxy():
    saved = {key: os.environ.get(key) for key in _PROXY_ENV_KEYS if key in os.environ}
    for key in _PROXY_ENV_KEYS:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value


def _normalize_akshare_hist_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename_map = {
        "日期": "date",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
    }
    available_map = {column: rename_map[str(column).strip()] for column in df.columns if str(column).strip() in rename_map}
    normalized = df.rename(columns=available_map).copy()

    if "date" not in normalized.columns and len(normalized.columns) >= 6:
        fallback = list(normalized.columns)
        normalized = normalized.rename(columns={
            fallback[0]: "date",
            fallback[1]: "open",
            fallback[2]: "close",
            fallback[3]: "high",
            fallback[4]: "low",
            fallback[5]: "volume",
        })
        if len(fallback) >= 7:
            normalized = normalized.rename(columns={fallback[6]: "amount"})

    if "date" not in normalized.columns:
        raise KeyError("date")

    normalized["date"] = pd.to_datetime(normalized["date"])
    for column in ["open", "high", "low", "close", "volume", "amount"]:
        if column in normalized.columns:
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    return normalized


def _resolve_spot_code_column(df: pd.DataFrame) -> str:
    for candidate in ["代码", "code", "symbol"]:
        if candidate in df.columns:
            return candidate
    if len(df.columns) == 0:
        raise KeyError("code")
    return str(df.columns[0])


def _normalize_cn_symbol(symbol: str) -> str:
    text = str(symbol or "").strip()
    lowered = text.lower()
    if lowered.startswith(("sh", "sz", "bj")) and len(lowered) >= 8 and lowered[2:8].isdigit():
        return lowered[2:8]
    if "." in text:
        head = text.split(".", 1)[0]
        if head.isdigit() and len(head) >= 6:
            return head[-6:]
    if text.isdigit() and len(text) >= 6:
        return text[-6:]
    return text


def _looks_like_cn_etf_symbol(symbol: str) -> bool:
    code = _normalize_cn_symbol(symbol)
    return len(code) == 6 and code.isdigit() and code.startswith(("1", "5"))


def _looks_like_cn_index_symbol(symbol: str) -> bool:
    text = str(symbol or "").strip().lower()
    return text.startswith(("sh", "sz")) and len(text) == 8 and text[2:].isdigit()


class DataProvider(ABC):
    """数据提供者抽象基类"""

    @abstractmethod
    def get_historical_data(
        self,
        symbol: str,
        timeframe: str = "1d",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 1000
    ) -> pd.DataFrame:
        """获取历史K线数据"""
        pass

    @abstractmethod
    def get_real_time_quote(self, symbol: str) -> Dict:
        """获取实时行情"""
        pass

    @abstractmethod
    def get_symbols(self) -> List[str]:
        """获取可用标的列表"""
        pass


class TushareProvider(DataProvider):
    """A股数据源 - Tushare Pro"""

    def __init__(self, token: Optional[str] = None):
        """
        初始化 Tushare
        token: 从 https://tushare.pro 注册获取
        """
        self.token = token or os.getenv("TUSHARE_TOKEN")
        if not self.token:
            raise ValueError("TUSHARE_TOKEN 环境变量未设置")
        import tushare as ts
        ts.set_token(self.token)
        self.pro = ts.pro_api()

    def get_historical_data(
        self,
        symbol: str,
        timeframe: str = "1d",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 1000
    ) -> pd.DataFrame:
        """
        获取A股历史数据
        symbol: 股票代码，如 '000001.SZ'（深交所）或 '600000.SH'（上交所）
        timeframe: 仅支持日线 '1d'
        """
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")
        if end_date is None:
            end_date = datetime.now().strftime("%Y%m%d")

        df = self.pro.daily(
            ts_code=symbol,
            start_date=start_date,
            end_date=end_date,
            limit=limit
        )

        # 标准化列名
        df = df.rename(columns={
            'trade_date': 'date',
            'open': 'open',
            'high': 'high',
            'low': 'low',
            'close': 'close',
            'vol': 'volume',
            'amount': 'amount'
        })
        df['date'] = pd.to_datetime(df['date'])
        return df.sort_values('date')

    def get_real_time_quote(self, symbol: str) -> Dict:
        """获取实时行情（需要付费权限）"""
        df = self.pro.daily(
            ts_code=symbol,
            trade_date=datetime.now().strftime("%Y%m%d")
        )
        if df.empty:
            return {}
        return df.iloc[0].to_dict()

    def get_symbols(self, market: str = "all") -> List[str]:
        """获取股票列表"""
        df = self.pro.stock_basic(exchange="", list_status="L")
        return df["ts_code"].tolist()


class AKShareProvider(DataProvider):
    """A-share data provider backed by AKShare."""

    def __init__(self):
        try:
            import akshare as ak
            self.ak = ak
        except ImportError:
            raise ImportError("Please install akshare: pip install akshare")

    def _get_hist_candidates(self, symbol: str) -> List[tuple[str, Callable[[], pd.DataFrame]]]:
        code = _normalize_cn_symbol(symbol)
        candidates: List[tuple[str, Callable[[], pd.DataFrame]]] = []

        if _looks_like_cn_etf_symbol(code):
            candidates.append(
                (
                    "fund_etf_hist_em",
                    lambda: self.ak.fund_etf_hist_em(
                        symbol=code,
                        period="daily",
                        adjust="qfq",
                    ),
                )
            )

        if _looks_like_cn_index_symbol(symbol):
            index_symbol = str(symbol).strip().lower()
            candidates.append(
                (
                    "stock_zh_index_daily_em",
                    lambda: self.ak.stock_zh_index_daily_em(symbol=index_symbol),
                )
            )

        candidates.append(
            (
                "stock_zh_a_hist",
                lambda: self.ak.stock_zh_a_hist(
                    symbol=code,
                    period="daily",
                    start_date="",
                    end_date="",
                    adjust="qfq",
                ),
            )
        )
        return candidates

    def _get_quote_candidates(self, symbol: str) -> List[tuple[str, Callable[[], pd.DataFrame]]]:
        code = _normalize_cn_symbol(symbol)
        candidates: List[tuple[str, Callable[[], pd.DataFrame]]] = []
        if _looks_like_cn_etf_symbol(code):
            candidates.append(("fund_etf_spot_em", self.ak.fund_etf_spot_em))
        candidates.append(("stock_zh_a_spot_em", self.ak.stock_zh_a_spot_em))
        return candidates

    def get_historical_data(
        self,
        symbol: str,
        timeframe: str = "1d",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 1000
    ) -> pd.DataFrame:
        last_error: Optional[Exception] = None
        for source_name, loader in self._get_hist_candidates(symbol):
            for attempt in range(1, 4):
                try:
                    with _without_http_proxy():
                        df = loader()
                    df = _normalize_akshare_hist_columns(df)
                    if start_date:
                        df = df[df["date"] >= pd.to_datetime(start_date)]
                    if end_date:
                        df = df[df["date"] <= pd.to_datetime(end_date)]
                    if not df.empty:
                        logger.info(
                            f"AKShare historical data loaded via {source_name} for {symbol} on attempt {attempt}"
                        )
                        return df.tail(limit)
                except Exception as e:
                    last_error = e
                    logger.warning(
                        f"AKShare historical candidate {source_name} failed for {symbol} on attempt {attempt}: {e}"
                    )
                    time.sleep(0.5 * attempt)

        logger.error(f"AKShare failed to fetch historical data for {symbol}: {last_error}")
        return pd.DataFrame()

    def get_real_time_quote(self, symbol: str) -> Dict:
        code = _normalize_cn_symbol(symbol)
        last_error: Optional[Exception] = None
        for source_name, loader in self._get_quote_candidates(symbol):
            for attempt in range(1, 4):
                try:
                    with _without_http_proxy():
                        df = loader()
                    code_column = _resolve_spot_code_column(df)
                    row = df[df[code_column].astype(str) == code]
                    if not row.empty:
                        logger.info(
                            f"AKShare realtime quote loaded via {source_name} for {symbol} on attempt {attempt}"
                        )
                        return row.iloc[0].to_dict()
                except Exception as e:
                    last_error = e
                    logger.warning(
                        f"AKShare realtime candidate {source_name} failed for {symbol} on attempt {attempt}: {e}"
                    )
                    time.sleep(0.5 * attempt)
        if last_error is not None:
            logger.error(f"AKShare failed to fetch realtime quote for {symbol}: {last_error}")
        return {}

    def get_symbols(self) -> List[str]:
        try:
            with _without_http_proxy():
                stock_df = self.ak.stock_zh_a_spot_em()
            stock_code_column = _resolve_spot_code_column(stock_df)
            symbols = set(stock_df[stock_code_column].astype(str).tolist())

            with _without_http_proxy():
                etf_df = self.ak.fund_etf_spot_em()
            etf_code_column = _resolve_spot_code_column(etf_df)
            symbols.update(etf_df[etf_code_column].astype(str).tolist())
            return sorted(symbols)
        except Exception as e:
            logger.error(f"AKShare failed to fetch symbols: {e}")
            return []


class CCXTProvider(DataProvider):
    """加密货币数据源 - CCXT（统一多家交易所）"""

    def __init__(
        self,
        exchange_id: str = "binance",
        api_key: Optional[str] = None,
        secret: Optional[str] = None,
        testnet: bool = False,
        market_type: str = "spot",
    ):
        """
        初始化交易所连接
        exchange_id: 交易所ID，如 'binance', 'okex', 'huobi', 'coinbase'
        api_key/secret: 可选，用于交易权限
        """
        import ccxt
        self.exchange_id = exchange_id
        self.testnet = bool(testnet)
        self.market_type = _normalize_ccxt_market_type(market_type)
        exchange_class = getattr(ccxt, exchange_id)
        self.exchange = exchange_class({
            'apiKey': api_key or _resolve_exchange_secret(exchange_id, "API_KEY", testnet=self.testnet, market_type=self.market_type),
            'secret': secret or _resolve_exchange_secret(exchange_id, "SECRET", testnet=self.testnet, market_type=self.market_type),
            'enableRateLimit': True,
            'timeout': 30000,
            'options': {
                'defaultType': self.market_type,
                'fetchMarkets': {
                    'types': [self.market_type],
                },
            },
        })
        if self.market_type == "future" and self.exchange_id.lower() == "binance":
            self.exchange.options["defaultSubType"] = "linear"
        if self.testnet and hasattr(self.exchange, "set_sandbox_mode"):
            self.exchange.set_sandbox_mode(True)
        if self.testnet and self.market_type == "future" and self.exchange_id.lower() == "binance":
            _apply_binance_futures_testnet_urls(self.exchange)

        session = getattr(self.exchange, "session", None)
        proxies = _resolve_http_proxies()
        if session is not None and proxies:
            session.trust_env = True
            session.proxies.update(proxies)

    def get_historical_data(
        self,
        symbol: str,
        timeframe: str = "1d",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 1000
    ) -> pd.DataFrame:
        """
        获取加密货币历史数据
        symbol: 如 'BTC/USDT', 'ETH/USDT'
        timeframe: 如 '1m', '5m', '1h', '1d'
        """
        since = None
        if start_date:
            since = self.exchange.parse8601(start_date + "T00:00:00Z")

        ohlcv = self.exchange.fetch_ohlcv(
            symbol=_normalize_ccxt_symbol(symbol, self.market_type, self.exchange_id),
            timeframe=timeframe,
            since=since,
            limit=limit
        )

        df = pd.DataFrame(
            ohlcv,
            columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
        )
        df['date'] = pd.to_datetime(df['timestamp'], unit='ms')
        df = df.drop(columns=['timestamp'])

        if end_date:
            df = df[df['date'] <= pd.to_datetime(end_date)]

        return df

    def get_real_time_quote(self, symbol: str) -> Dict:
        """获取实时行情（ticker）"""
        ticker = self.exchange.fetch_ticker(
            _normalize_ccxt_symbol(symbol, self.market_type, self.exchange_id)
        )
        return {
            'symbol': symbol,
            'last': ticker.get('last'),
            'bid': ticker.get('bid'),
            'ask': ticker.get('ask'),
            'volume': ticker.get('baseVolume'),
            'change': ticker.get('change'),
            'percentage': ticker.get('percentage'),
            'timestamp': ticker.get('timestamp')
        }

    def get_symbols(self) -> List[str]:
        """获取交易所支持的交易对"""
        markets = self.exchange.load_markets()
        return list(markets.keys())


class YahooFinanceProvider(DataProvider):
    """通用股票数据源 - Yahoo Finance（免费）"""

    def __init__(self):
        """初始化 yfinance"""
        try:
            import yfinance as yf
            self.yf = yf
        except ImportError:
            raise ImportError("请安装 yfinance: pip install yfinance")

    def get_historical_data(
        self,
        symbol: str,
        timeframe: str = "1d",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 1000
    ) -> pd.DataFrame:
        """
        获取股票历史数据
        symbol: 股票代码，如 'AAPL', '000001.SS'（上证）, '000001.SZ'（深证）
        timeframe: 如 '1d', '1h', '5m'
        """
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        if end_date is None:
            end_date = datetime.now().strftime("%Y-%m-%d")

        ticker = self.yf.Ticker(symbol)
        df = ticker.history(
            start=start_date,
            end=end_date,
            interval=timeframe,
            auto_adjust=True
        )

        df = df.rename(columns={
            'Open': 'open',
            'High': 'high',
            'Low': 'low',
            'Close': 'close',
            'Volume': 'volume'
        })
        df.index.name = 'date'
        df = df.reset_index()

        return df.tail(limit)

    def get_real_time_quote(self, symbol: str) -> Dict:
        """获取实时行情"""
        ticker = self.yf.Ticker(symbol)
        info = ticker.info
        return {
            'symbol': symbol,
            'last': info.get('regularMarketPrice'),
            'open': info.get('open'),
            'high': info.get('dayHigh'),
            'low': info.get('dayLow'),
            'volume': info.get('regularMarketVolume'),
            'market_cap': info.get('marketCap'),
            'timestamp': datetime.now().isoformat()
        }

    def get_symbols(self) -> List[str]:
        """yfinance 不直接支持获取所有符号，返回空列表"""
        logger.warning("YahooFinance 不支持批量获取符号列表")
        return []


class DataManager:
    """数据管理器 - 统一接口"""

    def __init__(
        self,
        provider: Union[str, DataProvider],
        **provider_kwargs
    ):
        """
        初始化数据管理器
        provider: 数据源名称 ('tushare', 'akshare', 'ccxt', 'yfinance')
                  或 DataProvider 实例
        """
        if isinstance(provider, str):
            self.provider_name = provider
            self.provider = self._create_provider(provider, **provider_kwargs)
        else:
            self.provider_name = provider.__class__.__name__.lower()
            self.provider = provider

        self.cache: Dict[str, pd.DataFrame] = {}
        self.disk_cache_ttl = int(provider_kwargs.pop("disk_cache_ttl", 24 * 60 * 60) or 24 * 60 * 60)
        self.cache_ttl = 60  # 缓存60秒

    def _create_provider(self, name: str, **kwargs) -> DataProvider:
        """工厂方法创建数据源"""
        providers = {
            'tushare': TushareProvider,
            'akshare': AKShareProvider,
            'ccxt': CCXTProvider,
            'yfinance': YahooFinanceProvider,
            'sina': SinaRealtimeProvider,
        }
        if name not in providers:
            raise ValueError(f"不支持的数据源: {name}。可选: {list(providers.keys())}")
        return providers[name](**kwargs)

    def get_data(
        self,
        symbol: str,
        timeframe: str = "1d",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        use_cache: bool = True,
        **kwargs
    ) -> pd.DataFrame:
        """
        获取数据（带缓存）
        返回列: date, open, high, low, close, volume(, amount)
        """
        cache_key = f"{symbol}_{timeframe}_{start_date}_{end_date}"
        disk_cache_path = _market_cache_path(self.provider_name, symbol, timeframe, start_date, end_date)

        if use_cache and cache_key in self.cache:
            cached_time, df = self.cache[cache_key]
            if (datetime.now() - cached_time).seconds < self.cache_ttl:
                logger.debug(f"使用缓存数据: {symbol}")
                return df

        try:
            df = self.provider.get_historical_data(
                symbol=symbol,
                timeframe=timeframe,
                start_date=start_date,
                end_date=end_date,
                **kwargs
            )
        except Exception as exc:
            if use_cache:
                try:
                    cache_age_sec = time.time() - disk_cache_path.stat().st_mtime
                except OSError:
                    cache_age_sec = None
                if cache_age_sec is not None and cache_age_sec <= self.disk_cache_ttl:
                    cached_df = _read_market_cache(disk_cache_path)
                    if cached_df is not None and not cached_df.empty:
                        logger.warning("Using disk-cached market data for %s after fetch error: %s", symbol, exc)
                        self.cache[cache_key] = (datetime.now(), cached_df)
                        return cached_df
            raise

        if use_cache:
            self.cache[cache_key] = (datetime.now(), df)
            _write_market_cache(disk_cache_path, df)

        logger.info(f"获取到 {len(df)} 条 {symbol} 数据")
        return df

    def get_quote(self, symbol: str) -> Dict:
        """获取实时报价"""
        return self.provider.get_real_time_quote(symbol)

    def clear_cache(self):
        """清空缓存"""
        self.cache.clear()


# 便捷函数





def safe_float(val, default=0.0):
    """Safely convert value to float."""
    try:
        return float(val) if val is not None and str(val).strip() else default
    except (ValueError, TypeError):
        return default

class SinaRealtimeProvider(DataProvider):
    """A-share real-time data provider using Sina & Tencent finance APIs.
    
    These APIs are accessible from China mainland networks and provide
    real-time quotes during trading sessions. Falls back to AKShare for
    historical daily data when Sina doesn't provide bars.
    """

    def __init__(self):
        self._akshare_fallback = None

    def _get_akshare_fallback(self):
        if self._akshare_fallback is None:
            try:
                self._akshare_fallback = AKShareProvider()
            except ImportError:
                pass
        return self._akshare_fallback

    @staticmethod
    def _cn_symbol_to_sina(symbol: str) -> str:
        """Convert 510300/SH510300 -> sh510300 for Sina API."""
        code = _normalize_cn_symbol(symbol)
        raw = str(symbol).strip()
        if raw.lower().startswith("sh") or code.startswith("6") or code.startswith("5"):
            return f"sh{code}"
        elif raw.lower().startswith("sz") or code.startswith("0") or code.startswith("3"):
            return f"sz{code}"
        elif raw.lower().startswith("bj") or code.startswith("8") or code.startswith("4"):
            return f"bj{code}"
        return f"sh{code}"

    @staticmethod
    def _cn_symbol_to_tencent(symbol: str) -> str:
        """Convert 510300/SH510300 -> sh510300 for Tencent API."""
        return SinaRealtimeProvider._cn_symbol_to_sina(symbol)

    def _fetch_sina_realtime(self, symbol: str) -> dict | None:
        """Fetch real-time quote from Sina finance API."""
        import requests
        sina_code = self._cn_symbol_to_sina(symbol)
        url = f"https://hq.sinajs.cn/list={sina_code}"
        try:
            session = requests.Session()
            session.trust_env = False
            r = session.get(url, timeout=10, headers={
                'Referer': 'https://finance.sina.com.cn/',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            })
            if r.status_code == 200 and r.text.strip():
                # Parse: var hq_str_sh510300="name,open,prev_close,price,high,low,bid,ask,volume,amount,..."
                match = re.search(r'="([^"]*)"', r.text)
                if match:
                    fields = match.group(1).split(',')
                    if len(fields) >= 10:
                        return {
                            "name": fields[0],
                            "open": safe_float(fields[1]),
                            "prev_close": safe_float(fields[2]),
                            "last": safe_float(fields[3]),
                            "high": safe_float(fields[4]),
                            "low": safe_float(fields[5]),
                            "volume": safe_float(fields[8]),
                            "amount": safe_float(fields[9]),
                            "source": "sina",
                        }
        except Exception as e:
            logger.warning(f"SinaRealtimeProvider: Sina fetch failed for {symbol}: {e}")
        return None

    def _fetch_tencent_realtime(self, symbol: str) -> dict | None:
        """Fetch real-time quote from Tencent finance API."""
        import requests
        tx_code = self._cn_symbol_to_tencent(symbol)
        url = f"https://qt.gtimg.cn/q={tx_code}"
        try:
            session = requests.Session()
            session.trust_env = False
            r = session.get(url, timeout=10, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            })
            if r.status_code == 200 and r.text.strip():
                # Parse: v_sh510300="1~name~code~price~prev_close~open~volume~..."
                match = re.search(r'="([^"]*)"', r.text)
                if match:
                    fields = match.group(1).split('~')
                    if len(fields) >= 10:
                        return {
                            "name": fields[1],
                            "last": safe_float(fields[3]),
                            "prev_close": safe_float(fields[4]),
                            "open": safe_float(fields[5]),
                            "volume": safe_float(fields[6]),
                            "high": safe_float(fields[33]) if len(fields) > 33 else safe_float(fields[5]),
                            "low": safe_float(fields[34]) if len(fields) > 34 else safe_float(fields[5]),
                            "amount": safe_float(fields[37]) if len(fields) > 37 else 0,
                            "source": "tencent",
                        }
        except Exception as e:
            logger.warning(f"SinaRealtimeProvider: Tencent fetch failed for {symbol}: {e}")
        return None

    def get_historical_data(
        self,
        symbol: str,
        timeframe: str = "1d",
        start_date: str | None = None,
        end_date: str | None = None,
        **kwargs,
    ) -> pd.DataFrame:
        """Get historical data. Uses AKShare (with proxy bypass) for daily data."""
        with _without_http_proxy():
            fallback = self._get_akshare_fallback()
            if fallback:
                return fallback.get_historical_data(symbol, timeframe, start_date, end_date, **kwargs)
        # If AKShare also fails, return empty
        logger.warning(f"SinaRealtimeProvider: No historical data available for {symbol}")
        return pd.DataFrame()

    def get_real_time_quote(self, symbol: str) -> dict:
        """Get real-time quote. Tries Sina first, then Tencent, then AKShare."""
        # Try Sina first
        quote = self._fetch_sina_realtime(symbol)
        if quote and quote.get("last", 0) > 0:
            return quote
        # Try Tencent
        quote = self._fetch_tencent_realtime(symbol)
        if quote and quote.get("last", 0) > 0:
            return quote
        # Fallback to AKShare
        with _without_http_proxy():
            fallback = self._get_akshare_fallback()
            if fallback:
                try:
                    return fallback.get_real_time_quote(symbol)
                except Exception:
                    pass
        return {}

    def get_symbols(self) -> list:
        """Return common A-share symbols."""
        return ["510300", "159915", "510500", "510050", "512100"]



def create_data_manager(
    source: str = "yfinance",
    **kwargs
) -> DataManager:
    """
    快速创建数据管理器
    示例:
        dm = create_data_manager("tushare", token="your_token")
        dm = create_data_manager("ccxt", exchange_id="binance")
        dm = create_data_manager("akshare")
        dm = create_data_manager("yfinance")
        dm = create_data_manager("sina")
        dm = create_data_manager("gm", token="your_gm_token", gm_python="path/to/gm/python")
        dm = create_data_manager("tradingview", session="your_session_id")
    """
    return DataManager(source, **kwargs)


class TradingViewProvider(DataProvider):
    """
    TradingView 数据源 - 通过 session cookie 访问 TradingView Scanner API

    功能：
    - 获取实时行情和K线数据
    - 获取技术指标（RSI, MACD, Bollinger Bands）
    - 搜索符号、获取热门榜单
    - 支持美股、加密货币、外汇等 TradingView 覆盖的标的

    配置：
    设置环境变量 TRADINGVIEW_SESSION 或在初始化时传入 session 参数
    获取方法：浏览器登录 TradingView → DevTools → Application → Cookies → sessionid
    """

    BASE_URL = "https://scanner.tradingview.com"
    SYMBOL_URL = "https://symbol-search.tradingview.com/symbol_search"

    def __init__(self, session: Optional[str] = None):
        self.session = session or os.getenv("TRADINGVIEW_SESSION", "")
        if not self.session:
            raise ValueError(
                "TradingView session 未设置。请设置 TRADINGVIEW_SESSION 环境变量，"
                "或在浏览器中登录 TradingView 后从 Cookies 复制 sessionid"
            )
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Cookie": f"sessionid={self.session}",
        }

    def get_historical_data(
        self,
        symbol: str,
        timeframe: str = "1d",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 1000
    ) -> pd.DataFrame:
        """
        获取历史K线数据（TradingView 只提供有限历史，主要用于近期数据）

        注意：TradingView Scanner API 主要提供实时扫描数据，历史K线建议使用 CCXT/YahooFinance
        这里通过多次调用或使用其他方法获取
        """
        # TradingView Scanner 不支持直接获取大量历史K线
        # 返回空 DataFrame，提示用户使用其他数据源获取历史数据
        logger.warning(
            "TradingView 不适合获取大量历史数据，建议使用 ccxt/yfinance 获取历史K线。"
            "TradingViewProvider 主要用于实时行情和技术指标。"
        )
        return pd.DataFrame(columns=['date', 'open', 'high', 'low', 'close', 'volume'])

    def get_real_time_quote(self, symbol: str) -> Dict:
        """获取实时行情和技术指标"""
        try:
            detail = self.get_symbol_detail(symbol)
            return {
                'symbol': symbol,
                'price': detail.get('price'),
                'open': detail.get('open'),
                'high': detail.get('high'),
                'low': detail.get('low'),
                'volume': detail.get('volume'),
                'rsi': detail.get('rsi'),
                'macd': detail.get('macd'),
                'macd_signal': detail.get('macd_signal'),
                'bb_upper': detail.get('bb_upper'),
                'bb_lower': detail.get('bb_lower'),
                'timestamp': datetime.now().isoformat()
            }
        except Exception as e:
            logger.error(f"TradingView 获取实时行情失败: {e}")
            return {}

    def get_symbol_detail(self, symbol: str) -> Dict:
        """
        获取符号详细信息（包含技术指标）
        返回: {symbol, name, price, open, high, low, volume, rsi, macd, macd_signal, bb_upper, bb_lower}
        """
        payload = {
            "filter": [{"left": "name", "operation": "equal", "right": symbol}],
            "columns": [
                "name", "description", "close", "open", "high", "low", "volume",
                "RSI", "MACD.macd", "MACD.signal", "BB.upper", "BB.lower",
            ],
            "range": [0, 1],
        }
        import httpx
        r = httpx.post(
            f"{self.BASE_URL}/america/scan",
            json=payload,
            headers=self.headers,
            timeout=15
        )
        r.raise_for_status()
        rows = r.json().get("data", [])
        if not rows:
            raise ValueError(f"Symbol not found: {symbol}")
        d = rows[0]["d"]
        return {
            "symbol": d[0],
            "name": d[1],
            "price": d[2],
            "open": d[3],
            "high": d[4],
            "low": d[5],
            "volume": d[6],
            "rsi": d[7],
            "macd": d[8],
            "macd_signal": d[9],
            "bb_upper": d[10],
            "bb_lower": d[11],
        }

    def get_symbols(self) -> List[str]:
        """TradingView 不支持批量获取所有符号列表"""
        logger.warning("TradingView 不支持批量获取符号列表")
        return []

    def get_trending(self, limit: int = 10) -> List[Dict]:
        """获取热门符号（按成交量）"""
        payload = {
            "filter": [{"left": "volume", "operation": "greater", "right": 1_000_000}],
            "sort": {"sortBy": "volume", "sortOrder": "desc"},
            "columns": ["name", "close", "change", "volume"],
            "range": [0, limit],
        }
        import httpx
        r = httpx.post(
            f"{self.BASE_URL}/america/scan",
            json=payload,
            headers=self.headers,
            timeout=15
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        return [
            {
                "symbol": row["d"][0],
                "price": row["d"][1],
                "change_pct": row["d"][2],
                "volume": row["d"][3],
            }
            for row in data
        ]

    def search_symbols(self, query: str) -> List[Dict]:
        """搜索符号"""
        import httpx
        r = httpx.get(
            self.SYMBOL_URL,
            params={"text": query, "type": "", "exchange": "", "lang": "en"},
            headers=self.headers,
            timeout=10
        )
        r.raise_for_status()
        return [
            {
                "symbol": s.get("symbol", ""),
                "name": s.get("description", ""),
                "exchange": s.get("exchange", ""),
                "type": s.get("type", ""),
            }
            for s in r.json()
        ]


class BinanceMarketRankProvider(DataProvider):
    """
    CryptoClaw 集成 - Binance 市场排名数据源

    功能：
    - 热门代币排行
    - 智能资金流入排行
    - 社交热度排行
    - Alpha 项目推荐

    来源：CryptoClaw skills/binance-market-rank
    """

    BASE_URL = "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token"

    def __init__(self, chain_id: int = 56):
        """
        初始化 Binance 市场排名 provider
        chain_id: 链标识 (56=BSC, 8453=Base, 1=Ethereum, CT_501=Solana)
        """
        self.chain_id = chain_id

    def get_historical_data(
        self,
        symbol: str,
        timeframe: str = "1d",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 1000
    ) -> pd.DataFrame:
        """
        获取代币历史数据（通过 Binance Web3 API）
        返回 OHLCV 数据
        """
        import httpx

        # 使用 Binance Web3 的 klines 接口
        url = f"{self.BASE_URL}/candles"
        params = {
            "chainId": str(self.chain_id),
            "symbol": symbol,
            "interval": timeframe,
            "limit": str(limit)
        }

        r = httpx.get(url, params=params, timeout=15)
        if r.status_code != 200:
            logger.warning(f"Binance Web3 API 返回: {r.status_code}")
            return pd.DataFrame()

        data = r.json().get("data", [])
        if not data:
            return pd.DataFrame()

        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['date'] = pd.to_datetime(df['timestamp'], unit='ms')
        df = df.drop(columns=['timestamp'])

        if start_date:
            df = df[df['date'] >= pd.to_datetime(start_date)]
        if end_date:
            df = df[df['date'] <= pd.to_datetime(end_date)]

        return df.tail(limit)

    def get_real_time_quote(self, symbol: str) -> Dict:
        """获取实时行情"""
        import httpx

        url = f"{self.BASE_URL}/detail"
        params = {
            "chainId": str(self.chain_id),
            "symbol": symbol
        }

        r = httpx.get(url, params=params, timeout=15)
        if r.status_code != 200:
            return {}

        d = r.json().get("data", {})
        return {
            'symbol': symbol,
            'price': d.get('price'),
            'volume': d.get('volume'),
            'market_cap': d.get('marketCap'),
            'change_24h': d.get('priceChange24h'),
            'timestamp': datetime.now().isoformat()
        }

    def get_symbols(self) -> List[str]:
        """获取热门代币列表"""
        return self.get_trending_symbols()

    def get_trending_symbols(self, rank_type: str = "10", limit: int = 20) -> List[Dict]:
        """
        获取热门代币排行

        rankType:
          - 10: Trending tokens (热门)
          - 11: Most searched (搜索最多)
          - 20: Binance Alpha picks (Alpha 项目)
          - 40: Tokenized stocks (代币化股票)
        """
        import httpx

        url = f"{self.BASE_URL}/rank/unified"
        payload = {
            "rankType": str(rank_type),
            "pageSize": limit,
            "page": 1
        }

        r = httpx.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            logger.warning(f"Binance market rank 失败: {r.status_code}")
            return []

        data = r.json().get("data", [])
        return [
            {
                "symbol": item.get("tokenSymbol", ""),
                "contract_address": item.get("contractAddress", ""),
                "price": item.get("price"),
                "change_24h": item.get("priceChange"),
                "market_cap": item.get("marketCap"),
                "volume": item.get("volume"),
                "rank_type": rank_type,
            }
            for item in data
        ]

    def get_smart_money_inflow(self, limit: int = 20) -> List[Dict]:
        """获取智能资金流入排行"""
        import httpx

        url = f"{self.BASE_URL}/smart-money/inflow"
        payload = {
            "chainId": str(self.chain_id),
            "pageSize": limit,
            "page": 1
        }

        r = httpx.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            return []

        data = r.json().get("data", [])
        return [
            {
                "symbol": item.get("tokenSymbol", ""),
                "inflow_amount": item.get("inflowAmount"),
                "smart_wallets": item.get("smartWalletCount"),
                "price": item.get("price"),
            }
            for item in data
        ]


class SmartMoneySignalProvider(DataProvider):
    """
    CryptoClaw 集成 - 智能资金交易信号数据源

    功能：
    - 跟踪专业投资者（Smart Money）的链上买卖活动
    - 支持 BSC 和 Solana 链
    - 信号包含方向、触发价格、当前价格、最高涨幅等

    来源：CryptoClaw skills/binance-trading-signal
    """

    BASE_URL = "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/web/signal/smart-money"

    def __init__(self, chain_id: int = 56):
        """
        初始化 Smart Money 信号 provider
        chain_id: 56 = BSC, CT_501 = Solana
        """
        self.chain_id = chain_id

    def get_historical_data(
        self,
        symbol: str,
        timeframe: str = "1d",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 1000
    ) -> pd.DataFrame:
        """
        获取历史信号（返回空，因为信号是离散事件）
        """
        logger.info("SmartMoney 信号是离散事件，建议使用 get_signals() 获取")
        return pd.DataFrame()

    def get_real_time_quote(self, symbol: str) -> Dict:
        """获取当前信号状态"""
        signals = self.get_signals(symbol=symbol, limit=5)
        if signals:
            return signals[0]
        return {}

    def get_symbols(self) -> List[str]:
        """获取有信号的代币列表"""
        signals = self.get_signals(limit=50)
        return list(set(s.get("symbol", "") for s in signals if s.get("symbol")))

    def get_signals(
        self,
        symbol: Optional[str] = None,
        limit: int = 20,
        direction: Optional[str] = None  # "buy" or "sell"
    ) -> List[Dict]:
        """
        获取智能资金信号

        参数:
            symbol: 过滤特定符号
            limit: 返回数量
            direction: "buy" 或 "sell" 过滤

        返回:
            [
                {
                    "signalId": "...",
                    "symbol": "BTC",
                    "contract_address": "...",
                    "direction": "buy",
                    "smart_money_count": 12,
                    "trigger_price": 45000.0,
                    "current_price": 46000.0,
                    "max_gain_pct": 8.5,
                    "exit_rate": 0.3,
                    "status": "active",  # active/timeout/completed
                    "timeframe": "24h",
                    "trigger_time": "..."
                },
                ...
            ]
        """
        import httpx

        payload = {
            "chainId": str(self.chain_id),
            "page": 1,
            "pageSize": min(limit, 100)
        }
        if symbol:
            # 注意：API 可能不支持直接按 symbol 过滤，需要在客户端过滤
            pass

        r = httpx.post(self.BASE_URL, json=payload, timeout=15)
        if r.status_code != 200:
            logger.warning(f"SmartMoney 信号获取失败: {r.status_code}")
            return []

        data = r.json().get("data", [])
        signals = []
        for item in data:
            sig = {
                "signalId": item.get("signalId"),
                "symbol": item.get("tokenSymbol", ""),
                "contract_address": item.get("contractAddress", ""),
                "direction": item.get("direction", "").lower(),
                "smart_money_count": item.get("smartMoneyCount", 0),
                "trigger_price": item.get("triggerPrice"),
                "current_price": item.get("currentPrice"),
                "highest_price": item.get("highestPrice"),
                "max_gain_pct": item.get("maxGainPct"),
                "exit_rate": item.get("exitRate"),
                "status": item.get("status", "").lower(),
                "timeframe": item.get("timeframe"),
                "trigger_time": item.get("triggerTime"),
                "tags": item.get("tags", []),
                "launch_platform": item.get("launchPlatform"),
            }
            if symbol and sig["symbol"].upper() != symbol.upper():
                continue
            if direction and sig["direction"] != direction.lower():
                continue
            signals.append(sig)

        return signals[:limit]
