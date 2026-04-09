# OpenClaw Trading System Development Manual

Last updated: `2026-04-09` (kernel refactor)

Bug fix history is in [CHANGELOG.md](CHANGELOG.md).

## 1. Scope

This document is the handoff and continuation manual for the OpenClaw split-lane trading system.

It covers:

- current architecture
- local source-of-truth paths
- remote deployment paths
- scheduler and desktop-bridge runtime
- validation workflow
- known blockers and safe continuation points

This manual intentionally does **not** store API secrets or broker credentials.

## 2. Current System Status

As of `2026-04-09`, the system is running:

- **Crypto review loop**: Working, auto-trading every 5 min on Binance **futures testnet** (USDT-M perpetual swap)
- **Crypto strategy**: Combined MA5/MA20 crossover + RSI 30/70, auto-approve enabled
- **Price data**: Binance mainnet (real prices) via `sensory/sources.ccxt.testnet: false`
- **Execution**: Binance futures testnet (test orders, not real money), `market_type="swap"`
- **A-share execution**: MyQuant `gm.api` route working (fixed 2026-04-09), simulation account 1,000,000 CNY
- **GM Token**: configured via env var `GM_TOKEN` / `GMTRADE_TOKEN`, Account ID: `e1255940-325a-11f1-ab23-00163e022aa6`
- **Notification**: Discord only, using `openclaw-bot#2977` via `discord_agent_bridge.py`
- **Discord bridge v2**: Exponential backoff reconnect, heartbeat ACK timeout detection, SSL error tolerance — 100% heartbeat ACK rate
- **Discord daemon**: Auto-restart via `discord_bridge_daemon.py`, Windows scheduled task `DiscordBridge` runs at logon
- **Macro refresh**: Every 15 min
- **Strategy optimization**: Nightly at 02:00
- **Asset discovery**: Hourly, auto-adds/removes symbols based on volume/volatility
- **Data sources**: A-share uses Sina/Tencent realtime API (primary) → akshare → yfinance fallback

### Current A-share status

- A-share execution uses `gm.api` package (NOT `gmtrade` — `gmtrade` is not installed)
- **GmTradeProvider compatibility** (fixed 2026-04-09):
  - `connect()` now falls back from `gmtrade.api` to `gm.api` if `gmtrade` is unavailable
  - `gm.api.set_account_id()` replaces `gm.account()` + `gm.login()`
  - `gm.api.get_cash(account_id=string)` — parameter is `account_id`, not `account`
  - `gm.api.get_position()` — singular, not `get_positions`
  - `gm.api.order_cancel_all()` — no `order_cancel(cl_ord_id=...)` available
  - `gm.api.history()` for K-line data — time format "2026-03-01 09:30:00" (NO timezone suffix)
  - Returns `DictLikeObject` (dict-like) instead of list for `get_cash`
- GM SDK available on BOTH runtimes:
  - `C:\Users\Roy\.openclaw\runtime\gm311\Scripts\python.exe` (Python 3.11, original)
  - `C:\Users\Roy\AppData\Local\Programs\Python\Python312\python.exe` (Python 3.12, `gm` installed 2026-04-09)
- GM Token is configured in both `execution.gmtrade.token` and `execution.gm_strategy.token`
- MyQuant account queries require the real UUID `account_id`; friendly aliases do not populate `get_cash()`, `get_position()`, or `context.accounts`
- GM simulation account: NAV 1,000,000 CNY, no positions, no trades yet

### Current crypto/futures status

- **Crypto has switched from spot testnet to futures testnet** as of 2026-04-08
- `market_modes.crypto = "ccxt_futures"` (was `"ccxt"` for spot)
- Binance futures testnet uses URL override + `load_markets` monkey-patch (not deprecated `sandbox` mode)
- `market_type="swap"` is set automatically when executor name is `ccxt_futures`
- Futures testnet balance: ~5000 USDT, verified working
- BTC/USDT:USDT and ETH/USDT:USDT tickers confirmed working

## 3. Source Of Truth

### 3.1 Local backup repo

