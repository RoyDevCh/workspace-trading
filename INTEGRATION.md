# OpenClaw 增强版交易代理 - 集成文档

## 项目概述

基于 CryptoClaw 和 TradingView-Claw 的功能，增强了 OpenClaw 交易代理的能力。

## 已集成的功能

### 1. 多源数据 (sensory 模块)

| 数据源 | 功能 | 说明 |
|--------|------|------|
| **TradingView** | 实时行情、技术指标 | 通过 session cookie 访问，RSI/MACD/布林带 |
| **CCXT** | 加密货币历史/实时数据 | 支持 100+ 交易所 |
| **Yahoo Finance** | 美股/ETF 数据 | 免费，支持全球市场 |
| **Tushare** | A股数据 | 需 token，专业级 |
| **AKShare** | A股数据 | 免费，无需 token |
| **Binance Market Rank** | 市场排名 | 热门代币、智能资金流入、Alpha 项目 |
| **Smart Money Signals** | 链上信号 | BSC/Solana 聪明钱买卖信号 |

### 2. 技术指标库 (indicators.py)

扩展了 15+ 个技术指标：

```python
from sensory import (
    compute_rsi,           # 相对强弱指数
    compute_macd,          # MACD
    compute_bollinger,     # 布林带
    compute_ma,            # 移动平均线 (MA5, MA10, MA20, MA50, MA200)
    compute_ema,           # 指数移动平均
    compute_stochastic,    # 随机指标 (KDJ)
    compute_atr,           # 平均真实波幅
    compute_volume_profile,# 成交量分布
    detect_candlestick_patterns,  # 蜡烛图形态识别
    compute_all_indicators,# 一键计算所有指标
)
```

### 3. LLM 信号引擎 (signal_engine.py)

使用 LLM (OpenRouter 或本地 Ollama) 进行技术分析：

```python
from sensory import SignalEngine

engine = SignalEngine(
    api_key="sk-or-...",           # OpenRouter API Key
    model="openai/gpt-4o-mini",   # 或 "anthropic/claude-3-haiku"
    use_local=False                # True 使用本地 Ollama
)

signal = engine.analyze("BTC/USDT", df)
# 返回: signal_type, score(0-100), entry, target, stop, reason
```

**信号等级系统：**
- S1 (≥95分): 极强信号
- S2 (≥90分): 强信号
- S3 (≥85分): 中等信号
- S4 (≥80分): 弱信号
- S5 (<80分): 不推荐

### 4. 多策略支持

| 策略 | 描述 | 所需配置 |
|------|------|----------|
| `ma_cross` | MA5/MA20 交叉 | 无 |
| `llm_signal` | LLM 技术分析 | `OPENROUTER_API_KEY` |
| `smart_money` | Smart Money 跟单 | 无（链上数据） |
| `multi` | 多策略融合 | 子策略配置 |

### 5. 增强命令集

```bash
# 查看账户状态
python trading_agent.py --status

# 单次交易 (MA策略)
python trading_agent.py --once --symbol BTC/USDT

# 使用 LLM 信号
python trading_agent.py --strategy llm_signal --once --symbol BTC/USDT --llm-api-key YOUR_KEY

# 多策略融合
python trading_agent.py --strategy multi --once --symbol BTC/USDT

# 扫描市场信号 (LLM分析多个币种)
python trading_agent.py --scan --symbols BTC/USDT,ETH/USDT,SOL/USDT --min-score 85

# 查看 Binance 热门代币
python trading_agent.py --trending --limit 20

# 查看 Smart Money 链上信号
python trading_agent.py --smart-money --chain bsc  # 或 --chain solana

# 连续交易模式
python trading_agent.py --symbol BTC/USDT --interval 300
```

## 环境配置

### 必需依赖

```bash
pip install pandas numpy ccxt easytrader flask pyyaml httpx
pip install pandas-ta  # 技术指标
```

### 可选配置

**1. OpenRouter API Key (LLM 信号策略)**

