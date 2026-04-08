# USER.md - About Your Human

- **Name:** Roy
- **What to call them:** Roy
- **Timezone:** Asia/Shanghai (UTC+8)
- **Language:** Chinese (primary), English (technical)

## Important: How To Answer Questions

When Roy asks about trades, holdings, or portfolio:
1. **ALWAYS call get_trading_state or get_portfolio_snapshot FIRST**
2. NEVER answer from memory alone -- always verify with live data
3. NEVER say "no trades" without actually querying the system
4. Include symbol, quantity, price, and reason when discussing trades

### Tools for common questions
| Question | Tool to use |
|----------|------------|
| Current holdings/balance | `get_portfolio_snapshot` |
| Recent trade history | `get_trading_state` (recent_events) |
| Full decision log | Read `logs/trading_decisions.jsonl` |
| Current strategy/rules | `get_trading_state` (rules section) |
| Macro regime | `get_trading_state` (macro_state) |

## Context
- Roy is the developer of this trading system
- Trading runs on Binance testnet (not real money), but prices are from mainnet
- System auto-trades every 5 minutes with MA crossover + RSI strategy
- Roy communicates via Feishu (飞书)
