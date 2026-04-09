# Trading Operations

For architecture, deployment, runtime, testing, and handoff details, read `DEVELOPMENT_MANUAL.md` first.

Use the helper scripts under `tools/` when you want to trigger trading workflows on demand from the remote host.

These helpers run the existing OpenClaw cron jobs in `isolated` mode, so they reuse the same production payloads without relying on any long-lived trading conversation.

Available commands:

- `tools\run_trading_review_now.cmd`
  Runs both production review lanes once: crypto first, then China A-shares.
- `tools\run_crypto_review_now.cmd`
  Runs the crypto review lane only using the same payload as `trading-crypto-5min`.
- `tools\run_cn_equity_review_now.cmd`
  Runs the China A-share review lane only using the same payload as `trading-cn-equity-5min`.
- `tools\refresh_macro_now.cmd`
  Refreshes macro state once without placing any orders.
- `tools\run_discover_assets_now.cmd`
  Runs both discovery lanes once: crypto first, then China A-shares.
- `tools\run_discover_crypto_assets_now.cmd`
  Runs the crypto discovery lane only.
- `tools\run_discover_cn_equity_assets_now.cmd`
  Runs the China A-share discovery lane only.
- `tools\run_strategy_optimize_now.cmd`
  Runs both optimization lanes once: crypto first, then China A-shares.
- `tools\run_strategy_optimize_crypto_now.cmd`
  Runs the crypto optimization lane only.
- `tools\run_strategy_optimize_cn_equity_now.cmd`
  Runs the China A-share optimization lane only.
- `tools\run_crypto_testnet_e2e_now.cmd`
  Runs one crypto testnet direct-execution E2E validation through the current bridge path.

Current live strategy assignment:

- `BTC/USDT -> combined`
- `ETH/USDT -> combined`
- `510300 / 159915 / 300308 / 300502 / 603986 -> dynamic`

Current futures status (updated 2026-04-08):

- **Crypto now uses `ccxt_futures` (Binance futures testnet, USDT-M perpetual swap)**
- `market_modes.crypto = "ccxt_futures"` (switched from spot `ccxt`)
- Futures testnet uses URL override + load_markets monkey-patch (not deprecated sandbox mode)
- `market_type="swap"` is set automatically when executor name is `ccxt_futures`
- Futures testnet verified: balance 5000 USDT, BTC/USDT:USDT and ETH/USDT:USDT tickers working
- Futures orders require explicit `leverage` plus `stop_price` or `estimated_loss`
- Futures are not in `auto_execution_markets` by default

Current A-share execution notes:

- Preferred no-GUI simulation path is the MyQuant `gm.api` / `gmtrade` runtime exposed through `backtest_strategy`, `start_strategy_simulation`, and related bridge actions.
- GM SDK runs on dedicated Python 3.11 runtime: `C:\Users\Roy\.openclaw\runtime\gm311\Scripts\python.exe`
- GM Token: configured via env var `GM_TOKEN` / `GMTRADE_TOKEN`
- Account ID: `e1255940-325a-11f1-ab23-00163e022aa6`
- Use the real MyQuant UUID `account_id` for account-state checks. Friendly aliases can still leave `get_cash`, `get_position`, and `context.accounts` empty.
- `SHSE.511990` is the current known-good same-session buy+sell validation symbol on the simulation account.
- `SHSE.510300` buy fills are valid, but immediate same-session sell may be blocked by `available_now=0` depending on account and venue rules.
- A-share data sources: Sina/Tencent realtime (primary, stable) → akshare → yfinance fallback
- 东方财富 API 被网络封锁，不可用

Current operator channel:

- Feishu has been removed from the active trading runtime.
- Operator notifications and chat control now go through Discord only.
- Bot: `openclaw-bot#2977`
- Bot Token & Channel ID: 见 `SECRETS_BACKUP.md`（环境变量 `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`）
- Bridge: `discord_agent_bridge.py` with auto-restart daemon
- Proxy: `127.0.0.1:7897`

Current strategy performance (as of 2026-04-08):

| Strategy | Total Return | Win Rate | Weight |
|----------|-------------|----------|--------|
| breakout | +3432% | 67% | 88.8% (main) |
| mean_reversion | +62.9% | 80% | 11.2% |
| trend_following | -13.2% | 33% | 0% (eliminated) |

Kernel modules (extracted 2026-04-09):

- `kernel/decision.py` — `OrderIntent` dataclass + `make_decision()` pure function
- `kernel/risk.py` — `RiskResult` dataclass + `check_risk()` pure function
- `kernel/order_log.py` — Idempotent order logger (dedup by intent_hash)
- `kernel/indicators.py` — Pure indicator functions
- `kernel/strategy/` — Strategy evaluation pure functions
- `tests/` — Unit tests for kernel modules (all passing)

## Infrastructure (updated 2026-04-08)

Mihomo proxy startup:

```cmd
schtasks /Run /TN "StartMihomo"
```

OpenClaw gateway startup:

```cmd
schtasks /Run /TN "OpenClaw Gateway"
```

Discord bridge startup:

```cmd
schtasks /Run /TN "DiscordBridge"
```

This starts `discord_bridge_daemon.py` which auto-restarts `discord_agent_bridge.py` on crash.

Split trading runners:

```cmd
schtasks /Run /TN "OpenClawTradingCrypto5MinRunner"
schtasks /Run /TN "OpenClawTradingCnEquity5MinRunner"
```