```bash
export OPENROUTER_API_KEY="sk-or-..."
```

或在命令行：
```bash
python trading_agent.py --llm-api-key sk-or-... --strategy llm_signal
```

**2. TradingView Session (TradingView 数据源)**

```bash
export TRADINGVIEW_SESSION="your_sessionid"
```

获取方法：浏览器登录 TradingView → F12 → Application → Cookies → sessionid

**3. 交易所 API Key (实盘交易)**

```bash
export BINANCE_API_KEY="..."
export BINANCE_SECRET="..."
```

**4. 飞书 Webhook (风控闸门)**

编辑 `gatekeeper/config.yaml`：
```yaml
feishu:
  webhook: "https://open.feishu.cn/open-apis/bot/v2/hook/your-token"
```

## 策略详解

### MA 交叉策略 (ma_cross)

传统技术分析：
- 金叉 (MA5 > MA20 且前一日 MA5 ≤ MA20) → 买入
- 死叉 (MA5 < MA20 且前一日 MA5 ≥ MA20) → 卖出
- 置信度: 60%

### LLM 信号策略 (llm_signal)

AI 技术分析：
- 分析 OHLCV + 10+ 项技术指标
- 返回信号类型、分数(0-100)、入场/目标/止损价
- 置信度 = score / 100
- 需配置 OPENROUTER_API_KEY

**提示词包含：**
- 价格位置（相对高低点）
- RSI 超买超卖
- MACD 金叉死叉
- 布林带上下轨突破
- 成交量配合
- 蜡烛图形态

### Smart Money 策略 (smart_money)

链上数据跟单：
- 监控 BSC/Solana 聪明钱钱包活动
- 检测专业投资者买入/卖出信号
- 置信度基于智能钱数量：`0.5 + count * 0.1`
- 根据退出率调整置信度

**数据来源：** Binance Web3 Smart Money Signals API

### 多策略融合 (multi)

加权投票机制：
- MA Cross: 20%
- LLM Signal: 40%
- Smart Money: 40%

决策规则：
- 买入权重 > 卖出权重 且 ≥ 50% 总权重 → 执行
- 否则观望

## 信号工作流

```
[数据获取] → [技术指标计算] → [策略生成信号] → [风控闸门审批] → [执行]
     ↓              ↓                  ↓                ↓
  CCXT/YF/TV   RSI/MACD/BB/MA    MA/LLM/Smart   Feishu Card   Binance
```

## 目录结构

```
~/.openclaw/
├── config.yaml                    # 主配置
├── openclaw.json                  # 代理注册
├── sensory/                       # 感官模块
│   ├── __init__.py
│   ├── data_provider.py          # 数据源 (7个Provider)
│   ├── indicators.py              # 技术指标库
│   ├── signal_engine.py          # LLM信号引擎
│   └── config.yaml
├── execution/                     # 执行模块
│   ├── execution.py               # CCXT/EasyTrader
│   └── config.yaml
├── gatekeeper/                    # 风控闸门
│   ├── gatekeeper.py              # 飞书审批 + HTTP服务器
│   └── config.yaml                # 需配置 webhook
├── workspace-trading/             # 交易代理工作区
│   ├── trading_agent.py           # 主程序
│   ├── logs/trading.log           # 运行日志
│   ├── SOUL.md
│   └── IDENTITY.md
└── agents/trading/agent/          # 代理元数据
    ├── auth-profiles.json
    └── models.json
```

## 使用示例

### 场景 1: 查看市场状态

```bash
cd ~/.openclaw/workspace-trading
python trading_agent.py --status
```

输出：
```
============================================================
[Trading Account Status]
============================================================
Total Asset: $0.00
Cash:        $0.00
Market Val:  $0.00
P&L:         $0.00

No open positions
```

### 场景 2: 运行单次交易 (MA策略)

```bash
python trading_agent.py --once --symbol BTC/USDT --quantity 0.001
```