The local backup at `C:\Users\Roy\.openclaw\workspace-trading\` contains:

- `openclaw_trading_bridge.py` — Main trading bridge (30+ actions, calls kernel modules)
- `execution/execution.py` — Execution layer (CCXTExecutionProvider, GmTradeProvider, ExecutionManager)
- `execution/__init__.py` — Package init
- `kernel/` — **Trading kernel (pure functions, no I/O)** — extracted 2026-04-09
  - `kernel/__init__.py` — Package init
  - `kernel/decision.py` — `OrderIntent` dataclass + `make_decision()` pure function
  - `kernel/risk.py` — `RiskResult` dataclass + `check_risk()` pure function
  - `kernel/order_log.py` — Idempotent order logger (dedup by intent hash)
  - `kernel/indicators.py` — Pure indicator functions (EMA, RSI, MACD, Bollinger)
  - `kernel/strategy/` — Strategy evaluation (pure functions)
    - `registry.py` — `get_strategy()` dispatcher + `evaluate_trend_following_signal()` + `evaluate_mean_reversion_signal()` + `evaluate_combined_signal()`
    - `trend_following.py` — Trend-following signal evaluation
    - `mean_reversion.py` — Mean-reversion signal evaluation
    - `combined.py` — Combined signal evaluation
- `tests/` — Unit tests for kernel modules
  - `test_decision.py` — OrderIntent + make_decision tests
  - `test_risk.py` — Risk check tests (12 cases)
  - `test_strategy.py` — Strategy evaluation tests (9 cases)
- `config.yaml` — System configuration (redact secrets before sharing)
- `sensory_data_provider.py` — Data provider with SinaRealtimeProvider
- `discord_agent_bridge.py` — Discord bot bridge
- `discord_bridge_daemon.py` — Auto-restart daemon wrapper
- `launch_bridge.bat` — Discord bridge launcher
- `gatekeeper.py` — Approval state machine
- `jobs.json` — Cron job definitions
- `gm_strategy_runtime.py` — MyQuant scripted runtime
- `templates/` — GM strategy templates
- `DEVELOPMENT_MANUAL.md` — This document
- `OPERATIONS.md` — Operations runbook
- `SOUL.md`, `TOOLS.md`, `USER.md` — Agent behavior docs
- `trading_rules.yaml` — Trading rules configuration
- `training_examples.json` — Few-shot behavioral examples
- `schedule.yaml` — Schedule definitions

### 3.2 Remote deployed paths

The live remote deployment target is the Windows host (`10.83.120.248`, SSH port 2222):

| Remote Path | Purpose |
|-------------|---------|
| `C:\Users\Roy\.openclaw\openclaw_trading_bridge.py` | Main bridge (also in workspace-trading) |
| `C:\Users\Roy\.openclaw\execution\execution.py` | Execution providers |
| `C:\Users\Roy\.openclaw\sensory\data_provider.py` | Data provider |
| `C:\Users\Roy\.openclaw\config.yaml` | System config |
| `C:\Users\Roy\.openclaw\cron\jobs.json` | Cron definitions |
| `C:\Users\Roy\.openclaw\gatekeeper\gatekeeper.py` | Gatekeeper |
| `C:\Users\Roy\.openclaw\workspace\discord_agent_bridge.py` | Discord bridge |
| `C:\Users\Roy\.openclaw\workspace\discord_bridge_daemon.py` | Discord auto-restart daemon |
| `C:\Users\Roy\.openclaw\workspace\launch_bridge.bat` | Bridge launcher |
| `C:\Users\Roy\.openclaw\workspace-trading\kernel\` | Trading kernel (pure functions) |
| `C:\Users\Roy\.openclaw\workspace-trading\tests\` | Kernel unit tests |
| `C:\Users\Roy\.openclaw\workspace-trading\*` | Workspace trading files |
| `C:\Users\Roy\.openclaw\runtime\gm311\` | GM SDK Python 3.11 runtime |
| `C:\Users\Roy\.openclaw\logs\` | Runtime logs |

If local and remote files diverge, the remote running copy is the live truth. Sync local backup from remote when needed.

## 4. Architecture

### 4.1 High-level flow

`OpenClaw plugin -> Python trading bridge -> kernel (pure decisions) / execution (I/O) -> runtime logs and state`

More concretely:

1. `plugin.js` exposes tools to OpenClaw.
2. OpenClaw calls the tool.
3. `openclaw_trading_bridge.py` dispatches the action.
4. The bridge reads config and rules.
5. Data comes from `SinaRealtimeProvider` (primary for A-share), `ccxt` (crypto), `akshare`/`yfinance` (fallback).
6. **Signal generation and risk checks use `kernel/` pure functions** (no I/O, no side effects).
7. `kernel/decision.py` produces an `OrderIntent` with deterministic hash for idempotency.
8. `kernel/risk.py` evaluates risk limits and returns `RiskResult(allowed, reasons)`.
9. `kernel/order_log.py` deduplicates orders by intent hash.
10. Real orders go through gatekeeper approval.
11. Execution is routed by market:
   - `crypto -> ccxt_futures / Binance futures testnet (USDT-M perpetual swap)`
   - `cn_equity -> gmtrade / MyQuant simulation`
   - `futures -> ccxt_futures / Binance futures testnet`
12. Decisions and runtime state are persisted to log/state files.
13. Notifications go to Discord via `discord_agent_bridge.py`.

### 4.2 Kernel architecture (pure functions)

The `kernel/` package is the deterministic core of the trading system. All functions are **pure** (no I/O, no global state mutation, no network calls). This makes them:

- **Unit-testable** without mocks
- **Replayable** — same inputs always produce same outputs
- **Composable** — strategies compose via `evaluate_combined_signal()`

Key data classes:

```python
@dataclass
class OrderIntent:
    symbol: str
    side: str        # "buy" | "sell"
    order_type: str  # "market" | "limit"
    quantity: float
    price: Optional[float] = None
    stop_price: Optional[float] = None
    leverage: int = 1
    strategy: str = ""
    signal_strength: float = 0.0
    intent_hash: str = ""  # Deterministic SHA-256 for idempotency

