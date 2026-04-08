#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
OpenClaw Trading Agent - 增强版
集成: Sensory(多源数据) + Execution(多交易所) + Gatekeeper(飞书审批) + 多策略引擎

策略支持:
  1. MA_CROSS      - 传统移动平均交叉
  2. LLM_SIGNAL    - LLM 技术分析信号 (OpenRouter/Ollama)
  3. SMART_MONEY   - 跟单 Smart Money 链上活动 (BSC/Solana)
  4. MULTI         - 多策略融合 (MA + LLM + SmartMoney 共识)

新增集成:
  - TradingView 实时行情和技术指标
  - CryptoClaw Binance 市场排名和智能资金信号
  - 信号等级系统 (S1-S5)
  - 多时间框架确认
  - 基于信号强度的动态仓位管理
"""

import sys
import os
import json
import yaml
import argparse
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Union, Any

# Add parent directory to path for imports
ROOT = Path(__file__).parent.parent.absolute()
sys.path.insert(0, str(ROOT))

# Import our modules
from sensory import (
    create_data_manager,
    TradingViewProvider,
    BinanceMarketRankProvider,
    SmartMoneySignalProvider,
    compute_all_indicators,
    compute_rsi,
    compute_macd,
)
from execution import create_executor, OrderSide, OrderType
from gatekeeper import GateKeeperSkill

# Try importing signal engine
try:
    from sensory.signal_engine import SignalEngine, score_to_tier
except ImportError:
    SignalEngine = None

# Configure logging
log_dir = ROOT / "logs"
log_dir.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_dir / "trading.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("trading-agent")


# ========== 策略基类和注册表 ==========

class Strategy:
    """交易策略基类"""

    name: str = "base"
    description: str = "Base strategy"

    def generate_signal(self, df, symbol: str, **kwargs) -> Dict:
        """生成交易信号"""
        return {"action": "hold", "confidence": 0.0, "reason": "Base strategy"}

    def required_data_sources(self) -> List[str]:
        return ["ccxt"]


class MACrossStrategy(Strategy):
    """MA 交叉策略"""

    name = "ma_cross"
    description = "移动平均线交叉策略 (MA5/MA20)"

    def generate_signal(self, df, symbol: str, **kwargs) -> Dict:
        if df is None or len(df) < 20:
            return {"action": "hold", "confidence": 0.0, "reason": "Insufficient data"}

        close = df['close']
        ma5 = close.rolling(5).mean().iloc[-1]
        ma20 = close.rolling(20).mean().iloc[-1]
        ma5_prev = close.rolling(5).mean().iloc[-2]
        ma20_prev = close.rolling(20).mean().iloc[-2]

        current_price = close.iloc[-1]

        # Golden cross: MA5 crosses above MA20
        if ma5 > ma20 and ma5_prev <= ma20_prev:
            return {
                "action": "buy",
                "confidence": 0.6,
                "reason": f"Golden cross: MA5({ma5:.2f}) > MA20({ma20:.2f})",
                "price": current_price,
                "indicators": {"MA5": ma5, "MA20": ma20}
            }
        # Death cross: MA5 crosses below MA20
        elif ma5 < ma20 and ma5_prev >= ma20_prev:
            return {
                "action": "sell",
                "confidence": 0.6,
                "reason": f"Death cross: MA5({ma5:.2f}) < MA20({ma20:.2f})",
                "price": current_price,
                "indicators": {"MA5": ma5, "MA20": ma20}
            }

        return {
            "action": "hold",
            "confidence": 0.0,
            "reason": "No MA crossover signal",
            "price": current_price
        }


class LLMSignalStrategy(Strategy):
    """LLM 信号策略 - 使用 LLM 进行技术分析"""

    name = "llm_signal"
    description = "LLM 技术分析信号 (OpenRouter/Ollama)"

    def __init__(self, api_key: str = None, model: str = "openai/gpt-4o-mini",
                 use_local: bool = False):
        if SignalEngine is None:
            raise ImportError("signal_engine 模块不可用")
        self.engine = SignalEngine(
            api_key=api_key,
            model=model,
            use_local=use_local
        )

    def generate_signal(self, df, symbol: str, **kwargs) -> Dict:
        try:
            indicators = compute_all_indicators(df)
            result = self.engine.analyze(symbol, df, indicators)

            return {
                "action": result["signal_type"].lower(),
                "confidence": result["score"] / 100.0,
                "reason": result["reason"],
                "price": result["entry"],
                "tier": result["tier"],
                "target": result["target"],
                "stop": result["stop"],
                "indicators": indicators,
                "llm_raw": result.get("raw_response", "")
            }
        except Exception as e:
            logger.error(f"LLM 信号分析失败: {e}")
            return {"action": "hold", "confidence": 0.0, "reason": f"LLM分析失败: {e}"}


class SmartMoneyStrategy(Strategy):
    """Smart Money 跟单策略 - 跟踪专业投资者链上活动"""

    name = "smart_money"
    description = "跟单 Smart Money 链上买卖信号 (BSC/Solana)"

    def __init__(self, chain_id: int = 56):
        """
        chain_id: 56=BSC, CT_501=Solana
        """
        self.chain_id = chain_id
        self.provider = SmartMoneySignalProvider(chain_id=chain_id)

    def generate_signal(self, df, symbol: str, **kwargs) -> Dict:
        """
        根据 Smart Money 信号生成交易决策

        策略逻辑:
        - 检测到 buy 信号 → 买入
        - 检测到 sell 信号 → 卖出
        - 根据 smart_money_count 和 exit_rate 调整置信度
        """
        try:
            # 获取该符号的最新信号
            signals = self.provider.get_signals(symbol=symbol, limit=5)
            if not signals:
                return {"action": "hold", "confidence": 0.0, "reason": "无 Smart Money 信号"}

            signal = signals[0]  # 最新的信号
            direction = signal.get("direction", "").lower()
            status = signal.get("status", "")
            smart_count = signal.get("smart_money_count", 0)
            exit_rate = signal.get("exit_rate", 0.0)
            max_gain = signal.get("max_gain_pct", 0.0)

            # 只关注活跃信号
            if status != "active":
                return {"action": "hold", "confidence": 0.0, "reason": f"信号状态: {status}"}

            confidence = min(0.5 + smart_count * 0.1, 1.0)  # 智能钱越多越 confident
            confidence *= (1.0 - exit_rate * 0.5)  # 退出比例高则降低 confidence

            if direction == "buy" and max_gain < 50:  # 避免追高
                return {
                    "action": "buy",
                    "confidence": confidence,
                    "reason": f"Smart Money 买入 ({smart_count} 个钱包, 最高涨幅 {max_gain:.1f}%)",
                    "price": signal.get("current_price"),
                    "signal_id": signal.get("signalId"),
                    "smart_money_count": smart_count,
                    "max_gain_pct": max_gain
                }
            elif direction == "sell" and max_gain > -10:  #  Smart Money 开始卖出
                return {
                    "action": "sell",
                    "confidence": confidence,
                    "reason": f"Smart Money 卖出 ({smart_count} 个钱包, 退出率 {exit_rate:.1%})",
                    "price": signal.get("current_price"),
                    "signal_id": signal.get("signalId")
                }
            else:
                return {"action": "hold", "confidence": 0.0, "reason": "信号不符合跟单条件"}

        except Exception as e:
            logger.error(f"SmartMoney 策略失败: {e}")
            return {"action": "hold", "confidence": 0.0, "reason": f"SmartMoney 错误: {e}"}

    def required_data_sources(self) -> List[str]:
        return ["smartmoney"]


class MultiStrategy(Strategy):
    """多策略融合 - 综合多个策略的信号"""

    name = "multi"
    description = "多策略融合 (MA + LLM + SmartMoney 共识)"

    def __init__(self, strategies: List[Strategy], weights: Dict[str, float] = None):
        """
        Args:
            strategies: 策略列表
            weights: 策略权重 {strategy_name: weight}
        """
        self.strategies = strategies
        self.weights = weights or {
            "ma_cross": 0.2,
            "llm_signal": 0.4,
            "smart_money": 0.4
        }

    def generate_signal(self, df, symbol: str, **kwargs) -> Dict:
        signals = []
        for strategy in self.strategies:
            try:
                sig = strategy.generate_signal(df, symbol, **kwargs)
                sig["strategy"] = strategy.name
                signals.append(sig)
            except Exception as e:
                logger.debug(f"策略 {strategy.name} 失败: {e}")

        if not signals:
            return {"action": "hold", "confidence": 0.0, "reason": "所有策略失败"}

        # 计算加权投票
        buy_weight = sum(
            self.weights.get(s["strategy"], 1.0) * s["confidence"]
            for s in signals if s["action"] == "buy"
        )
        sell_weight = sum(
            self.weights.get(s["strategy"], 1.0) * s["confidence"]
            for s in signals if s["action"] == "sell"
        )
        total_weight = sum(self.weights.values())

        # 决策
        threshold = 0.5 * total_weight
        if buy_weight > sell_weight and buy_weight >= threshold:
            # 找出置信度最高的买入信号
            best = max((s for s in signals if s["action"] == "buy"),
                       key=lambda x: x["confidence"])
            return {
                "action": "buy",
                "confidence": buy_weight / total_weight,
                "reason": f"多策略共识: {[s['strategy'] + '(' + s['action'] + ')' for s in signals]}",
                "price": best.get("price"),
                "signals": signals
            }
        elif sell_weight > buy_weight and sell_weight >= threshold:
            best = max((s for s in signals if s["action"] == "sell"),
                       key=lambda x: x["confidence"])
            return {
                "action": "sell",
                "confidence": sell_weight / total_weight,
                "reason": f"多策略共识: {[s['strategy'] + '(' + s['action'] + ')' for s in signals]}",
                "price": best.get("price"),
                "signals": signals
            }

        return {"action": "hold", "confidence": 0.0, "reason": "未达到决策阈值"}


# ========== 交易代理主类 ==========

class TradingAgent:
    """OpenClaw 增强版交易代理"""

    STRATEGY_CLASSES = {
        "ma_cross": MACrossStrategy,
        "llm_signal": LLMSignalStrategy,
        "smart_money": SmartMoneyStrategy,
        "multi": MultiStrategy,
    }

    def __init__(self, config_path: str = None):
        self.config = self._load_config(config_path)
        self.data_mgr = None
        self.executor = None
        self.gatekeeper = None
        self.strategy = None
        self.initialized = False

    def _load_config(self, config_path: str) -> Dict:
        candidates = [
            config_path,
            ROOT / "config.yaml",
            ROOT / ".." / "config.yaml",
            Path("C:/Users/Roy/.openclaw/config.yaml"),
        ]
        for path in candidates:
            if path is None:
                continue
            path = Path(path)
            if path.exists():
                with open(path, 'r', encoding='utf-8') as f:
                    return yaml.safe_load(f)
        logger.warning("未找到配置文件，使用默认配置")
        return {
            'sensory': {'default_source': 'yfinance'},
            'execution': {'default': 'ccxt', 'ccxt': {'testnet': True}},
            'gatekeeper': {'enabled': True, 'manual_approval': False},
            'strategy': {'type': 'ma_cross'}
        }

    def initialize(self) -> bool:
        logger.info("初始化交易代理...")

        # 1. 初始化数据源
        sensory_cfg = self.config.get('sensory', {})
        source = sensory_cfg.get('default_source', 'yfinance')
        self.data_mgr = create_data_manager(source)
        logger.info(f"  数据源: {source}")

        # 2. 初始化执行器
        exec_cfg = self.config.get('execution', {})
        exec_type = exec_cfg.get('default', 'ccxt')
        ccxt_cfg = exec_cfg.get('ccxt', {})
        self.executor = create_executor(
            exec_type,
            testnet=ccxt_cfg.get('testnet', True),
            exchange_id=ccxt_cfg.get('exchange', 'binance')
        )
        if not self.executor.connect():
            logger.error("  执行器连接失败")
            return False
        logger.info(f"  执行器: {exec_type}")

        # 3. 初始化风控闸门
        gate_cfg = self.config.get('gatekeeper', {})
        if gate_cfg.get('enabled', True):
            self.gatekeeper = GateKeeperSkill(str(ROOT / "gatekeeper" / "config.yaml"))
            logger.info(f"  风控闸门: 启用, 人工审批={gate_cfg.get('manual_approval', True)}")
        else:
            logger.info("  风控闸门: 禁用")

        # 4. 初始化策略
        strat_cfg = self.config.get('strategy', {})
        strat_type = strat_cfg.get('type', 'ma_cross')
        self.strategy = self._create_strategy(strat_type, strat_cfg)
        logger.info(f"  策略: {self.strategy.name} - {self.strategy.description}")

        self.initialized = True
        logger.info("交易代理初始化完成")
        return True

    def _create_strategy(self, strat_type: str, cfg: Dict) -> Strategy:
        """创建策略实例"""
        if strat_type not in self.STRATEGY_CLASSES:
            logger.warning(f"未知策略: {strat_type}, 使用 ma_cross")
            strat_type = "ma_cross"

        StrategyClass = self.STRATEGY_CLASSES[strat_type]

        if strat_type == "llm_signal":
            return StrategyClass(
                api_key=cfg.get('llm_api_key') or os.getenv('OPENROUTER_API_KEY'),
                model=cfg.get('llm_model', 'openai/gpt-4o-mini'),
                use_local=cfg.get('use_local_llm', False)
            )
        elif strat_type == "smart_money":
            return StrategyClass(chain_id=cfg.get('chain_id', 56))
        elif strat_type == "multi":
            # 构建子策略
            sub_strategies = []
            weights = cfg.get('weights', {})
            for name in ["ma_cross", "llm_signal", "smart_money"]:
                if cfg.get(f"enable_{name}", True):
                    try:
                        sub_strategies.append(self._create_strategy(name, cfg))
                    except Exception as e:
                        logger.warning(f"子策略 {name} 创建失败: {e}")
            return MultiStrategy(sub_strategies, weights)
        else:
            return StrategyClass()

    # ========== 核心交易逻辑 ==========

    def run_single_cycle(self, symbol: str, quantity: float = None) -> Optional[Dict]:
        """运行单次交易周期"""
        logger.info(f"=== 交易周期: {symbol} ===")

        # 1. 获取数据
        df = self.data_mgr.get_data(symbol, limit=100)
        if len(df) < 20:
            logger.warning("  数据不足，跳过")
            return None

        # 2. 生成信号
        signal = self.strategy.generate_signal(df, symbol)
        logger.info(f"  信号: {signal['action'].upper()} (置信度: {signal['confidence']:.1%})")
        logger.info(f"  原因: {signal.get('reason', 'N/A')}")
        logger.info(f"  价格: ${signal.get('price', 'N/A')}")

        if signal['action'] == 'hold':
            logger.info("  无需操作")
            return None

        # 3. 计算仓位 (基于置信度)
        base_qty = quantity or 0.001
        adjusted_qty = base_qty * signal['confidence']
        logger.info(f"  调整仓位: {base_qty} × {signal['confidence']:.1%} = {adjusted_qty:.6f}")

        # 4. 执行（带闸门审批）
        order = self.execute_with_approval(
            symbol=symbol,
            side=signal['action'],
            quantity=adjusted_qty,
            price=signal.get('price'),
            reason=signal.get('reason'),
            strategy=self.strategy.name
        )

        return order

    def execute_with_approval(self, symbol: str, side: str, quantity: float,
                              price=None, reason: str = "", strategy: str = ""):
        """带闸门审批的执行"""
        logger.info(f"交易请求: {side.upper()} {quantity} {symbol}")

        if self.gatekeeper:
            logger.info("  请求审批...")
            approved = self.gatekeeper.approve_before_execute(
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
                order_type='market',
                reason=reason,
                strategy=strategy,
                executor=self.executor
            )
            if not approved:
                logger.warning("  交易被拒绝")
                return None
            logger.info("  已批准")

        logger.info("  执行订单...")
        if side == 'buy':
            order = self.executor.buy(symbol, quantity, price=price, order_type='market')
        else:
            order = self.executor.sell(symbol, quantity, price=price, order_type='market')

        logger.info(f"  订单 {order.id}: {order.status.value}")
        if order.error_message:
            logger.error(f"  错误: {order.error_message}")

        return order

    def show_status(self):
        """显示账户状态"""
        print("\n" + "=" * 60)
        print("[Trading Account Status]")
        print("=" * 60)

        balance = self.executor.get_balance()
        print(f"Total Asset: ${balance.get('total_asset', 0):,.2f}")
        print(f"Cash:        ${balance.get('cash', 0):,.2f}")
        print(f"Market Val:  ${balance.get('market_value', 0):,.2f}")
        print(f"P&L:         ${balance.get('pnl', 0):,.2f}")

        positions = self.executor.get_positions()
        if positions:
            print(f"\nPositions ({len(positions)}):")
            for pos in positions[:10]:
                print(f"  {pos.symbol}: {pos.quantity} @ ${pos.avg_price:.2f} "
                      f"(P&L: ${pos.unrealized_pnl:.2f})")
        else:
            print("\nNo open positions")

    def scan_signals(self, symbols: List[str], min_score: float = 80.0) -> List[Dict]:
        """扫描市场信号"""
        logger.info(f"扫描 {len(symbols)} 个符号，最低分数: {min_score}")

        if SignalEngine is None:
            logger.error("SignalEngine 不可用，请安装依赖")
            return []

        engine = SignalEngine(
            api_key=os.getenv('OPENROUTER_API_KEY'),
            model=self.config.get('strategy', {}).get('llm_model', 'openai/gpt-4o-mini')
        )
        return engine.scan_market(symbols, self.data_mgr, min_score)

    def get_trending_tokens(self, limit: int = 20, rank_type: str = "10") -> List[Dict]:
        """获取 Binance 热门代币"""
        provider = BinanceMarketRankProvider(chain_id=56)
        return provider.get_trending_symbols(rank_type=rank_type, limit=limit)

    def get_smart_money_signals(self, chain_id: int = 56, limit: int = 20) -> List[Dict]:
        """获取 Smart Money 信号"""
        provider = SmartMoneySignalProvider(chain_id=chain_id)
        return provider.get_signals(limit=limit)

    def run_continuous(self, symbols: List[str], interval_seconds: int = 300,
                       quantity: float = 0.001):
        """连续交易循环"""
        logger.info(f"连续交易模式: {symbols}, 间隔: {interval_seconds}s")

        try:
            while True:
                for symbol in symbols:
                    try:
                        order = self.run_single_cycle(symbol, quantity)
                        if order:
                            logger.info(f"已执行: {order.id}")
                    except Exception as e:
                        logger.error(f"交易 {symbol} 出错: {e}")

                self.show_status()
                logger.info(f"等待 {interval_seconds}s...")
                import time
                time.sleep(interval_seconds)
        except KeyboardInterrupt:
            logger.info("用户中断")


# ========== 主程序入口 ==========

def main():
    parser = argparse.ArgumentParser(
        description='OpenClaw 增强版交易代理 - 支持多策略、LLM信号、Smart Money跟单',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
策略选项:
  ma_cross       - 移动平均交叉 (默认)
  llm_signal     - LLM 技术分析 (需 OPENROUTER_API_KEY)
  smart_money    - Smart Money 链上跟单
  multi          - 多策略融合

示例:
  # 单次交易 (MA交叉策略)
  python trading_agent.py --once --symbol BTC/USDT

  # 使用 LLM 信号
  python trading_agent.py --strategy llm_signal --once --symbol BTC/USDT

  # 多策略融合
  python trading_agent.py --strategy multi --once --symbol BTC/USDT

  # 扫描市场信号
  python trading_agent.py --scan --symbols BTC/USDT,ETH/USDT,SOL/USDT

  # 查看热门代币
  python trading_agent.py --trending --limit 10

  # 查看 Smart Money 信号
  python trading_agent.py --smart-money --chain bsc

  # 查看账户状态
  python trading_agent.py --status
        """
    )

    # 基本参数
    parser.add_argument('--symbol', default='BTC/USDT', help='交易符号')
    parser.add_argument('--quantity', type=float, default=0.001, help='基础交易数量')
    parser.add_argument('--once', action='store_true', help='单次运行')
    parser.add_argument('--interval', type=int, default=300, help='循环间隔(秒)')
    parser.add_argument('--status', action='store_true', help='仅显示账户状态')
    parser.add_argument('--config', help='配置文件路径')

    # 策略选择
    parser.add_argument('--strategy',
                        choices=['ma_cross', 'llm_signal', 'smart_money', 'multi'],
                        default='ma_cross',
                        help='交易策略')

    # LLM 参数
    parser.add_argument('--llm-api-key', help='OpenRouter API Key')
    parser.add_argument('--llm-model', default='openai/gpt-4o-mini',
                        help='LLM 模型 (OpenRouter 或本地 Ollama 模型名)')
    parser.add_argument('--use-local-llm', action='store_true',
                        help='使用本地 Ollama 而非 OpenRouter')

    # 扫描命令
    parser.add_argument('--scan', action='store_true',
                        help='扫描市场信号')
    parser.add_argument('--scan-symbols',
                        help='扫描符号列表，逗号分隔 (如 BTC/USDT,ETH/USDT)')
    parser.add_argument('--min-score', type=float, default=80.0,
                        help='LLM 信号最低分数 (0-100)')

    # 热门代币命令
    parser.add_argument('--trending', action='store_true',
                        help='显示 Binance 热门代币')
    parser.add_argument('--limit', type=int, default=20,
                        help='返回数量')

    # Smart Money 命令
    parser.add_argument('--smart-money', action='store_true',
                        help='显示 Smart Money 信号')
    parser.add_argument('--chain', choices=['bsc', 'solana'], default='bsc',
                        help='链网络')

    args = parser.parse_args()

    # 创建代理
    agent = TradingAgent(config_path=args.config)

    # 初始化
    if not agent.initialize():
        logger.error("初始化失败")
        sys.exit(1)

    # 执行命令
    if args.status:
        agent.show_status()

    elif args.scan:
        symbols = args.scan_symbols.split(',') if args.scan_symbols else [args.symbol]
        signals = agent.scan_signals(symbols, min_score=args.min_score)
        print(f"\n=== 扫描结果 ({len(signals)} 个信号) ===")
        for sig in signals:
            print(f"\n{sig['symbol']}: {sig['signal_type']} (分数: {sig['score']:.1f}, 等级: {sig['tier']})")
            print(f"  入场: ${sig['entry']:.2f}, 目标: ${sig['target']:.2f}, 止损: ${sig['stop']:.2f}")
            print(f"  原因: {sig['reason']}")

    elif args.trending:
        tokens = agent.get_trending_tokens(limit=args.limit)
        print(f"\n=== Binance 热门代币 (Top {len(tokens)}) ===")
        for token in tokens:
            print(f"  {token['symbol']}: ${token.get('price', 'N/A'):.4f} "
                  f"({token.get('change_24h', 'N/A'):+.1f}%)  Volume: {token.get('volume', 'N/A')}")

    elif args.smart_money:
        chain_id = 56 if args.chain == 'bsc' else 501
        signals = agent.get_smart_money_signals(chain_id=chain_id, limit=args.limit)
        print(f"\n=== Smart Money 信号 ({args.chain.upper()}) ===")
        for sig in signals:
            direction_icon = "🟢" if sig['direction'] == 'buy' else "🔴"
            print(f"  {direction_icon} {sig['symbol']}: {sig['direction'].upper()}")
            print(f"    智能钱数量: {sig['smart_money_count']}, 最高涨幅: {sig['max_gain_pct']:.1f}%")
            print(f"    当前价: ${sig['current_price']:.4f}, 触发价: ${sig['trigger_price']:.4f}")
            print(f"    退出率: {sig['exit_rate']:.1%}, 状态: {sig['status']}")

    elif args.once:
        logger.info(f"单次交易: {args.symbol}, 策略: {args.strategy}")
        order = agent.run_single_cycle(args.symbol, args.quantity)
        if order:
            logger.info(f"订单结果: {order.status.value}")
        agent.show_status()

    else:
        # 连续模式
        agent.run_continuous([args.symbol], args.interval, args.quantity)


if __name__ == "__main__":
    main()
