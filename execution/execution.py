"""
OpenClaw 手脚模块 - 交易执行层
统一接口: execute_order(symbol, side, quantity, price=None, order_type="market")
负责实际执行买卖操作的执行器
"""
import os
import time
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from enum import Enum

logger = logging.getLogger(__name__)


def _truthy_env(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _should_bypass_proxy(exchange_id: str = "", testnet: bool = False) -> bool:
    mode = str(os.getenv("OPENCLAW_BINANCE_TESTNET_PROXY_MODE", "direct")).strip().lower()
    if mode in {"proxy", "force-proxy"}:
        return False
    if mode in {"direct", "off", "bypass"}:
        return exchange_id.lower() == "binance" and testnet
    if _truthy_env(os.getenv("OPENCLAW_BINANCE_TESTNET_DIRECT")):
        return exchange_id.lower() == "binance" and testnet
    # Default: Binance testnet should bypass proxy (testnet is often blocked by proxies)
    if exchange_id.lower() == "binance" and testnet:
        return True
    return False


def _resolve_http_proxies(exchange_id: str = "", testnet: bool = False) -> Dict[str, str]:
    if _should_bypass_proxy(exchange_id=exchange_id, testnet=testnet):
        return {}

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
        return "swap"  # CCXT uses "swap" for perpetual futures, not "future"
    return "spot"


def _normalize_ccxt_symbol(symbol: str, market_type: str = "spot", exchange_id: str = "") -> str:
    text = str(symbol or "").strip()
    if not text:
        return text
    if _normalize_ccxt_market_type(market_type) != "swap":
        return text
    if exchange_id.lower() != "binance":
        return text
    if ":" in text or "/" not in text:
        return text

    base, quote = text.split("/", 1)
    if quote.upper() == "USDT":
        return f"{base}/{quote}:USDT"
    return text


def _resolve_exchange_secret(
    exchange_id: str,
    field: str,
    testnet: bool = False,
    market_type: str = "spot",
) -> Optional[str]:
    exchange_key = exchange_id.upper()
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


def _apply_binance_futures_testnet_urls(exchange: Any) -> None:
    """Override Binance exchange URLs to use the futures testnet.
    
    CCXT 4.5.46+ deprecated testnet=True for Binance futures (throws NotSupported).
    This function works around it by:
    1. NOT setting testnet/sandbox flags (keeps them False)
    2. Manually overriding all API URLs to testnet endpoints
    3. Adding sapi URL placeholders so sign() doesn't throw NotSupported
    4. Monkey-patching fetch_markets to skip sapi calls (404 on testnet)
    5. Monkey-patching load_markets with a fallback that loads markets manually
    """
    try:
        urls = getattr(exchange, "urls", {})
        api_urls = urls.get("api", {}) if isinstance(urls, dict) else {}
        if not isinstance(api_urls, dict):
            return

        spot_testnet = "https://testnet.binance.vision"
        futures_testnet = "https://testnet.binancefuture.com"

        # ── CRITICAL: Force testnet/sandbox flags to False ──
        # CCXT sign() checks isSandboxModeEnabled and blocks futures testnet.
        # By keeping these False, CCXT won't intercept our URL-overridden requests.
        exchange.testnet = False
        if hasattr(exchange, 'sandbox'):
            exchange.sandbox = False
        # Also ensure the option flag is not set
        if hasattr(exchange, 'options') and isinstance(exchange.options, dict):
            exchange.options.pop('sandbox', None)
            exchange.options.pop('testnet', None)

        # ── Override all API URLs to testnet endpoints ──
        # Futures (fapi) URLs → testnet.binancefuture.com
        # NOTE: Do NOT add trailing slash! CCXT sign() does url += '/' + path
        # which would create double-slash (e.g. /fapi/v1//time) → CloudFront 403
        for key in (
            "fapiPublic", "fapiPublicV2", "fapiPublicV3",
            "fapiPrivate", "fapiPrivateV2", "fapiPrivateV3",
            "fapiData",
        ):
            version = key.replace("fapiPublic", "").replace("fapiPrivate", "").replace("fapiData", "").lower()
            if "Public" in key:
                api_urls[key] = f"{futures_testnet}/fapi/{version or 'v1'}"
            elif "Private" in key:
                api_urls[key] = f"{futures_testnet}/fapi/{version or 'v1'}"
            elif "Data" in key:
                api_urls[key] = f"{futures_testnet}/futures/data"

        # Delivery (dapi) URLs → testnet.binancefuture.com
        api_urls["dapiPublic"] = f"{futures_testnet}/dapi/v1"
        api_urls["dapiPrivate"] = f"{futures_testnet}/dapi/v1"
        api_urls["dapiPrivateV2"] = f"{futures_testnet}/dapi/v2"

        # Spot API URLs → testnet.binance.vision
        api_urls["public"] = f"{spot_testnet}/api/v3"
        api_urls["private"] = f"{spot_testnet}/api/v3"
        api_urls["v1"] = f"{spot_testnet}/api/v1"

        # ── CRITICAL: Add sapi URL placeholders ──
        # CCXT's sign() throws NotSupported if sapi URLs are missing.
        # These endpoints 404 on testnet, but we need the URL to exist so
        # sign() generates the URL (then fetch_markets patch skips the call).
        sapi_versions = {"sapi": "v1", "sapiV2": "v2", "sapiV3": "v3", "sapiV4": "v4"}
        for key, ver in sapi_versions.items():
            api_urls[key] = f"{spot_testnet}/sapi/{ver}"

        exchange.urls["api"] = api_urls

        # ── Disable sapi-dependent features ──
        if hasattr(exchange, "has") and isinstance(exchange.has, dict):
            exchange.has["fetchCurrencies"] = False
            exchange.has["fetchMarginMarkets"] = False

        # ── Monkey-patch fetch_markets to skip sapi calls ──
        # CCXT's fetch_markets() calls sapiGetMarginAllPairs which 404s on testnet.
        # We patch it to catch the 404 and return an empty list for that call.
        _original_fetch_markets = exchange.fetch_markets
        _fetch_markets_patched = False

        def _testnet_fetch_markets(params=None):
            nonlocal _fetch_markets_patched
            if _fetch_markets_patched and not (params and params.get('reload')):
                return exchange.markets if exchange.markets else []
            try:
                result = _original_fetch_markets(params=params)
                if result and len(result) > 0:
                    _fetch_markets_patched = True
                    return result
            except Exception:
                pass
            # Fallback: manually load markets from testnet exchangeInfo
            return _manual_load_testnet_markets(exchange, spot_testnet, futures_testnet)

        exchange.fetch_markets = _testnet_fetch_markets

        # ── Monkey-patch load_markets with fallback ──
        _original_load_markets = exchange.load_markets
        _patched = False

        def _testnet_load_markets(reload=False):
            nonlocal _patched
            if _patched and not reload:
                return exchange.markets
            try:
                result = _original_load_markets(reload=reload)
                if result and len(result) > 0:
                    _patched = True
                    return result
            except Exception:
                pass
            # Fallback: manually load markets from testnet exchangeInfo
            result = _manual_load_testnet_markets(exchange, spot_testnet, futures_testnet)
            if result:
                _patched = True
                return result
            # Last resort
            exchange.markets = exchange.markets or {}
            exchange.markets_by_id = exchange.markets_by_id or {}
            _patched = True
            return exchange.markets

        exchange.load_markets = _testnet_load_markets
    except Exception:
        return


def _manual_load_testnet_markets(exchange: Any, spot_testnet: str, futures_testnet: str) -> dict:
    """Manually load markets from testnet exchangeInfo endpoints.
    
    This is a fallback for when CCXT's built-in fetch_markets/load_markets
    fails on testnet (due to sapi 404 or sandbox deprecation).
    """
    try:
        import json, urllib.request
        # Load futures markets from fapi exchangeInfo
        url = f"{futures_testnet}/fapi/v1/exchangeInfo"
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        # Also load spot markets from spot testnet
        spot_url = f"{spot_testnet}/api/v3/exchangeInfo"
        req2 = urllib.request.Request(spot_url)
        req2.add_header("User-Agent", "Mozilla/5.0")
        spot_data = {}
        try:
            with urllib.request.urlopen(req2, timeout=15) as resp2:
                spot_data = json.loads(resp2.read())
        except Exception:
            pass
        # Parse and inject markets
        exchange.markets = {}
        exchange.markets_by_id = {}
        # Add spot markets
        for sym in spot_data.get("symbols", []):
            if sym.get("status") != "TRADING":
                continue
            base = sym.get("baseAsset", "")
            quote = sym.get("quoteAsset", "")
            symbol = f"{base}/{quote}"
            market_info = {
                "id": sym.get("symbol"),
                "symbol": symbol,
                "base": base,
                "quote": quote,
                "active": True,
                "type": "spot",
                "spot": True,
                "swap": False,
                "future": False,
                "option": False,
                "indexId": None,
                "linear": None,
                "inverse": None,
                "contract": False,
                "contractSize": None,
                "expiry": None,
                "strike": None,
                "optionType": None,
                "settle": None,
                "precision": {"amount": sym.get("baseAssetPrecision", 8), "price": sym.get("quoteAssetPrecision", 8)},
                "limits": {"amount": {"min": None, "max": None}, "price": {"min": None, "max": None}, "cost": {"min": None, "max": None}},
                "info": sym,
            }
            exchange.markets[symbol] = market_info
            exchange.markets_by_id[sym["symbol"]] = [market_info]
            for curr in [base, quote]:
                if curr and curr not in exchange.currencies:
                    exchange.currencies[curr] = {"id": curr, "code": curr, "precision": 8}
        # Add futures/swap markets
        for sym in data.get("symbols", []):
            if sym.get("status") != "TRADING":
                continue
            base = sym.get("baseAsset", "")
            quote = sym.get("quoteAsset", "")
            settle = sym.get("marginAsset", quote)
            symbol = f"{base}/{quote}:{settle}"
            market_info = {
                "id": sym.get("symbol"),
                "symbol": symbol,
                "base": base,
                "quote": quote,
                "settle": settle,
                "active": True,
                "type": "swap",
                "spot": False,
                "swap": True,
                "future": False,
                "option": False,
                "indexId": None,
                "linear": True,
                "inverse": False,
                "contract": True,
                "contractSize": float(sym.get("contractSize", 1)),
                "expiry": None,
                "strike": None,
                "optionType": None,
                "precision": {"amount": sym.get("quantityPrecision", 3), "price": sym.get("pricePrecision", 8)},
                "limits": {"amount": {"min": None, "max": None}, "price": {"min": None, "max": None}, "cost": {"min": None, "max": None}},
                "info": sym,
            }
            exchange.markets[symbol] = market_info
            exchange.markets_by_id[sym["symbol"]] = [market_info]
            for curr in [base, quote, settle]:
                if curr and curr not in exchange.currencies:
                    exchange.currencies[curr] = {"id": curr, "code": curr, "precision": 8}
        if not hasattr(exchange, 'currencies_by_id') or not exchange.currencies_by_id:
            exchange.currencies_by_id = {v.get("id", k): v for k, v in exchange.currencies.items()}
        return exchange.markets
    except Exception:
        return {}
class OrderSide(Enum):
    """订单方向"""
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    """订单类型"""
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(Enum):
    """订单状态"""
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class Order:
    """订单数据结构"""
    id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: float
    price: Optional[float] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: float = 0.0
    avg_price: float = 0.0
    commission: float = 0.0
    timestamp: datetime = None
    error_message: str = ""

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()

    def to_dict(self) -> Dict:
        """转换为字典"""
        d = asdict(self)
        d['side'] = self.side.value
        d['order_type'] = self.order_type.value
        d['status'] = self.status.value
        d['timestamp'] = self.timestamp.isoformat()
        return d


@dataclass
class Position:
    """持仓数据结构"""
    symbol: str
    quantity: float
    avg_price: float
    current_price: float
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()
        self.unrealized_pnl = (self.current_price - self.avg_price) * self.quantity

    def to_dict(self) -> Dict:
        """转换为字典"""
        return asdict(self)


class ExecutionProvider(ABC):
    """Abstract execution provider."""

    @abstractmethod
    def connect(self) -> bool:
        pass

    @abstractmethod
    def disconnect(self) -> bool:
        pass

    @abstractmethod
    def place_order(self, order: Order) -> Order:
        pass

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        pass

    @abstractmethod
    def get_order(self, order_id: str) -> Optional[Order]:
        pass

    @abstractmethod
    def get_positions(self) -> List[Position]:
        pass

    @abstractmethod
    def get_balance(self) -> Dict[str, float]:
        pass


class EasyTraderProvider(ExecutionProvider):
    """China A-share execution provider backed by easytrader."""

    def __init__(
        self,
        client: str = "",
        username: Optional[str] = None,
        password: Optional[str] = None,
        host: str = "localhost",
        port: int = 8000,
        prepare_path: Optional[str] = None,
    ):
        self.client_type = client
        self.username = username
        self.password = password
        self.host = host
        self.port = port
        self.prepare_path = prepare_path
        self.trader = None
        self.connected = False

    def connect(self) -> bool:
        try:
            import easytrader
            if not str(self.client_type or "").strip():
                raise RuntimeError("easytrader client is not configured")

            self.trader = easytrader.use(self.client_type)
            if self.prepare_path:
                self.trader.prepare(self.prepare_path)
            elif self.username and self.password:
                self.trader.prepare(user=self.username, password=self.password)
            elif hasattr(self.trader, "connect"):
                self.trader.connect(host=self.host, port=self.port)

            self.connected = True
            logger.info(f"Connected to easytrader client {self.client_type}")
            return True
        except ImportError:
            logger.error("Please install easytrader: pip install easytrader")
            raise
        except Exception as e:
            logger.error(f"Failed to connect easytrader client: {e}")
            self.connected = False
            return False

    def disconnect(self) -> bool:
        self.connected = False
        return True

    def place_order(self, order: Order) -> Order:
        if not self.connected or not self.trader:
            raise RuntimeError("Not connected to easytrader client")

        try:
            symbol = self._format_symbol(order.symbol)
            trade_price = order.price
            if order.order_type != OrderType.LIMIT or trade_price is None:
                trade_price = self._get_current_price(symbol)

            side_method = self.trader.buy if order.side == OrderSide.BUY else self.trader.sell
            result = side_method(code=symbol, price=trade_price, amount=order.quantity)

            order.id = str(result.get("entrust_no", result.get("order_id", "")))
            order.status = OrderStatus.SUBMITTED
            order.price = trade_price
            order.timestamp = datetime.now()
            logger.info(
                f"EasyTrader order submitted: {order.symbol} {order.side.value} {order.quantity}"
            )
            return order
        except Exception as e:
            logger.error(f"EasyTrader order failed: {e}")
            order.status = OrderStatus.REJECTED
            order.error_message = str(e)
            return order

    def cancel_order(self, order_id: str) -> bool:
        try:
            result = self.trader.cancel_entrust(order_id)
            return result is not None
        except Exception as e:
            logger.error(f"Cancel order failed: {e}")
            return False

    def get_order(self, order_id: str) -> Optional[Order]:
        try:
            result = self.trader.get_entrust(order_id)
            if result:
                return self._parse_order(result)
            return None
        except Exception as e:
            logger.error(f"Get order failed: {e}")
            return None

    def get_positions(self) -> List[Position]:
        try:
            result = self.trader.position
            positions = []
            for item in result:
                positions.append(
                    Position(
                        symbol=item.get("stock_code", ""),
                        quantity=float(item.get("current_amount", 0) or 0),
                        avg_price=float(item.get("avg_cost", 0) or 0),
                        current_price=float(item.get("current_price", 0) or 0),
                    )
                )
            return positions
        except Exception as e:
            logger.error(f"Get positions failed: {e}")
            return []

    def get_balance(self) -> Dict[str, float]:
        try:
            result = self.trader.balance
            if isinstance(result, list) and result:
                result = result[0]
            return {
                "total_asset": float(result.get("total_asset", 0) or 0),
                "cash": float(result.get("enable_balance", 0) or 0),
                "market_value": float(result.get("market_value", 0) or 0),
                "pnl": float(result.get("profit", 0) or 0),
            }
        except Exception as e:
            logger.error(f"Get balance failed: {e}")
            return {"cash": 0, "total_asset": 0, "market_value": 0, "pnl": 0}

    def _format_symbol(self, symbol: str) -> str:
        if "." in symbol:
            return symbol.split(".")[0]
        lowered = symbol.lower()
        if lowered.startswith(("sh", "sz", "bj")) and len(symbol) >= 8:
            return symbol[2:8]
        return symbol

    def _get_current_price(self, symbol: str) -> float:
        try:
            quote = self.trader.quote(symbol)
            return float(quote.get("price", 0) or 0)
        except Exception:
            return 0.0

    def _parse_order(self, raw: Dict) -> Order:
        return Order(
            id=str(raw.get("entrust_no", "")),
            symbol=raw.get("stock_code", ""),
            side=OrderSide.BUY if raw.get("bs_flag") == 1 else OrderSide.SELL,
            order_type=OrderType.LIMIT if raw.get("price", 0) > 0 else OrderType.MARKET,
            quantity=float(raw.get("amount", 0) or 0),
            price=float(raw.get("price", 0) or 0),
            status=OrderStatus(raw.get("status", "pending")),
            filled_quantity=float(raw.get("deal_amount", 0) or 0),
            avg_price=float(raw.get("avg_price", 0) or 0),
        )


class GmTradeProvider(ExecutionProvider):
    """China A-share execution provider backed by MyQuant gmtrade."""

    def __init__(
        self,
        token: Optional[str] = None,
        account_id: Optional[str] = None,
        account_alias: Optional[str] = None,
        endpoint: str = "api.myquant.cn:9000",
        lot_size: int = 100,
        allow_closed_session: bool = False,
    ):
        self.token = str(token or os.getenv("GMTRADE_TOKEN") or os.getenv("GM_TOKEN") or "").strip()
        self.account_id = str(account_id or os.getenv("GMTRADE_ACCOUNT_ID") or "").strip()
        self.account_alias = str(account_alias or os.getenv("GMTRADE_ACCOUNT_ALIAS") or "").strip()
        self.endpoint = str(endpoint or os.getenv("GMTRADE_ENDPOINT") or "api.myquant.cn:9000").strip()
        self.lot_size = max(int(lot_size or 100), 1)
        self.allow_closed_session = bool(allow_closed_session)
        self.gm = None
        self.account = None
        self.connected = False

    def connect(self) -> bool:
        # Try gmtrade.api first, fall back to gm.api (both provide trading functions)
        gm = None
        api_source = None
        try:
            import gmtrade.api as gm  # type: ignore
            api_source = "gmtrade"
        except ImportError:
            try:
                import gm.api as gm  # type: ignore
                api_source = "gm"
            except ImportError:
                logger.error("Please install gmtrade or gm: pip install gmtrade / pip install gm")
                raise

        try:
            if not self.token:
                logger.error("gm token is missing")
                return False
            if not (self.account_id or self.account_alias):
                logger.error("gm account_id/account_alias is missing")
                return False

            gm.set_token(self.token)
            if self.endpoint and hasattr(gm, "set_endpoint"):
                try:
                    gm.set_endpoint(self.endpoint)
                except Exception:
                    pass  # gm.api may not support set_endpoint

            # gmtrade uses gm.account() + gm.login(), gm.api uses set_account_id()
            if api_source == "gmtrade":
                account_kwargs: Dict[str, str] = {}
                if self.account_id:
                    account_kwargs["account_id"] = self.account_id
                if self.account_alias:
                    account_kwargs["account_alias"] = self.account_alias
                self.account = gm.account(**account_kwargs)
                gm.login(self.account)
            else:
                # gm.api: use set_account_id for live trading
                if hasattr(gm, "set_account_id"):
                    gm.set_account_id(self.account_id)
                self.account = self.account_id  # Store as string for gm.api

            self.gm = gm
            self.connected = True
            logger.info("Connected to %s account %s (api=%s)", api_source, self.account_id or self.account_alias, api_source)
            return True
        except Exception as e:
            logger.error(f"Failed to connect gm account ({api_source}): {e}")
            self.connected = False
            return False

    def disconnect(self) -> bool:
        try:
            if self.connected and self.gm is not None:
                stop = getattr(self.gm, "stop", None)
                if callable(stop):
                    try:
                        stop()
                    except Exception:
                        pass
        finally:
            self.connected = False
        return True

    def place_order(self, order: Order) -> Order:
        if not self.connected or self.gm is None:
            raise RuntimeError("Not connected to gmtrade")

        try:
            symbol = self._normalize_symbol(order.symbol)
            volume = self._normalize_volume(order)
            if volume <= 0:
                raise ValueError(
                    f"Resolved order volume is zero for {order.symbol}; "
                    f"buy orders must usually be in lots of {self.lot_size}"
                )

            payload: Dict[str, Any] = {
                "symbol": symbol,
                "volume": volume,
                "side": self._gm_side(order.side),
                "order_type": self._gm_order_type(order.order_type),
                "position_effect": self._gm_position_effect(order.side),
            }
            if order.price is not None:
                payload["price"] = float(order.price)
            elif order.order_type == OrderType.LIMIT:
                raise ValueError("Limit orders require a price for gmtrade")

            raw = self._call_with_optional_account("order_volume", **payload)
            return self._parse_gm_order(
                raw,
                fallback_symbol=symbol,
                fallback_side=order.side,
                fallback_order_type=order.order_type,
                fallback_quantity=volume,
                fallback_price=order.price,
            )
        except Exception as e:
            logger.error(f"gmtrade order failed: {e}")
            order.status = OrderStatus.REJECTED
            order.error_message = str(e)
            return order

    def cancel_order(self, order_id: str) -> bool:
        if not self.connected or self.gm is None:
            return False
        try:
            # Try order_cancel first (gmtrade), then order_cancel_all (gm.api)
            try:
                self._call_with_optional_account(
                    "order_cancel",
                    wait_cancel_orders=[{"cl_ord_id": str(order_id)}],
                )
            except (TypeError, AttributeError):
                # gm.api doesn't have order_cancel, try order_cancel_all as fallback
                self._call_with_optional_account("order_cancel_all")
            return True
        except Exception as e:
            logger.error(f"gmtrade cancel order failed: {e}")
            return False

    def get_order(self, order_id: str) -> Optional[Order]:
        if not self.connected or self.gm is None:
            return None
        try:
            for method_name in ("get_unfinished_orders", "get_orders"):
                getter = getattr(self.gm, method_name, None)
                if not callable(getter):
                    continue
                rows = self._call_with_optional_account(method_name) or []
                for row in rows:
                    candidate = self._parse_gm_order(row)
                    if candidate.id == str(order_id):
                        return candidate
        except Exception as e:
            logger.error(f"gmtrade get order failed: {e}")
        return None

    def get_positions(self) -> List[Position]:
        if not self.connected or self.gm is None:
            return []
        try:
            # gmtrade uses get_positions (plural), gm.api uses get_position (singular)
            rows = []
            for method_name in ("get_positions", "get_position"):
                if not hasattr(self.gm, method_name):
                    continue
                try:
                    result = self._call_with_optional_account(method_name)
                    if result:
                        rows = result if isinstance(result, list) else [result]
                        break
                except Exception:
                    continue
            positions: List[Position] = []
            for item in rows:
                volume = float(self._gm_attr(item, "volume", default=0) or 0)
                if volume <= 0:
                    continue
                positions.append(
                    Position(
                        symbol=str(self._gm_attr(item, "symbol", default="") or ""),
                        quantity=volume,
                        avg_price=float(self._gm_attr(item, "vwap", default=0) or 0),
                        current_price=float(self._gm_attr(item, "price", default=0) or 0),
                        realized_pnl=float(self._gm_attr(item, "pnl", default=0) or 0),
                    )
                )
            return positions
        except Exception as e:
            logger.error(f"gmtrade get positions failed: {e}")
            return []

    def get_balance(self) -> Dict[str, float]:
        if not self.connected or self.gm is None:
            return {"cash": 0, "total_asset": 0, "market_value": 0, "pnl": 0}
        try:
            cash_rows = self._call_with_optional_account("get_cash")
            # gmtrade returns list of dicts, gm.api returns dict directly
            if isinstance(cash_rows, list) and cash_rows:
                cash = cash_rows[0]
            elif isinstance(cash_rows, dict):
                cash = cash_rows
            elif hasattr(cash_rows, '__dict__'):
                # gm.api may return an object with attributes
                cash = {k: getattr(cash_rows, k, 0) for k in ('nav', 'available', 'pnl', 'fpnl', 'frozen', 'order_frozen', 'market_value', 'balance')}
            else:
                cash = cash_rows or {}
            total_asset = float(self._gm_attr(cash, "nav", default=0) or 0)
            available = float(self._gm_attr(cash, "available", default=0) or 0)
            pnl = float(self._gm_attr(cash, "pnl", default=0) or 0)
            order_frozen = float(self._gm_attr(cash, "order_frozen", default=0) or 0)
            market_value = max(total_asset - available - order_frozen, 0.0)
            return {
                "total_asset": total_asset,
                "cash": available,
                "market_value": market_value,
                "pnl": pnl,
                "frozen": float(self._gm_attr(cash, "frozen", default=0) or 0),
                "order_frozen": order_frozen,
            }
        except Exception as e:
            logger.error(f"gmtrade get balance failed: {e}")
            return {"cash": 0, "total_asset": 0, "market_value": 0, "pnl": 0}

    def _call_with_optional_account(self, method_name: str, **kwargs: Any) -> Any:
        method = getattr(self.gm, method_name, None)
        if method is None:
            raise AttributeError(f"gm module has no attribute '{method_name}'")
        # Try different calling patterns:
        # 1. gmtrade: account=<account_object>
        # 2. gm.api: account_id=<string>
        # 3. No account param (set_account_id already called, or function doesn't need it)
        call_attempts = []
        if not isinstance(self.account, str):
            # gmtrade: account is an object
            call_attempts.append({"account": self.account, **kwargs})
        if isinstance(self.account, str) and self.account:
            # gm.api: account_id is a string
            call_attempts.append({"account_id": self.account, **kwargs})
        # Always try without account param as fallback
        call_attempts.append(kwargs)
        
        last_error = None
        for attempt_kwargs in call_attempts:
            try:
                return method(**attempt_kwargs)
            except (TypeError, AttributeError) as e:
                last_error = e
                continue
        
        # Last resort: call with no args
        try:
            return method()
        except TypeError:
            raise last_error or TypeError(f"Cannot call {method_name} with any known signature")

    def _gm_attr(self, raw: Any, *names: str, default: Any = None) -> Any:
        for name in names:
            if isinstance(raw, dict) and raw.get(name) is not None:
                return raw.get(name)
            if hasattr(raw, name):
                value = getattr(raw, name)
                if value is not None:
                    return value
        return default

    def _normalize_symbol(self, symbol: str) -> str:
        text = str(symbol or "").strip().upper()
        if not text:
            return text
        if text.startswith(("SHSE.", "SZSE.", "BJSE.")):
            return text
        if "." in text:
            code, suffix = text.split(".", 1)
            suffix = suffix.upper()
            if suffix in {"SH", "SS"}:
                return f"SHSE.{code}"
            if suffix == "SZ":
                return f"SZSE.{code}"
            if suffix == "BJ":
                return f"BJSE.{code}"
        lowered = text.lower()
        if lowered.startswith("sh") and len(text) >= 8:
            return f"SHSE.{text[2:8]}"
        if lowered.startswith("sz") and len(text) >= 8:
            return f"SZSE.{text[2:8]}"
        if lowered.startswith("bj") and len(text) >= 8:
            return f"BJSE.{text[2:8]}"
        code = text[-6:] if text[-6:].isdigit() else text
        if code[:1] in {"5", "6", "9"}:
            return f"SHSE.{code}"
        if code[:1] in {"4", "8"}:
            return f"BJSE.{code}"
        return f"SZSE.{code}"

    def _normalize_volume(self, order: Order) -> int:
        quantity = int(float(order.quantity) or 0)
        if order.side == OrderSide.BUY:
            return (quantity // self.lot_size) * self.lot_size
        return quantity

    def _gm_side(self, side: OrderSide) -> int:
        return int(getattr(self.gm, "OrderSide_Buy" if side == OrderSide.BUY else "OrderSide_Sell"))

    def _gm_order_type(self, order_type: OrderType) -> int:
        attr_name = "OrderType_Limit" if order_type == OrderType.LIMIT else "OrderType_Market"
        return int(getattr(self.gm, attr_name))

    def _gm_position_effect(self, side: OrderSide) -> int:
        attr_name = "PositionEffect_Open" if side == OrderSide.BUY else "PositionEffect_Close"
        return int(getattr(self.gm, attr_name))

    def _map_gm_order_status(self, value: Any) -> OrderStatus:
        status = int(value or 0)
        mapping = {
            int(getattr(self.gm, "OrderStatus_New", 1)): OrderStatus.SUBMITTED,
            int(getattr(self.gm, "OrderStatus_PartiallyFilled", 2)): OrderStatus.PARTIAL,
            int(getattr(self.gm, "OrderStatus_Filled", 3)): OrderStatus.FILLED,
            int(getattr(self.gm, "OrderStatus_Canceled", 5)): OrderStatus.CANCELLED,
            int(getattr(self.gm, "OrderStatus_PendingCancel", 6)): OrderStatus.SUBMITTED,
            int(getattr(self.gm, "OrderStatus_Rejected", 8)): OrderStatus.REJECTED,
            int(getattr(self.gm, "OrderStatus_Suspended", 9)): OrderStatus.PENDING,
            int(getattr(self.gm, "OrderStatus_PendingNew", 10)): OrderStatus.PENDING,
            int(getattr(self.gm, "OrderStatus_Expired", 12)): OrderStatus.CANCELLED,
        }
        return mapping.get(status, OrderStatus.PENDING)

    def _parse_gm_order(
        self,
        raw: Any,
        *,
        fallback_symbol: str = "",
        fallback_side: Optional[OrderSide] = None,
        fallback_order_type: Optional[OrderType] = None,
        fallback_quantity: float = 0.0,
        fallback_price: Optional[float] = None,
    ) -> Order:
        side_value = int(self._gm_attr(raw, "side", default=0) or 0)
        buy_side = int(getattr(self.gm, "OrderSide_Buy", 1)) if self.gm is not None else 1
        order_type_value = int(self._gm_attr(raw, "order_type", default=0) or 0)
        limit_type = int(getattr(self.gm, "OrderType_Limit", 1)) if self.gm is not None else 1
        side = fallback_side or (OrderSide.BUY if side_value == buy_side else OrderSide.SELL)
        order_type = fallback_order_type or (OrderType.LIMIT if order_type_value == limit_type else OrderType.MARKET)
        quantity = float(self._gm_attr(raw, "volume", "target_volume", default=fallback_quantity) or fallback_quantity or 0)
        price_value = self._gm_attr(raw, "price", default=fallback_price)
        status_value = self._gm_attr(raw, "status", default=0)
        return Order(
            id=str(self._gm_attr(raw, "cl_ord_id", "order_id", default="") or ""),
            symbol=str(self._gm_attr(raw, "symbol", default=fallback_symbol) or fallback_symbol),
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=float(price_value or 0) if price_value is not None else None,
            status=self._map_gm_order_status(status_value),
            filled_quantity=float(self._gm_attr(raw, "filled_volume", default=0) or 0),
            avg_price=float(self._gm_attr(raw, "filled_vwap", default=0) or 0),
            commission=float(self._gm_attr(raw, "commission", default=0) or 0),
            timestamp=datetime.now(),
            error_message=str(self._gm_attr(raw, "ord_rej_reason_detail", default="") or ""),
        )


class CCXTExecutionProvider(ExecutionProvider):
    """
    加密货币交易执行器 - 基于 CCXT
    直接调用交易所官方API，稳定可靠
    """

    def __init__(
        self,
        exchange_id: str = "binance",
        api_key: Optional[str] = None,
        secret: Optional[str] = None,
        testnet: bool = False,
        market_type: str = "spot",
        default_leverage: Optional[int] = None,
        max_leverage: Optional[int] = None,
        quote_asset: str = "USDT",
    ):
        """
        初始化交易所连接
        exchange_id: 交易所ID (binance, okex, huobi, coinbase...)
        api_key/secret: 从交易所获取
        testnet: 是否使用测试网
        """
        import ccxt

        self.exchange_id = exchange_id
        self.testnet = testnet
        self.market_type = _normalize_ccxt_market_type(market_type)
        self.quote_asset = str(quote_asset or "USDT").strip().upper() or "USDT"
        try:
            self.default_leverage = max(int(default_leverage or 1), 1)
        except Exception:
            self.default_leverage = 1
        try:
            self.max_leverage = max(int(max_leverage or self.default_leverage), 1)
        except Exception:
            self.max_leverage = self.default_leverage

        self.api_key = api_key or _resolve_exchange_secret(
            exchange_id,
            "API_KEY",
            testnet=testnet,
            market_type=self.market_type,
        )
        self.secret = secret or _resolve_exchange_secret(
            exchange_id,
            "SECRET",
            testnet=testnet,
            market_type=self.market_type,
        )

        exchange_class = getattr(ccxt, exchange_id)
        config = {
            'apiKey': self.api_key,
            'secret': self.secret,
            'enableRateLimit': True,
            'timeout': 30000,
            'options': {
                'defaultType': self.market_type,
                'adjustForTimeDifference': True,
                'recvWindow': 10000,
            },
        }
        if self.market_type == "swap" and self.exchange_id.lower() == "binance":
            config["options"]["defaultSubType"] = "linear"

        # For Binance testnet: clear proxy env vars BEFORE creating exchange
        # CCXT's requests.Session reads env vars at creation time
        _cleared_proxy_vars = {}
        if self.testnet and self.exchange_id.lower() == "binance":
            for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                        "all_proxy", "ALL_PROXY"):
                val = os.environ.pop(var, None)
                if val is not None:
                    _cleared_proxy_vars[var] = val
            # Also add NO_PROXY to prevent any proxy for testnet domains
            existing_no_proxy = os.environ.get("no_proxy", os.environ.get("NO_PROXY", ""))
            testnet_domains = "testnet.binancefuture.com,testnet.binance.vision,localhost,127.0.0.1"
            if testnet_domains not in existing_no_proxy:
                new_no_proxy = f"{testnet_domains},{existing_no_proxy}" if existing_no_proxy else testnet_domains
                os.environ["NO_PROXY"] = new_no_proxy
                os.environ["no_proxy"] = new_no_proxy

        self.exchange = exchange_class(config)
        # Binance futures: skip set_sandbox_mode (deprecated in CCXT), use URL override instead
        if self.testnet and self.market_type == "swap" and self.exchange_id.lower() == "binance":
            _apply_binance_futures_testnet_urls(self.exchange)
        elif testnet and hasattr(self.exchange, "set_sandbox_mode"):
            try:
                self.exchange.set_sandbox_mode(True)
            except Exception:
                pass  # sandbox mode not supported for this market type

        session = getattr(self.exchange, "session", None)
        proxies = _resolve_http_proxies(exchange_id=self.exchange_id, testnet=self.testnet)
        if session is not None:
            if proxies:
                session.trust_env = True
                session.proxies.update(proxies)
            else:
                session.trust_env = False
                # For Binance testnet, also clear proxy env vars to prevent
                # requests library from using them despite trust_env=False
                if self.testnet and self.exchange_id.lower() == "binance":
                    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                                "all_proxy", "ALL_PROXY"):
                        os.environ.pop(var, None)
                try:
                    session.proxies.clear()
                except Exception:
                    pass

        self.connected = False

    def _testnet_direct_balance(self) -> Dict[str, float]:
        """Fallback: query Binance futures testnet balance via direct REST API.
        Used when CCXT's fetch_balance fails due to V3/sapi/leverageBracket issues."""
        import hashlib, hmac, time, urllib.request, json as _json
        try:
            futures_testnet = "https://testnet.binancefuture.com"
            ts = int(time.time() * 1000)
            qs = f"timestamp={ts}"
            sig = hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
            url = f"{futures_testnet}/fapi/v2/balance?{qs}&signature={sig}"
            req = urllib.request.Request(url)
            req.add_header("X-MBX-APIKEY", self.api_key)
            req.add_header("User-Agent", "Mozilla/5.0")
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = _json.loads(resp.read())
            result = {}
            for item in data:
                asset = item.get("asset", "")
                bal = float(item.get("balance", 0))
                avail = float(item.get("availableBalance", 0))
                if bal != 0:
                    result[asset] = {"total": bal, "free": avail, "used": bal - avail}
            total_usdt = result.get("USDT", {}).get("total", 0)
            return {"total_asset": total_usdt, "cash": total_usdt,
                    "market_value": 0, "pnl": 0, "raw": result}
        except Exception:
            return {}

    def connect(self) -> bool:
        """连接到交易所"""
        try:
            try:
                if hasattr(self.exchange, "load_time_difference"):
                    self.exchange.load_time_difference()
            except Exception as exc:
                logger.warning(f"加载交易所时间差失败，将继续尝试连接: {exc}")
            # 加载市场信息
            try:
                self.exchange.load_markets()
            except Exception as exc:
                logger.warning(f"加载市场信息失败: {exc}")
            # 验证API密钥
            if self.api_key and self.secret:
                try:
                    if self.market_type in ("future", "swap"):
                        balance = self.exchange.fetch_balance({"type": "future"})
                    else:
                        balance = self.exchange.fetch_balance()
                    logger.info(f"已连接到 {self.exchange_id}，余额: {balance.get('total', {})}")
                except Exception as exc:
                    # For Binance testnet, CCXT may fail on some endpoints (V3 account, leverageBracket)
                    # but the exchange is still usable. Try a simpler balance check.
                    if self.testnet and self.exchange_id.lower() == "binance":
                        logger.warning(f"CCXT fetch_balance 失败，尝试直接API: {exc}")
                        try:
                            balance = self._testnet_direct_balance()
                            if balance:
                                logger.info(f"直接API余额: {balance}")
                            else:
                                raise exc
                        except Exception:
                            raise exc
                    else:
                        raise
            else:
                logger.warning("未配置API密钥，仅支持行情查询")

            self.connected = True
            return True

        except Exception as e:
            logger.error(f"连接交易所失败: {e}")
            self.connected = False
            return False

    def disconnect(self) -> bool:
        """断开连接"""
        self.connected = False
        return True

    def place_order(self, order: Order) -> Order:
        """
        下单
        支持: 限价单、市价单
        """
        if not self.connected:
            raise RuntimeError("未连接到交易所")

        try:
            # CCXT 符号格式: BTC/USDT
            symbol = _normalize_ccxt_symbol(order.symbol, self.market_type, self.exchange_id)
            if self.market_type in ("future", "swap"):
                self._apply_futures_leverage(symbol)

            if order.order_type == OrderType.MARKET:
                # 市价单
                result = self.exchange.create_market_order(
                    symbol=symbol,
                    side=order.side.value,
                    amount=order.quantity
                )
            else:
                # 限价单
                result = self.exchange.create_limit_order(
                    symbol=symbol,
                    side=order.side.value,
                    amount=order.quantity,
                    price=order.price
                )

            # 解析返回结果
            order.id = str(result['id'])
            order.status = OrderStatus.SUBMITTED
            order.timestamp = datetime.now()

            # 获取成交信息（如果有）
            if 'filled' in result and result['filled']:
                order.filled_quantity = float(result['filled'])
                order.avg_price = float(result.get('average', order.price or 0))
                if order.filled_quantity >= order.quantity:
                    order.status = OrderStatus.FILLED

            logger.info(
                f"下单成功: {order.symbol} {order.side.value} "
                f"{order.quantity} @ {order.price or 'market'}"
            )
            return order

        except Exception as e:
            logger.error(f"下单失败 {order.symbol}: {e}")
            order.status = OrderStatus.REJECTED
            order.error_message = str(e)
            return order

    def cancel_order(self, order_id: str, symbol: str) -> bool:
        """撤单"""
        try:
            self.exchange.cancel_order(order_id, symbol)
            return True
        except Exception as e:
            logger.error(f"撤单失败: {e}")
            return False

    def get_order(self, order_id: str, symbol: str) -> Optional[Order]:
        """查询订单"""
        try:
            result = self.exchange.fetch_order(order_id, symbol)

            order = Order(
                id=str(result['id']),
                symbol=symbol,
                side=OrderSide(result['side']),
                order_type=OrderType(result['type']),
                quantity=float(result['amount']),
                price=float(result.get('price', 0)),
                status=OrderStatus(result['status']),
                filled_quantity=float(result.get('filled', 0)),
                avg_price=float(result.get('average', 0))
            )
            return order
        except Exception as e:
            logger.error(f"查询订单失败: {e}")
            return None

    def get_positions(self) -> List[Position]:
        """查询持仓"""
        try:
            if self.market_type in ("future", "swap") and hasattr(self.exchange, "fetch_positions"):
                raw_positions = self.exchange.fetch_positions()
                positions: List[Position] = []
                for item in raw_positions or []:
                    info = item.get("info", {}) if isinstance(item, dict) else {}
                    quantity = float(
                        item.get("contracts")
                        or item.get("amount")
                        or info.get("positionAmt")
                        or 0
                    )
                    if abs(quantity) <= 0:
                        continue
                    entry_price = float(
                        item.get("entryPrice")
                        or item.get("average")
                        or info.get("entryPrice")
                        or 0
                    )
                    current_price = float(
                        item.get("markPrice")
                        or item.get("lastPrice")
                        or info.get("markPrice")
                        or entry_price
                        or 0
                    )
                    symbol = str(item.get("symbol") or info.get("symbol") or "")
                    positions.append(
                        Position(
                            symbol=symbol,
                            quantity=quantity,
                            avg_price=entry_price,
                            current_price=current_price,
                        )
                    )
                return positions

            result = self.exchange.fetch_balance()
            positions = []

            # Build a price lookup via fetch_tickers (single API call)
            price_map: Dict[str, float] = {}
            try:
                tickers = self.exchange.fetch_tickers() or {}
                for sym, t in tickers.items():
                    if isinstance(t, dict):
                        last = t.get("last") or t.get("close") or 0
                        if last:
                            price_map[sym.upper()] = float(last)
            except Exception as exc:
                logger.warning(f"fetch_tickers failed in get_positions: {exc}")

            for currency, info in result.get('total', {}).items():
                amount = float(info or 0)
                if amount > 0:
                    pair = f"{currency}/{self.quote_asset}"
                    current_price = price_map.get(pair.upper(), 0.0)
                    pos = Position(
                        symbol=currency,
                        quantity=amount,
                        avg_price=0,
                        current_price=current_price,
                    )
                    positions.append(pos)

            return positions
        except Exception as e:
            logger.error(f"查询持仓失败: {e}")
            return []

    def get_balance(self) -> Dict[str, float]:
        """查询资金"""
        try:
            params = {"type": "future"} if self.market_type in ("future", "swap") else {}
            result = self.exchange.fetch_balance(params)
            total = float(result.get('total', {}).get(self.quote_asset, 0) or 0)
            free = float(result.get('free', {}).get(self.quote_asset, 0) or 0)
            used = float(result.get('used', {}).get(self.quote_asset, 0) or 0)
            pnl = 0.0

            if self.market_type in ("future", "swap"):
                info = result.get("info", {}) if isinstance(result, dict) else {}
                assets = info.get("assets") if isinstance(info, dict) else None
                if isinstance(assets, list):
                    target = next(
                        (
                            item for item in assets
                            if str(item.get("asset") or "").upper() == self.quote_asset
                        ),
                        None,
                    )
                    if target:
                        total = float(
                            target.get("walletBalance")
                            or target.get("marginBalance")
                            or total
                            or 0
                        )
                        free = float(
                            target.get("availableBalance")
                            or target.get("maxWithdrawAmount")
                            or free
                            or 0
                        )
                        pnl = float(target.get("unrealizedProfit") or 0)
                        used = max(total - free, 0.0)

            return {
                'total_asset': total,
                'cash': free,
                'market_value': total - free,
                'pnl': pnl,
            }
        except Exception as e:
            logger.error(f"查询资金失败: {e}")
            return {'cash': 0, 'total_asset': 0, 'market_value': 0, 'pnl': 0}

    def get_ticker(self, symbol: str) -> Dict:
        """获取实时行情"""
        try:
            result = self.exchange.fetch_ticker(
                _normalize_ccxt_symbol(symbol, self.market_type, self.exchange_id)
            )
            return {
                'symbol': symbol,
                'last': float(result['last']),
                'bid': float(result.get('bid', 0)),
                'ask': float(result.get('ask', 0)),
                'volume': float(result.get('baseVolume', 0)),
                'timestamp': result.get('timestamp', 0)
            }
        except Exception as e:
            logger.error(f"获取行情失败: {e}")
            return {}

    def _apply_futures_leverage(self, symbol: str) -> None:
        leverage = max(min(int(self.default_leverage), int(self.max_leverage)), 1)
        if leverage <= 1:
            return
        try:
            if hasattr(self.exchange, "set_leverage"):
                self.exchange.set_leverage(leverage, symbol)
                return
        except Exception as exc:
            logger.warning(f"设置合约杠杆失败，将继续按默认杠杆尝试下单: {exc}")

        try:
            market = self.exchange.market(symbol)
            market_id = market.get("id") or symbol.replace("/", "").replace(":", "")
            if hasattr(self.exchange, "fapiPrivatePostLeverage"):
                self.exchange.fapiPrivatePostLeverage(
                    {"symbol": market_id, "leverage": leverage}
                )
        except Exception as exc:
            logger.warning(f"Binance futures leverage API 调用失败: {exc}")