@dataclass
class RiskResult:
    allowed: bool
    reasons: List[str]
    estimated_loss: Optional[float] = None
    position_value: Optional[float] = None
    loss_pct: Optional[float] = None
```

Decision flow:

```
market data → indicators (kernel/indicators.py)
           → strategy evaluate (kernel/strategy/)
           → OrderIntent (kernel/decision.py)
           → risk check (kernel/risk.py)
           → order log dedup (kernel/order_log.py)
           → execution (execution.py, I/O layer)
```

### 4.3 OpenClaw knowledge layer

OpenClaw is guided by:

- `SOUL.md` — personality and strategy rules
- `TOOLS.md` — how to use each tool correctly
- `USER.md` — user profile and communication preferences
- `memory/YYYY-MM-DD.md` — daily notes (methodology, not data)
- `training_examples.json` — few-shot behavioral examples

**Important**: When adding new tools or changing behavior, update BOTH the code AND these docs. Otherwise the agent won't use tools correctly.

### 4.4 Scheduler model

Trading jobs use `sessionTarget: isolated`.

This is intentional — long-lived sessions accumulate too much context. Isolated job turns are more stable and deterministic.

Do **not** casually switch trading jobs back to long-lived interactive sessions.

### 4.5 Data flow for trade queries

```
User asks "what trades happened"
  -> Agent reads memory/USER.md (knows to use tools, not guess)
  -> Calls get_trading_state (recent_events, last 20)
  -> Or reads trading_decisions.jsonl for full history
  -> Returns real data with symbol/qty/price/reason
```

### 4.6 Discord bridge architecture (v2)

```
Discord user sends message
  -> discord_agent_bridge.py (polls Discord via API)
  -> Forwards to OpenClaw gateway (port 18789)
  -> Gateway processes and returns response
  -> discord_agent_bridge.py sends response back to Discord
