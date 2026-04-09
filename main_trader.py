#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
OpenClaw 完整交易流程示例
串联: 感官(Sensory) → 闸门(Gatekeeper) → 手脚(Execution)
"""
import sys
import os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 导入三个核心模块
from sensory import create_data_manager, DataManager
from execution import create_executor, OrderSide, OrderType
from gatekeeper import GateKeeperSkill

import yaml


def load_config():
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


class OpenClawTrader:
    """
    OpenClaw 自动交易器
    整合: 感官 + 闸门 + 手脚
    """

    def __init__(self, config_path: str = "config.yaml"):
        # 加载配置
        self.config = load_config()

        # 1. 初始化感官 (数据获取)
        print("1. 初始化感官模块...")
        sensory_config = self.config.get('sensory', {})
        self.data_mgr = create_data_manager(
            source=sensory_config.get('default_source', 'yfinance')
        )

        # 2. 初始化手脚 (交易执行)
        print("2. 初始化手脚模块...")
        exec_config = self.config.get('execution', {})
        self.executor = create_executor(
            mode=exec_config.get('default', 'ccxt'),
            testnet=exec_config.get('ccxt', {}).get('testnet', True)  # 默认测试网
        )

        # 3. 初始化闸门 (人工确认)
        print("3. 初始化风控闸门...")
        gate_config = self.config.get('gatekeeper', {})
        if gate_config.get('enabled', True):
            self.gatekeeper = GateKeeperSkill(config_path)
            print(f"   闸门状态: {'启用' if gate_config.get('manual_approval', True) else '禁用'}")
            print(f"   确认超时: {gate_config.get('timeout_seconds', 300)}秒")
        else:
            self.gatekeeper = None
            print("   闸门状态: 已关闭（直接执行）")

        # 连接
        print("4. 连接交易所...")
        self.executor.connect()

        print("✅ OpenClaw 初始化完成!\n")

    def analyze_signal(self, symbol: str, timeframe: str = "1d") -> dict:
        """
        分析交易信号（简化版）
        返回: {'action': 'buy'|'sell'|'hold', 'reason': str, 'confidence': float}
        """
        # 获取历史数据
        df = self.data_mgr.get_data(symbol, timeframe=timeframe, limit=100)

        if df.empty:
            return {'action': 'hold', 'reason': '数据为空', 'confidence': 0.0}

        # 简单均线策略示例
        close = df['close']
        ma5 = close.rolling(5).mean().iloc[-1]
        ma20 = close.rolling(20).mean().iloc[-1]
        current_price = close.iloc[-1]

        # 金叉: MA5 上穿 MA20
        if ma5 > ma20 and close.rolling(5).mean().iloc[-2] <= close.rolling(20).mean().iloc[-2]:
            return {
                'action': 'buy',
                'reason': f'MA5({ma5:.2f}) 上穿 MA20({ma20:.2f}) - 金叉买入信号',
                'confidence': 0.7,
                'price': current_price
            }
        # 死叉: MA5 下穿 MA20
        elif ma5 < ma20 and close.rolling(5).mean().iloc[-2] >= close.rolling(20).mean().iloc[-2]:
            return {
                'action': 'sell',
                'reason': f'MA5({ma5:.2f}) 下穿 MA20({ma20:.2f}) - 死叉卖出信号',
                'confidence': 0.7,
                'price': current_price
            }

        return {
            'action': 'hold',
            'reason': '无明确信号',
            'confidence': 0.0,
            'price': current_price
        }

    def execute_with_gatekeeper(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float = None,
        order_type: str = "market",
        reason: str = "",
        strategy: str = "MA_Crossover"
    ):
        """
        执行交易（带闸门）
        流程: 分析 → 闸门确认 → 执行
        """
        print(f"\n{'='*50}")
        print(f"处理交易请求: {symbol}")
        print(f"{'='*50}")

        # 步骤1: 闸门确认（如启用）
        if self.gatekeeper:
            print(f"⏳ 等待人工确认...")
            approved = self.gatekeeper.approve_before_execute(
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
                order_type=order_type,
                reason=reason,
                strategy=strategy,
                executor=self.executor
            )

            if not approved:
                print("❌ 交易被拒绝或超时，不执行")
                return None
            print("✅ 已批准，继续执行...")

        # 步骤2: 执行交易
        print(f"🚀 执行下单: {side} {quantity} {symbol}")

        if side == 'buy':
            order = self.executor.buy(
                symbol=symbol,
                quantity=quantity,
                price=price,
                order_type=order_type
            )
        else:
            order = self.executor.sell(
                symbol=symbol,
                quantity=quantity,
                price=price,
                order_type=order_type
            )

        print(f"   订单ID: {order.id}")
        print(f"   状态: {order.status.value}")
        print(f"   已成交: {order.filled_quantity}")
        if order.avg_price:
            print(f"   成交均价: {order.avg_price}")

        return order

    def auto_trade(self, symbol: str, quantity: float = 0.01):
        """
        自动交易主循环（单次）
        """
        print(f"\n🤖 OpenClaw 自动交易")
        print(f"   标的: {symbol}")
        print(f"   数量: {quantity}")

        # 分析信号
        print("\n📊 分析市场信号...")
        signal = self.analyze_signal(symbol)

        print(f"   操作建议: {signal['action']}")
        print(f"   原因: {signal['reason']}")
        print(f"   置信度: {signal['confidence']:.1%}")

        if signal['action'] == 'hold':
            print("\n⏸️  无交易信号，保持观望")
            return None

        # 执行交易（带闸门）
        order = self.execute_with_gatekeeper(
            symbol=symbol,
            side=signal['action'],
            quantity=quantity,
            price=signal.get('price'),
            order_type="market",
            reason=signal['reason'],
            strategy="MA_Crossover"
        )

        return order

    def show_status(self):
        """显示当前状态"""
        print("\n" + "="*50)
        print("📋 当前状态")
        print("="*50)

        # 余额
        balance = self.executor.get_balance()
        print(f"总资产: ${balance.get('total_asset', 0):,.2f}")
        print(f"可用现金: ${balance.get('cash', 0):,.2f}")
        print(f"持仓市值: ${balance.get('market_value', 0):,.2f}")

        # 持仓
        positions = self.executor.get_positions()
        if positions:
            print(f"\n持仓标的 ({len(positions)}):")
            for pos in positions[:10]:
                print(f"  {pos.symbol}: {pos.quantity} @ ${pos.avg_price:.2f}")
        else:
            print("\n暂无持仓")

    def close(self):
        """关闭连接"""
        self.executor.disconnect()
        print("\n👋 OpenClaw 已关闭")


def main():
    """主函数"""
    print("="*60)
    print("OpenClaw 自动交易系统 - 集成演示")
    print("="*60)

    try:
        # 创建交易器
        trader = OpenClawTrader()

        # 显示当前状态
        trader.show_status()

        # 自动交易示例（测试网）
        print("\n" + "="*60)
        print("🎯 开始自动交易流程...")
        print("="*60)

        # 选择标的（BTC/USDT 测试网）
        symbol = "BTC/USDT"

        # 执行一次自动交易
        order = trader.auto_trade(symbol, quantity=0.001)

        # 显示最终状态
        trader.show_status()

        # 闸门统计
        if trader.gatekeeper:
            stats = trader.gatekeeper.get_statistics()
            print(f"\n📊 闸门统计:")
            print(f"   总审批: {stats['total_requests']}")
            print(f"   通过率: {stats['approval_rate']:.1%}")

        trader.close()

        print("\n" + "="*60)
        print("✅ 演示完成!")
        print("="*60)
        print("\n下一步:")
        print("1. 配置真实交易API密钥")
        print("2. 在 config.yaml 中设置 testnet: false")
        print("3. 配置飞书 Webhook 启用人工确认")
        print("4. 完善交易策略（认知模块）")

    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断")
    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