class ExecutionManager:
    """执行管理器 - 统一接口"""

    def __init__(
        self,
        provider: Union[str, ExecutionProvider],
        **provider_kwargs
    ):
        """
        初始化执行管理器
        provider: 执行器名称 ('easytrader', 'ccxt') 或 ExecutionProvider 实例
        """
        if isinstance(provider, str):
            self.provider = self._create_provider(provider, **provider_kwargs)
        else:
            self.provider = provider

        self.order_history: List[Order] = []
        self.position_history: List[Position] = []

    def _create_provider(self, name: str, **kwargs) -> ExecutionProvider:
        """工厂方法创建执行器"""
        if name == "ccxt_futures":
            kwargs.setdefault("market_type", "swap")
            return CCXTExecutionProvider(**kwargs)
        providers = {
            'easytrader': EasyTraderProvider,
            'ccxt': CCXTExecutionProvider,
        }
        if name not in providers:
            raise ValueError(f"不支持的执行器: {name}。可选: {list(providers.keys())}")
        return providers[name](**kwargs)

    def connect(self) -> bool:
        """连接到交易平台"""
        return self.provider.connect()

    def disconnect(self) -> bool:
        """断开连接"""
        return self.provider.disconnect()

    def buy(
        self,
        symbol: str,
        quantity: float,
        price: Optional[float] = None,
        order_type: str = "market"
    ) -> Order:
        """
        买入
        symbol: 标的代码（A股需带后缀，如 '000001.SZ'；加密币如 'BTC/USDT'）
        quantity: 数量（A股为股数，加密币为币数）
        price: 限价单价格
        order_type: 'market' 或 'limit'
        """
        order = Order(
            id="",
            symbol=symbol,
            side=OrderSide.BUY,
            order_type=OrderType(order_type),
            quantity=quantity,
            price=price,
            timestamp=datetime.now()
        )

        filled_order = self.provider.place_order(order)
        self.order_history.append(filled_order)
        return filled_order

    def sell(
        self,
        symbol: str,
        quantity: float,
        price: Optional[float] = None,
        order_type: str = "market"
    ) -> Order:
        """卖出"""
        order = Order(
            id="",
            symbol=symbol,
            side=OrderSide.SELL,
            order_type=OrderType(order_type),
            quantity=quantity,
            price=price,
            timestamp=datetime.now()
        )

        filled_order = self.provider.place_order(order)
        self.order_history.append(filled_order)
        return filled_order

    def cancel(self, order_id: str) -> bool:
        """取消订单"""
        result = self.provider.cancel_order(order_id)
        if result:
            for o in self.order_history:
                if o.id == order_id:
                    o.status = OrderStatus.CANCELLED
        return result

    def get_positions(self) -> List[Position]:
        """获取当前持仓"""
        positions = self.provider.get_positions()
        self.position_history = positions
        return positions

    def get_balance(self) -> Dict[str, float]:
        """获取账户资金"""
        return self.provider.get_balance()

    def get_orders(self, symbol: Optional[str] = None) -> List[Order]:
        """获取订单历史"""
        if symbol:
            return [o for o in self.order_history if o.symbol == symbol]
        return self.order_history

    def get_position(self, symbol: str) -> Optional[Position]:
        """获取指定标的持仓"""
        for pos in self.get_positions():
            if pos.symbol == symbol:
                return pos
        return None


# 便捷函数
def create_executor(
    mode: str = "ccxt",
    **kwargs
) -> ExecutionManager:
    """
    快速创建执行器
    示例:
        # 加密货币 (币安)
        exec = create_executor("ccxt", exchange_id="binance", api_key="...", secret="...")

        # A股 easytrader
        exec = create_executor("easytrader", client="huatai")
    """
    normalized = str(mode or "").strip().lower()
    if normalized in {"gm", "gmtrade"}:
        return ExecutionManager(GmTradeProvider(**kwargs))
    return ExecutionManager(mode, **kwargs)