```

- Uses proxy `127.0.0.1:7897` to access Discord API
- `discord_bridge_daemon.py` wraps the bridge with auto-restart (exponential backoff, max 10 restarts/hr)
- Windows scheduled task `DiscordBridge` starts the daemon at logon (72h timeout)
- Heartbeat logged every 60 seconds with ACK timeout detection (60s)
- **v2 fixes (2026-04-09)**:
  - Exponential backoff reconnect (1s→2s→4s→8s→16s→30s max) instead of fixed 5s
  - Heartbeat ACK timeout detection (60s) — triggers intentional reconnect instead of waiting for SSL storm
  - SSL certificate verification bypass (`ssl_cert_reqs=ssl.CERT_NONE`)
  - GBK encoding fix for Windows log files
  - Separate daemon and bridge log files
  - Rate-limit restart detection (10 restarts/hr → sleep 5 min)
  - Daemon typo fix: `restart_restarts` → `recent_restarts`
- **v2 stability**: SSL errors no longer cause disconnects, 100% heartbeat ACK rate

## 5. Component Map

### 5.1 Plugin layer (`plugin.js`)

- Expose bridge actions as OpenClaw tools
- Inject workspace path and rules file
- Mirror proxy environment variables (HTTP_PROXY / http_proxy)

### 5.2 Kernel layer (`kernel/`) — Pure functions

The deterministic core. No I/O, no global state, no network calls.

- `kernel/decision.py` — `OrderIntent` dataclass + `make_decision()` pure function
  - Takes market data + strategy signal + risk result, produces an `OrderIntent`
  - `intent_hash` is SHA-256 of (symbol, side, quantity, price, stop_price, strategy, timestamp_minute)
  - Used for idempotent order logging (dedup by hash)
- `kernel/risk.py` — `RiskResult` dataclass + `check_risk()` pure function
  - Position size limits, daily budget, max loss, futures stop-price requirement
  - All parameters passed in explicitly (no global config reads)
- `kernel/indicators.py` — Pure indicator functions (EMA, RSI, MACD, Bollinger)
- `kernel/strategy/registry.py` — `get_strategy()` dispatcher + strategy evaluation functions
  - `evaluate_trend_following_signal()` — MA crossover + ADX + volume confirmation
  - `evaluate_mean_reversion_signal()` — RSI + Bollinger + Z-score
  - `evaluate_combined_signal()` — Weighted combination of trend + mean_reversion
- `kernel/order_log.py` — Idempotent order logger (JSONL, dedup by intent_hash)
- `tests/` — Unit tests for all kernel modules (test_decision, test_risk, test_strategy)

Design principles:

- **Pure functions only** — no side effects, no I/O, no global state
- **Explicit inputs** — all data passed as parameters, no hidden config reads
- **Deterministic** — same inputs always produce same outputs
- **Testable** — no mocks needed for unit tests

- Expose bridge actions as OpenClaw tools
- Inject workspace path and rules file
- Mirror proxy environment variables (HTTP_PROXY / http_proxy)

### 5.3 Bridge layer (`openclaw_trading_bridge.py`)

Key responsibilities:

- Unified tool entrypoint (30+ actions)
- Market-aware review universe that can be filtered into separate crypto and cn_equity lanes
- Per-symbol strategy resolution (combined, trend_following, mean_reversion)
- Indicator calculation (EMA, RSI, MACD, Bollinger)
- Signal generation and validation
- Risk checks (max loss, position size, daily budget)
- Order routing via approval/rejection/execution
- Decision logging and state persistence
- Operator notifications via Discord

Key design patterns:

- `to_jsonable()` — recursive sanitizer that cleans surrogate chars before any JSON serialization
- `save_state()` — persists runtime state through `to_jsonable()` for encoding safety
- `send_operator_notification()` sends directly to the configured Discord channel

### 5.4 Execution layer (`execution.py`)

- `CCXTExecutionProvider` — crypto spot + futures execution via ccxt
  - `__init__` params: `exchange_id`, `api_key`, `secret`, `testnet`, `market_type`, etc.
  - **Note**: parameter is `secret` not `api_secret`
  - `get_positions()`: uses `fetch_tickers()` for real-time prices (not hardcoded 0)
  - Supports both mainnet (prices) and testnet (execution) via config
  - For futures testnet: uses `_apply_binance_futures_testnet_urls()` + `load_markets` monkey-patch
  - `market_type="swap"` triggers futures-specific URL overrides

- `GmTradeProvider` — A-share execution via MyQuant `gm.api`
  - **IMPORTANT**: `gmtrade` package is NOT installed; provider uses `gm.api` (pip install gm) with compatibility layer
  - `connect()`: tries `gmtrade.api` first, falls back to `gm.api` with `set_account_id()` for authentication
  - `_call_with_optional_account()`: multi-fallback parameter passing (account→account_id→no param)
  - `get_positions()`: uses `gm.api.get_position()` (singular), returns list
  - `get_balance()`: handles `DictLikeObject` return from `gm.api.get_cash()`
  - `cancel_order()`: tries `order_cancel()` first, falls back to `order_cancel_all()`
  - Scripted execution path for backtest, simulation, and one-shot order validation
  - Requires the real UUID `account_id` for account state queries

- `ExecutionManager` — routes orders to the correct provider based on `market_modes` config

### 5.5 MyQuant scripted runtime

Files: `gm_strategy_runtime.py`, `templates/gm_ma_cross_template.py`, `templates/gm_one_shot_order_template.py`, `templates/gm_account_probe_template.py`

- Runs without GUI automation
- Stores rendered scripts, pid/state, reports, and logs under `runtime/gm_strategy/`
- Supports backtest, long-running simulation, account probe, and one-shot order submission
- Uses dedicated Python 3.11 runtime at `C:\Users\Roy\.openclaw\runtime\gm311\Scripts\python.exe`

### 5.6 Sensory layer (`data_provider.py`)

- `SinaRealtimeProvider` — Primary A-share data source (Sina/Tencent realtime API)
- `AkshareProvider` — Secondary A-share data source
- `YFinanceProvider` — Tertiary A-share fallback
- Bridge `cn_equity` provider chain: `["sina", "akshare", "yfinance"]`
- Crypto data still via ccxt (mainnet prices)

### 5.7 Approval layer (`gatekeeper.py`)

- Approval state machine (pending → approved/rejected/expired)
- Gatekeeper code remains available for future approval work, but manual approval is disabled in the current runtime
- Operator notifications now go to Discord
- Stale-approval recovery is disabled in the current runtime

### 5.8 Discord bridge v2 (`discord_agent_bridge.py` + `discord_bridge_daemon.py`)

- `discord_agent_bridge.py`: Main bridge process (~713 lines), connects to Discord via bot token
- `discord_bridge_daemon.py`: Auto-restart wrapper (~91 lines), monitors bridge process with exponential backoff
- `launch_bridge.bat`: Sets proxy env vars and starts daemon
- Proxy: `127.0.0.1:7897` (required for Discord API access from China)
- Bot identity: `openclaw-bot#2977`
- Heartbeat: logged every 60 seconds, ACK timeout detection at 60s
- Reconnect: exponential backoff 1s→30s max, rate-limited to 10 restarts/hr
- SSL: certificate verification bypassed, SSL EOF errors tolerated without disconnect
- Logs: separate files for daemon (`discord_daemon.log`) and bridge (`discord_bridge.log`)

