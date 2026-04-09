# OpenClaw Trading System

> 自动化交易系统 — A股(GM模拟盘) + 加密货币(Binance合约测试网)

## 架构概览

```
~/.openclaw/
├── workspace-trading/           ← 本仓库：OpenClaw Agent workspace + 交易源码
│   ├── openclaw_trading_bridge.py   主桥接器（5280行，待重构）
│   ├── execution/                   交易执行层
│   │   ├── __init__.py
│   │   └── execution.py            CCXT + GmTrade 执行器
│   ├── discord_agent_bridge.py      Discord 通知桥
│   ├── discord_bridge_daemon.py     Daemon 自动重启
│   ├── kernel/                      确定性交易内核（重构中）
│   │   ├── decision.py              OrderIntent + 决策引擎
│   │   ├── risk.py                  风控纯函数
│   │   └── strategy/                策略模块
│   ├── trading_rules.yaml           唯一交易规则文件
│   ├── config/                      配置文件（不在 Git 中）
│   ├── runtime/                     运行产物（不在 Git 中）
│   └── docs/                        文档
│
├── execution/                   ← 远程执行模块（~/.openclaw/execution/）
├── gatekeeper/                  ← 审批模块（~/.openclaw/gatekeeper/）
├── sensory/                     ← 数据模块（~/.openclaw/sensory/）
├── config.yaml                  ← 全局配置（API keys 等，不在 Git 中）
└── openclaw.json                ← OpenClaw 平台配置（不在 Git 中）
```

## 核心原则

1. **确定性下单内核**：交易决策的输入只能是行情快照 + 持仓状态 + 机器可读规则
2. **Agent 只在建议层**：OpenClaw agent 负责监控、解释、汇报，不直接下单
3. **单一实现**：任何功能只存在一份实现，杜绝 `workspace_*` 前缀的重复文件

## 运行

```bash
# 一次性交易审查
python openclaw_trading_bridge.py run_market_review_once

# Discord bridge daemon
launch_bridge.bat
```

## 数据源链

| 市场 | 主数据源 | 备选1 | 备选2 |
|------|---------|-------|-------|
| A股 | Sina实时 | AKShare | yFinance |
| 加密货币 | CCXT (Binance) | - | - |
| A股交易 | GM模拟盘 | - | - |
| 合约交易 | CCXT Binance Testnet | - | - |

## 安全

- 所有密钥存储在 `SECRETS_BACKUP.md`（已加入 `.gitignore`）
- 代码从环境变量读取密钥，不硬编码
- 详见 `DEVELOPMENT_MANUAL.md` §8.1

## 文档

- [开发手册](DEVELOPMENT_MANUAL.md)
- [运维手册](OPERATIONS.md)
- [架构重构方案](docs/ARCHITECTURE_REFACTOR_PLAN.md)

## 状态

⚠️ 重构中 — 当前处于 Phase 1（确定性边界提取）阶段。详见重构方案。