These high-frequency runners invoke the trading bridge directly:

```cmd
echo {"market":"crypto"} | py -3 C:\Users\Roy\.openclaw\workspace-trading\openclaw_trading_bridge.py run_market_review_once
echo {"market":"cn_equity"} | py -3 C:\Users\Roy\.openclaw\workspace-trading\openclaw_trading_bridge.py run_market_review_once
```

Split discovery runners:

```cmd
schtasks /Run /TN "OpenClawDiscoverCryptoAssetsHourlyRunner"
schtasks /Run /TN "OpenClawDiscoverCnEquityAssetsHourlyRunner"
```

Split optimizer runners:

```cmd
schtasks /Run /TN "OpenClawStrategyOptimizeCnEquityNightlyRunner"
schtasks /Run /TN "OpenClawStrategyOptimizeCryptoNightlyRunner"
```

If the remote task layout drifts, rebuild it with:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\Roy\.openclaw\tools\install_split_windows_tasks.ps1
```

Before restarting gateway, clear stale lock:

```powershell
Remove-Item "C:\Users\Roy\AppData\Local\Temp\openclaw\gateway*.lock" -Force -ErrorAction SilentlyContinue
```

Switch mihomo to Taiwan node:

```cmd
python C:\Users\Roy\switch_tw.py
```

Proxy config: `C:\Users\Roy\.config\clash\profiles\latest_sub.yml`
Mixed-port: `7897`

## Key Configuration (updated 2026-04-08)

### config.yaml structure

> 🔐 完整密钥值见本地 `SECRETS_BACKUP.md`（已加入 .gitignore，不会提交到 Git）

```yaml
execution:
  default: ccxt
  market_modes:
    crypto: ccxt_futures       # Futures testnet (USDT-M swap)
    cn_equity: gmtrade         # MyQuant simulation
    futures: ccxt_futures      # Futures testnet
  ccxt:
    exchange: binance
    testnet: true
    api_key: <见 SECRETS_BACKUP.md - Binance Spot Testnet>
    secret: <见 SECRETS_BACKUP.md - Binance Spot Testnet>
  futures:
    exchange: binance
    testnet: true
    api_key: <见 SECRETS_BACKUP.md - Binance Futures Testnet>
    secret: <见 SECRETS_BACKUP.md - Binance Futures Testnet>
    quote_asset: USDT
  gmtrade:
    token: <见 SECRETS_BACKUP.md - GM Token>
    account_id: e1255940-325a-11f1-ab23-00163e022aa6
    endpoint: api.myquant.cn:9000
    lot_size: 100
  gm_strategy:
    python_executable: C:/Users/Roy/.openclaw/runtime/gm311/Scripts/python.exe
    token: <见 SECRETS_BACKUP.md - GM Token>
    account_id: e1255940-325a-11f1-ab23-00163e022aa6
```

### Data source configuration

```yaml
sensory:
  sources:
    ccxt:
      exchange: binance
      testnet: false            # Mainnet prices for data
    akshare: {}
    yfinance: {}
```

A-share bridge provider chain: `["sina", "akshare", "yfinance"]`

## Troubleshooting Quick Reference

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `fetch_ticker` returns 0 prices | `sensory.sources.ccxt.testnet` is `true` or missing | Set `testnet: false` in sensory.sources.ccxt |
| `ECONNREFUSED 127.0.0.1:7897` | Mihomo not running | `schtasks /Run /TN StartMihomo` |
| `gateway already running` | Stale lock file | Delete `gateway*.lock` in temp dir |
| `JavaScript heap out of memory` | Another process eating RAM | Check `tasklist` for bloated processes |
| mihomo `Parse config error: GeoSite` | `fallback-filter` contains `geosite` key | Remove `geosite` from fallback-filter |
| `MODULE_NOT_FOUND openclaw.mjs` | Stale startup batch file | Use `gateway.cmd` instead of old bat file |
| Discord bot not responding | Bridge process died | `schtasks /Run /TN "DiscordBridge"`, check `logs/discord_bridge.log` |
| `NotSupported: binance does not have a testnet/sandbox URL for dapiPublic` | Using raw CCXT without URL override | Use `CCXTExecutionProvider(testnet=True, market_type="swap")` which applies the monkey-patch |
| `AuthenticationError: binance requires "secret" credential` | Parameter name mismatch | Use `secret=` not `api_secret=` in CCXTExecutionProvider constructor |
| GM `ImportError: No module named 'gm'` | Using system Python 3.12 | Use `gm311` runtime: `C:\Users\Roy\.openclaw\runtime\gm311\Scripts\python.exe` |
| `UnicodeEncodeError` in notifications | Surrogate characters in data | `to_jsonable()` sanitizer should handle most cases |
| `load_markets` returns 0 markets | Proxy blocking testnet URLs | `_should_bypass_proxy` now defaults to True for binance+testnet (fixed 2026-04-08) |
| `market_type == "future"` doesn't match | CCXT uses "swap" not "future" | All comparisons now use `market_type in ("future", "swap")` (fixed 2026-04-08) |
| `fetch_balance` returns spot balance on swap | Old comparison `== "future"` misses "swap" | Fixed: `market_type in ("future", "swap")` (fixed 2026-04-08) |

Why this exists:

- Scheduled runs already use `sessionTarget: isolated` and are stable.
- The long-lived interactive session can accumulate too much context over time.
- These helpers provide a safe manual entry point that follows the same isolated path as the scheduler.