## 6. Runtime State And Logs

Remote host runtime files:

| File | Purpose |
|------|---------|
| `runtime/trading_state.json` | Pause status, daily risk budget, recent events (last 20) |
| `runtime/macro_state.json` | Cross-asset regime, volatility, preferred market |
| `runtime/strategy_report.json` | Nightly optimization results |
| `logs/trading_decisions.jsonl` | Append-only full decision log |
| `runtime/trade_message_approvals.json` | Approval state machine |
| `runtime/gm_strategy/` | MyQuant rendered scripts, reports, state files, and simulation logs |
| `logs/discord_bridge.log` | Discord bridge heartbeat and event log |
| `logs/discord_daemon.log` | Discord daemon restart and monitoring log |
| `logs/openclaw_trading_bridge.log` | Bridge execution log |

## 7. Scheduler And Background Tasks

### 7.1 OpenClaw cron jobs

Defined in `cron/jobs.json`:

| Job | Schedule | Delivery | Agent |
|-----|----------|----------|-------|
| `trading-crypto-5min` | `*/5 * * * *` | none | trading-crypto |
| `trading-cn-equity-5min` | `*/5 * * * *` | none | trading-cn-equity |
| `macro-refresh-15min` | `*/15 * * * *` | none | trading |
| `discover-crypto-assets-hourly` | `0 * * * *` | none | trading-crypto |
| `discover-cn-equity-assets-hourly` | `0 * * * *` | none | trading-cn-equity |
| `strategy-optimize-crypto-nightly` | `5 2 * * *` | none | trading-crypto |
| `strategy-optimize-cn-equity-nightly` | `0 2 * * *` | none | trading-cn-equity |
| `daily-content` | `0 8 * * *` | announce | main-brain |
| `evening-content` | `0 18 * * *` | announce | main-brain |
| `nightly-review` | `30 23 * * *` | announce | main |

### 7.2 Windows scheduled tasks