输出：
```
2026-04-03 - INFO - === Trading Cycle: BTC/USDT ===
2026-04-03 - INFO -   Fetching BTC/USDT data...
2026-04-03 - INFO -   Signal: BUY (confidence: 60.0%)
2026-04-03 - INFO -   Reason: Golden cross: MA5(85200.50) > MA20(84600.30)
2026-04-03 - INFO -   Requesting approval...
2026-04-03 - INFO -   Approved
2026-04-03 - INFO -   Executing order...
2026-04-03 - INFO -   Order ord_123: filled
```

### 场景 3: LLM 信号交易

```bash
export OPENROUTER_API_KEY="sk-or-..."
python trading_agent.py --strategy llm_signal --once --symbol ETH/USDT
```

### 场景 4: 扫描市场

```bash
python trading_agent.py --scan --symbols BTC/USDT,ETH/USDT,SOL/USDT --min-score 90
```

输出：
```
=== 扫描结果 (2 个信号) ===

BTC/USDT: LONG (分数: 93.5, 等级: S2)
  入场: $84500.00, 目标: $89000.00, 止损: $81000.00
  原因: RSI超卖回升，MACD金叉，价格测试下轨支撑

ETH/USDT: SHORT (分数: 91.2, 等级: S2)
  入场: $3250.00, 目标: $3000.00, 止损: $3400.00
  原因: 价格阻力位回落，RSI超买，成交量萎缩
```

### 场景 5: 查看 Smart Money 信号

```bash
python trading_agent.py --smart-money --chain bsc --limit 10
```

输出：
```
=== Smart Money 信号 (BSC) ===
  🟢 BNB: buy
    智能钱数量: 15, 最高涨幅: 12.5%
    当前价: $580.20, 触发价: $575.00
    退出率: 5.0%, 状态: active
  🔴 CAKE: sell
    智能钱数量: 8, 最高涨幅: -3.2%
    当前价: $2.45, 触发价: $2.50
    退出率: 45.0%, 状态: active
```

## 配置示例

### config.yaml

```yaml
sensory:
  default_source: "ccxt"          # 默认数据源
  symbols:                        # 监控列表
    - "BTC/USDT"
    - "ETH/USDT"
    - "SOL/USDT"

execution:
  default: "ccxt"
  ccxt:
    testnet: true                 # 测试网模式
    exchange: "binance"

gatekeeper:
  enabled: true
  manual_approval: true           # 强制人工审批
  feishu:
    webhook: "https://open.feishu.cn/..."

strategy:
  type: "multi"                   # 多策略融合
  enable_ma_cross: true
  enable_llm_signal: true
  enable_smart_money: true
  weights:
    ma_cross: 0.2
    llm_signal: 0.4
    smart_money: 0.4
  llm_api_key: "${OPENROUTER_API_KEY}"
  llm_model: "openai/gpt-4o-mini"
  chain_id: 56                    # BSC
```

## 注意事项

1. **网络环境**：某些 API（TradingView, Binance Web3）可能被防火墙限制，建议使用代理
2. **API 限流**：OpenRouter 免费 tier 有速率限制，注意控制扫描频率
3. **资金安全**：始终在测试网验证，实盘前充分回测
4. **审批必开**：建议始终启用 gatekeeper 人工审批
5. **仓位管理**：置信度会动态调整仓位，避免过度杠杆

## 后续优化方向

- [ ] 添加更多链（Ethereum, Arbitrum, Polygon）
- [ ] 集成 DeFi DEX 执行（Uniswap, PancakeSwap）
- [ ] 支持自定义 Pine Script 策略转换
- [ ] 添加回测引擎
- [ ] 集成更多 LLM 提供商（Claude, Gemini, 本地模型）
- [ ] 添加风险管理模块（最大回撤止损、波动率调整）
- [ ] 支持多时间框架确认（1h + 4h + 1d 共识）

---

**集成完成时间**: 2026-04-03
**集成的项目**: [CryptoClaw](https://github.com/TermiX-official/cryptoclaw) + [TradingView-Claw](https://gitcode.com/gh_mirrors/tr/TradingView-Claw)