| Task | Purpose |
|------|---------|
| `OpenClaw Gateway` | Starts gateway.cmd on port 18789 |
| `StartMihomo` | Starts mihomo proxy on port 7897 |
| `OpenClawTradingCrypto5MinRunner` | Runs crypto review every 5 minutes |
| `OpenClawTradingCnEquity5MinRunner` | Runs A-share review every 5 minutes, offset from crypto |
| `OpenClawDiscoverCryptoAssetsHourlyRunner` | Runs crypto discovery hourly |
| `OpenClawDiscoverCnEquityAssetsHourlyRunner` | Runs A-share discovery hourly |
| `OpenClawStrategyOptimizeCnEquityNightlyRunner` | Runs A-share nightly optimization |
| `OpenClawStrategyOptimizeCryptoNightlyRunner` | Runs crypto nightly optimization |
| `DiscordBridge` | Starts Discord bridge daemon at logon |

Legacy aggregate runners are kept disabled for rollback:

- `OpenClawTrading5MinRunner`
- `OpenClawDiscoverAssetsHourlyRunner`
- `OpenClawStrategyOptimizeNightlyRunner`

Provision or refresh the split task layout with:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\Roy\.openclaw\tools\install_split_windows_tasks.ps1
```

The two `*Trading*5MinRunner` tasks now call `openclaw_trading_bridge.py run_market_review_once` directly for lower latency.
Hourly discovery and nightly optimization still use the split OpenClaw cron jobs.

### 7.3 Infrastructure

| Service | Port | Config |
|---------|------|--------|
| OpenClaw gateway | 18789 | `gateway.cmd` |
| Mihomo proxy | 7897 | `latest_sub.yml`, Taiwan nodes |
| Mihomo API | 9090 | Switch nodes via REST API |

## 8. Environment Rules

### 8.1 Secrets

Do not write secrets into repo files (Binance API keys, webhook secrets, broker credentials).

**密钥备份文件**: `SECRETS_BACKUP.md`（本地存储所有密钥原文，已加入 `.gitignore`，不会提交到 Git）

Secrets are stored in:
- `C:\Users\Roy\.openclaw\config.yaml` — All execution API keys (DO NOT commit)
- `C:\Users\Roy\.openclaw\.env` — Legacy environment variables (spot testnet keys)
- `C:\Users\Roy\.openclaw\openclaw.json` — Discord bot token, OpenRouter API key, Feishu token

代码中的密钥引用方式：
- `discord_agent_bridge.py`: 从 `DISCORD_BOT_TOKEN` 和 `DISCORD_CHANNEL_ID` 环境变量读取
- `execution.py`: API key/secret 从 `config.yaml` 读取
- GM Token: 通过 `GM_TOKEN` / `GMTRADE_TOKEN` 环境变量或 `config.yaml` 的 `execution.gmtrade.token` 读取

⚠️ **安全提醒**: 所有密钥曾暴露于 GitHub 历史（已于 2026-04-09 清理），建议轮换所有 API Key。

### 8.2 Proxies

- Crypto market data: uses proxy (`HTTP_PROXY=http://127.0.0.1:7897`)
- Binance testnet execution: biased toward direct connection
- Discord API: requires proxy (`127.0.0.1:7897`)

### 8.3 Python environments

| Runtime | Use Case |
|---------|----------|
| `C:\Users\Roy\AppData\Local\Programs\Python\Python312\python.exe` | System Python, general scripts, bridge, GmTradeProvider |
| `C:\Users\Roy\.openclaw\runtime\gm311\Scripts\python.exe` | MyQuant / `gm.api` scripted runtime (Python 3.11) |

**IMPORTANT**: The system Python 3.12 now has `gm` module installed (as of 2026-04-09). Both runtimes can execute GM operations. Use `gm311` for dedicated MyQuant scripted simulations, and Python 3.12 for bridge-integrated GmTradeProvider calls.

## 9. Safe Development Workflow

1. **Edit locally or directly on remote** — both approaches are valid
2. **Validate locally** — `py -3 -m py_compile <file>`
3. **Deploy to remote** — `scp -P 2222 <local> roy@10.83.120.248:<remote>`
4. **Restart only what's needed**:
   - Plugin/bridge code: no restart needed (next invocation picks up changes)
   - MyQuant scripted runtime: stop and relaunch the specific simulation if `gm_strategy_runtime.py` or a template changed
   - OpenClaw config: restart gateway via `schtasks /Run /TN "OpenClaw Gateway"`
   - Discord bridge: restart via `schtasks /Run /TN "DiscordBridge"`
5. **Sync local backup** — After remote changes, `scp -P 2222 roy@10.83.120.248:<remote> <local>`

## 10. Validation Playbook

### 10.1 Crypto/futures validation layers

1. `get_market_data` — confirm price data flows
2. `calculate_indicator` — verify indicators
3. `generate_signal` — verify signal logic
4. `check_risk` — verify risk limits
5. `place_order` — Binance futures testnet fill

Futures-specific validation:

```python
from execution import CCXTExecutionProvider
provider = CCXTExecutionProvider(
    exchange_id="binance", testnet=True, market_type="swap",
    api_key="<key>", secret="<secret>"
)
provider.exchange.load_markets()
balance = provider.exchange.fetch_balance()  # Should show 5000 USDT
ticker = provider.exchange.fetch_ticker("BTC/USDT:USDT")  # Should return price
```

### 10.2 A-share scripted validation sequence

1. Use a real MyQuant UUID `account_id`, not a friendly alias
2. Run `backtest_strategy` or `start_strategy_simulation` with the GM template for the target account
3. Probe account state with the GM account probe path and verify `get_cash()`, `get_position()`, and `context.accounts` all resolve
4. Validate one-shot order submission with `gm_one_shot_order_template.py`
5. Confirm balance, order status, and position snapshots in `runtime/gm_strategy/`
6. Prefer `SHSE.511990` for same-session buy+sell roundtrip checks; `SHSE.510300` may still have `available_now=0` immediately after buy

### 10.3 Discord bridge validation

1. Check bridge process: look for `discord_agent_bridge.py` and `discord_bridge_daemon.py` in `tasklist`
2. Check heartbeat log: `C:\Users\Roy\.openclaw\logs\discord_bridge.log` (should have recent entries)
3. Send a test message to the Discord channel and verify bot responds

### 10.4 Platform validation

```powershell
openclaw config validate
openclaw plugins doctor
```

## 11. Known Issues

### 11.1 MyQuant account-id caveat

- Friendly aliases can be accepted by order payloads but still return empty `get_cash()`, `get_position()`, and `context.accounts`
- Always validate the real UUID `account_id` first with the GM account probe template

### 11.2 AKShare instability

- A-share data source: `sina` (primary, stable) → `akshare` → `yfinance` fallback
- 东方财富 API 被网络封锁，不可用

### 11.3 Discord bridge stability

- **v2 (2026-04-09)**: Bridge is now much more stable with exponential backoff and heartbeat ACK timeout detection
- SSL EOF errors are tolerated without disconnect (previously caused reconnect storms)
- Old bridge had 89 reconnects in 39 hours; v2 has 0 reconnects under the same conditions
- Heartbeat ACK rate: 100%
- Daemon has rate-limit protection (10 restarts/hr, then 5-min cooldown)
- Known issue: daemon code had typo `restart_restarts` → `recent_restarts` (fixed, effective on next daemon restart)

### 11.4 CCXTExecutionProvider parameter naming

- The constructor uses `secret` not `api_secret` for the API secret
- Config YAML uses `secret` under `execution.futures` and `execution.ccxt`
- This mismatch has caused confusion in test scripts

### 11.5 UnicodeEncodeError in notifications

- Bridge notification code can throw `UnicodeEncodeError` with surrogate characters
- `to_jsonable()` sanitizer mitigates this but edge cases remain

### 11.6 Binance testnet proxy bypass

- Remote host has proxy (127.0.0.1:7897) set globally via env vars
- Binance testnet URLs must bypass proxy for direct access
- `_should_bypass_proxy()` now defaults to bypass for `binance + testnet` (2026-04-08 fix)
- Without bypass, `load_markets()` fails (sapi 404 through proxy), monkey-patch fallback also fails (urllib doesn't use CCXT session)
- Can override with `OPENCLAW_BINANCE_TESTNET_PROXY_MODE=force-proxy` if needed

### 11.7 market_type="swap" vs "future" comparison

- CCXT uses `"swap"` for perpetual futures (USDT-M), not `"future"`
- All `market_type == "future"` comparisons in execution.py must use `market_type in ("future", "swap")`
- Fixed in 2026-04-08: `connect()`, `place_order()`, `get_positions()`, `get_balance()` all updated
- `_normalize_ccxt_market_type()` normalizes input to either "swap" or "spot"

### 11.8 GmTradeProvider gm.api compatibility (2026-04-09)

- `gmtrade` package is NOT installed; GmTradeProvider uses `gm.api` with compatibility layer
- Key API differences between `gmtrade.api` and `gm.api`:
  - Authentication: `gm.api.set_account_id(uuid)` replaces `gm.account()` + `gm.login()`
  - `get_cash(account_id=string)` — parameter is `account_id`, not `account`
  - `get_position()` — singular, returns list; NOT `get_positions()`
  - `order_cancel_all()` — no `order_cancel(cl_ord_id=...)` available
  - `history()` — time format "2026-03-01 09:30:00" (NO timezone suffix like +08:00)
  - Returns `DictLikeObject` (dict-like) instead of plain dict for `get_cash`
- If `gmtrade` is installed in the future, `connect()` will prefer it; `gm.api` is the fallback
- `gm` package is installed on BOTH Python312 and gm311 runtime as of 2026-04-09

### 11.9 Do not start desktop bridge from plain SSH

- Process may start in wrong window station
- Always use `\AutoStartTHSBridge` scheduled task

## 12. How To Continue Development

Recommended continuation order:

1. **A-share live trading** - GmTradeProvider is now functional with `gm.api`; wire it into the 5-min review loop
2. **Automate strategy rollout** - wire nightly optimization and daytime simulation management around `runtime/gm_strategy/`
3. **Futures validation** - validate futures testnet order placement end-to-end (currently only balance/ticker verified)
4. **Strategy expansion** - add symbol-specific strategies and stronger backtesting coverage (see strategy analysis in daily logs)
5. **Production migration** - switch from testnet/simulation to real venues only when ready

Concrete next code entry points:

- Trading kernel (pure functions): `workspace-trading/kernel/decision.py`, `kernel/risk.py`, `kernel/strategy/`
- Kernel unit tests: `workspace-trading/tests/test_decision.py`, `tests/test_risk.py`, `tests/test_strategy.py`
- MyQuant runtime orchestration: `workspace-trading/gm_strategy_runtime.py`
- MyQuant one-shot/account probe templates: `workspace-trading/templates/gm_one_shot_order_template.py`, `workspace-trading/templates/gm_account_probe_template.py`
- Execution routing: `execution/execution.py`
- OpenClaw decision layer: `workspace-trading/openclaw_trading_bridge.py`
- Data provider: `sensory/data_provider.py`
- Discord bridge: `workspace/discord_agent_bridge.py`

## 13. Rules For Future Contributors

Do:

- Edit local source first, deploy after validation
- Keep SOUL.md / TOOLS.md / USER.md in sync with code changes
- Keep one decision log entry per symbol per wake-up
- Keep approval mandatory for real orders
- Use the MyQuant scripted route for all current A-share simulation and validation work
- Use `gm311` Python runtime for dedicated MyQuant scripted simulations; Python 3.12 for bridge-integrated GM calls
- Check Discord bridge heartbeat when debugging communication issues

Do not:

- Write secrets into repo files
- Bypass `place_order` risk checks
- Switch trading jobs to long-lived session mode
- Claim A-share execution works without live market-hours proof
- Use alias-style `account_id` values for MyQuant balance/position verification
- Use `CCXTExecutionProvider(api_secret=...)` — the parameter name is `secret`
- Start Discord bridge from plain SSH — use the scheduled task

## 14. Minimal Recovery Checklist

1. Validate OpenClaw config: `openclaw config validate`
2. Confirm both `trading-crypto-5min` and `trading-cn-equity-5min` jobs exist in `cron/jobs.json`
3. Confirm gateway running: `curl http://127.0.0.1:18789/health`
4. Confirm proxy: `curl -x http://127.0.0.1:7897 https://api.binance.com/api/v3/ping`
5. Run one manual trading review via `run_trading_review_now.cmd`
6. For scripted A-shares: verify a GM UUID account with the account probe path under `runtime/gm_strategy/`
7. Check Discord bridge: verify heartbeat in `logs/discord_bridge.log`
8. Restart Discord bridge if needed: `schtasks /Run /TN "DiscordBridge"`

## 15. Related Documents

- Bug fix history: [CHANGELOG.md](CHANGELOG.md)
- Operations runbook: [OPERATIONS.md](OPERATIONS.md)
- Agent behavior: [SOUL.md](SOUL.md)
- Tool usage: [TOOLS.md](TOOLS.md)
