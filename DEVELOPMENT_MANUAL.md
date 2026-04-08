# OpenClaw Trading System Development Manual

Last updated: `2026-04-07`

Bug fix history is in [CHANGELOG.md](CHANGELOG.md).

## 1. Scope

This document is the handoff and continuation manual for the OpenClaw multi-asset trading system.

It covers:

- current architecture
- local source-of-truth paths
- remote deployment paths
- scheduler and desktop-bridge runtime
- validation workflow
- known blockers and safe continuation points

This manual intentionally does **not** store API secrets or broker credentials.

## 2. Current System Status

As of `2026-04-07`, the system is running:

- **Crypto review loop**: Working, auto-trading every 5 min on Binance testnet
- **Crypto strategy**: Combined MA5/MA20 crossover + RSI 30/70, auto-approve enabled
- **Price data**: Binance mainnet (real prices)
- **Execution**: Binance testnet (test orders, not real money)
- **A-share scripted simulation**: MyQuant / `gm.api` route is working without GUI and has completed a real buy+sell roundtrip on `openclawtest`
- **Futures**: Code implemented, needs remote deployment + live validation
- **Notification**: Discord only, using the configured OpenClaw Discord bot/channel
- **Macro refresh**: Every 15 min
- **Strategy optimization**: Nightly at 02:00
- **Asset discovery**: Hourly, auto-adds/removes symbols based on volume/volatility

### Current A-share status

- A-share simulation is now MyQuant scripted execution through `gm.api` / `gmtrade` only
- `openclawtest` completed a validated same-session buy+sell roundtrip on `SHSE.511990`
- `SHSE.510300` buy fill is valid, but immediate same-session sell may still be blocked by `available_now=0`
- MyQuant account queries require the real UUID `account_id`; friendly aliases do not populate `get_cash()`, `get_position()`, or `context.accounts`


### Remaining futures blocker

- Futures bridge changes need deployment to remote `.openclaw` workspace
- First validation: manual testnet order with explicit `leverage` + `stop_price`

## 3. Source Of Truth

### 3.1 Local development repo

These local files are the source of truth for future edits:

- plugin entry:
  - `C:\Users\Roy\Documents\codex\stock\rs\ext\trading\plugin.js`
- trading bridge workspace:
  - `C:\Users\Roy\Documents\codex\stock\rs\remote-openclaw\workspace-trading\openclaw_trading_bridge.py`
  - `C:\Users\Roy\Documents\codex\stock\rs\remote-openclaw\workspace-trading\gm_strategy_runtime.py`
  - `C:\Users\Roy\Documents\codex\stock\rs\remote-openclaw\workspace-trading\DEVELOPMENT_MANUAL.md`
  - `C:\Users\Roy\Documents\codex\stock\rs\remote-openclaw\workspace-trading\OPERATIONS.md`
  - `C:\Users\Roy\Documents\codex\stock\rs\remote-openclaw\workspace-trading\SOUL.md`
  - `C:\Users\Roy\Documents\codex\stock\rs\remote-openclaw\workspace-trading\TOOLS.md`
  - `C:\Users\Roy\Documents\codex\stock\rs\remote-openclaw\workspace-trading\trading_rules.yaml`
  - `C:\Users\Roy\Documents\codex\stock\rs\remote-openclaw\workspace-trading\training_examples.json`
  - `C:\Users\Roy\Documents\codex\stock\rs\remote-openclaw\workspace-trading\schedule.yaml`
  - `C:\Users\Roy\Documents\codex\stock\rs\remote-openclaw\workspace-trading\templates\gm_ma_cross_template.py`
  - `C:\Users\Roy\Documents\codex\stock\rs\remote-openclaw\workspace-trading\templates\gm_one_shot_order_template.py`
  - `C:\Users\Roy\Documents\codex\stock\rs\remote-openclaw\workspace-trading\templates\gm_account_probe_template.py`
- execution:
  - `C:\Users\Roy\Documents\codex\stock\rs\remote-openclaw\execution\execution.py`
  - `C:\Users\Roy\Documents\codex\stock\rs\remote-openclaw\execution\__init__.py`
- sensory:
  - `C:\Users\Roy\Documents\codex\stock\rs\remote-openclaw\sensory\data_provider.py`
  - `C:\Users\Roy\Documents\codex\stock\rs\remote-openclaw\sensory\__init__.py`
- system config:
  - `C:\Users\Roy\Documents\codex\stock\rs\remote-openclaw\config.yaml`
  - `C:\Users\Roy\Documents\codex\stock\rs\remote-openclaw\jobs.json`
- gatekeeper:
  - `C:\Users\Roy\Documents\codex\stock\rs\remote-openclaw\gatekeeper\gatekeeper.py`
  - `C:\Users\Roy\Documents\codex\stock\rs\remote-openclaw\gatekeeper\config.yaml`
- validation helpers:
  - `C:\Users\Roy\Documents\codex\stock\rs\remote-openclaw\workspace-trading\bridge_smoke.py`
  - `C:\Users\Roy\Documents\codex\stock\rs\remote-openclaw\tools\e2e_live_testnet_order.py`
  
### 3.2 Remote deployed paths

The live remote deployment target is the user workspace on the Windows host (`10.83.120.248`, SSH port 2222):

- `C:\Users\Roy\.openclaw\extensions\trading\plugin.js`
- `C:\Users\Roy\.openclaw\workspace-trading\...`
- `C:\Users\Roy\.openclaw\execution\execution.py`
- `C:\Users\Roy\.openclaw\config.yaml`
- `C:\Users\Roy\.openclaw\cron\jobs.json`

If local files and remote files diverge, treat the local repo under `rs\remote-openclaw` and `rs\ext\trading` as canonical, then redeploy.

## 4. Architecture

### 4.1 High-level flow

`OpenClaw plugin -> Python trading bridge -> data / indicator / risk / approval / execution -> runtime logs and state`

More concretely:

1. `plugin.js` exposes tools to OpenClaw.
2. OpenClaw calls the tool.
3. `openclaw_trading_bridge.py` dispatches the action.
4. The bridge reads config and rules.
5. Data comes from `ccxt`, `akshare`, or `yfinance` fallback.
6. Risk is checked in the bridge.
7. Real orders go through gatekeeper approval.
8. Execution is routed by market:
   - `crypto -> ccxt / Binance spot testnet`
   - `futures -> ccxt_futures / Binance futures testnet`
9. Decisions and runtime state are persisted to log/state files.

### 4.2 OpenClaw knowledge layer

OpenClaw is guided by:

- `SOUL.md` 锟?personality and strategy rules
- `TOOLS.md` 锟?how to use each tool correctly
- `USER.md` 锟?user profile and communication preferences
- `memory/YYYY-MM-DD.md` 锟?daily notes (methodology, not data)
- `training_examples.json` 锟?few-shot behavioral examples

**Important**: When adding new tools or changing behavior, update BOTH the code AND these docs. Otherwise the agent won't use tools correctly.

### 4.3 Scheduler model

Trading jobs use `sessionTarget: isolated`.

This is intentional 锟?long-lived sessions accumulate too much context. Isolated job turns are more stable and deterministic.

Do **not** casually switch trading jobs back to long-lived interactive sessions.

### 4.4 Data flow for trade queries

```
User asks "what trades happened"
  -> Agent reads memory/USER.md (knows to use tools, not guess)
  -> Calls get_trading_state (recent_events, last 20)
  -> Or reads trading_decisions.jsonl for full history
  -> Returns real data with symbol/qty/price/reason
```

## 5. Component Map

### 5.1 Plugin layer (`plugin.js`)

- Expose bridge actions as OpenClaw tools
- Inject workspace path and rules file
- Mirror proxy environment variables (HTTP_PROXY / http_proxy)

### 5.2 Bridge layer (`openclaw_trading_bridge.py`)

Key responsibilities:

- Unified tool entrypoint (30+ actions)
- Multi-asset review universe (crypto + cn_equity)
- Per-symbol strategy resolution (combined, trend_following, mean_reversion)
- Indicator calculation (EMA, RSI, MACD, Bollinger)
- Signal generation and validation
- Risk checks (max loss, position size, daily budget)
- Order routing via approval/rejection/execution
- Decision logging and state persistence
- Operator notifications via Discord

Key design patterns:

- `to_jsonable()` 锟?recursive sanitizer that cleans surrogate chars before any JSON serialization
- `save_state()` 锟?persists runtime state through `to_jsonable()` for encoding safety
- `send_operator_notification()` sends directly to the configured Discord channel
- Notification titles embed key info: `[OpenClaw 浜ゆ槗缁撴灉] BTC/USDT 涔板叆 0.01 @ 67000`

### 5.3 Execution layer (`execution.py`)

- `CCXTProvider` 锟?crypto spot + futures execution via ccxt
  - `get_positions()`: uses `fetch_tickers()` for real-time prices (not hardcoded 0)
  - Supports both mainnet (prices) and testnet (execution) via config

- `GmTradeProvider` 锟?A-share execution via MyQuant `gm.api` / `gmtrade`
  - Scripted execution path for backtest, simulation, and one-shot order validation
  - Requires the real UUID `account_id` for account state queries

### 5.4 MyQuant scripted runtime

Files: `gm_strategy_runtime.py`, `templates/gm_ma_cross_template.py`, `templates/gm_one_shot_order_template.py`, `templates/gm_account_probe_template.py`

- Runs without GUI automation
- Stores rendered scripts, pid/state, reports, and logs under `runtime/gm_strategy/`
- Supports backtest, long-running simulation, account probe, and one-shot order submission

### 5.5 Approval layer (`gatekeeper.py`)

- Approval state machine (pending 锟?approved/rejected/expired)
- Gatekeeper code remains available for future approval work, but manual approval is disabled in the current runtime
- Operator notifications now go to Discord
- Stale-approval recovery is disabled in the current runtime

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

## 7. Scheduler And Background Tasks

### 7.1 OpenClaw cron jobs

Defined in `cron/jobs.json`:

| Job | Schedule | Delivery | Agent |
|-----|----------|----------|-------|
| `trading-5min` | `*/5 * * * *` | none | trading |
| `macro-refresh-15min` | `*/15 * * * *` | none | trading |
| `discover-assets-hourly` | `0 * * * *` | none | trading |
| `strategy-optimize-nightly` | `0 2 * * *` | none | trading |
| `daily-content` | `0 8 * * *` | announce | main-brain |
| `evening-content` | `0 18 * * *` | announce | main-brain |
| `nightly-review` | `30 23 * * *` | announce | main |

### 7.2 Windows scheduled tasks

| Task | Purpose |
|------|---------|
| `OpenClaw Gateway` | Starts gateway.cmd on port 18789 |
| `StartMihomo` | Starts mihomo proxy on port 7897 |

### 7.3 Infrastructure

| Service | Port | Config |
|---------|------|--------|
| OpenClaw gateway | 18789 | `gateway.cmd` |
| Mihomo proxy | 7897 | `latest_sub.yml`, Taiwan nodes |
| Mihomo API | 9090 | Switch nodes via REST API |

## 8. Environment Rules

### 8.1 Secrets

Do not write secrets into repo files (Binance API keys, webhook secrets, broker credentials).

### 8.2 Proxies

- Crypto market data: uses proxy (`HTTP_PROXY=http://127.0.0.1:7897`)
- Binance testnet execution: biased toward direct connection

### 8.3 Python environments

| Runtime | Use Case |
|---------|----------|
| `C:\Users\Roy\.pyenv\pyenv-win\versions\3.11.9\python.exe` | Trading bridge, general |
| `C:\Users\Roy\.openclaw\runtime\gm311\Scripts\python.exe` | MyQuant / `gm.api` scripted runtime |

## 9. Safe Development Workflow

1. **Edit locally first** 锟?always edit `rs\remote-openclaw\...` and `rs\ext\trading\...`
2. **Validate locally** 锟?`py -3 -m py_compile <file>`
3. **Deploy to remote** 锟?`scp -P 2222 <local> roy@10.83.120.248:<remote>`
4. **Restart only what's needed**:
   - Plugin/bridge code: no restart needed (next invocation picks up changes)
   - MyQuant scripted runtime: stop and relaunch the specific simulation if `gm_strategy_runtime.py` or a template changed
   - OpenClaw config: restart gateway via `schtasks /Run /TN "OpenClaw Gateway"`

## 10. Validation Playbook

### 10.1 Crypto validation layers

1. `get_market_data` 锟?confirm price data flows
2. `calculate_indicator` 锟?verify indicators
3. `generate_signal` 锟?verify signal logic
4. `check_risk` 锟?verify risk limits
5. `place_order` 锟?Binance testnet fill

### 10.2 A-share scripted validation sequence

1. Use a real MyQuant UUID `account_id`, not a friendly alias
2. Run `backtest_strategy` or `start_strategy_simulation` with the GM template for the target account
3. Probe account state with the GM account probe path and verify `get_cash()`, `get_position()`, and `context.accounts` all resolve
4. Validate one-shot order submission with `gm_one_shot_order_template.py`
5. Confirm balance, order status, and position snapshots in `runtime/gm_strategy/`
6. Prefer `SHSE.511990` for same-session buy+sell roundtrip checks; `SHSE.510300` may still have `available_now=0` immediately after buy


2. `prepare_trade_panel('buy' or 'sell')` 锟?panel ready
3. `probe_account` again 锟?confirm recovery
4. Only then test `place_order` during market hours

### 10.4 Platform validation

```powershell
openclaw config validate
openclaw plugins doctor
```

## 11. Known Issues

### 11.1 MyQuant account-id caveat

- Friendly aliases can be accepted by order payloads but still return empty `get_cash()`, `get_position()`, and `context.accounts`
- Always validate the real UUID `account_id` first with the GM account probe template


- `prepare_trade_panel` succeeds, but `place_order` submit still needs live market-hours validation

### 11.3 AKShare instability

- A-share data source: `auto` (akshare 锟?yfinance fallback)
- Acceptable for continuity, not ideal for production

### 11.4 Do not start desktop bridge from plain SSH

- Process may start in wrong window station
- Always use `\AutoStartTHSBridge` scheduled task

## 12. How To Continue Development

Recommended continuation order:

1. **MyQuant first** - keep the scripted `gm.api` / `gmtrade` path as the default A-share route
2. **Automate strategy rollout** - wire nightly optimization and daytime simulation management around `runtime/gm_strategy/`
3. **Futures deployment** - deploy futures bridge to remote, validate with testnet order
4. **Strategy expansion** - add symbol-specific strategies and stronger backtesting coverage
5. **Production migration** - switch from testnet/simulation to real venues only when ready

Concrete next code entry points:

- MyQuant runtime orchestration: `workspace-trading/gm_strategy_runtime.py`
- MyQuant one-shot/account probe templates: `workspace-trading/templates/gm_one_shot_order_template.py`, `workspace-trading/templates/gm_account_probe_template.py`
- Execution routing: `execution/execution.py`
- OpenClaw decision layer: `workspace-trading/openclaw_trading_bridge.py`

## 13. Rules For Future Contributors

Do:

- Edit local source first, deploy after validation
- Keep SOUL.md / TOOLS.md / USER.md in sync with code changes
- Keep one decision log entry per symbol per wake-up
- Keep approval mandatory for real orders
- Use the MyQuant scripted route for all current A-share simulation and validation work

Do not:

- Write secrets into repo files
- Bypass `place_order` risk checks
- Switch trading jobs to long-lived session mode
- Claim A-share execution works without live market-hours proof
- Use alias-style `account_id` values for MyQuant balance/position verification

## 14. Minimal Recovery Checklist

1. Validate OpenClaw config: `openclaw config validate`
2. Confirm `trading-5min` job exists in `cron/jobs.json`
3. Confirm gateway running: `curl http://127.0.0.1:18789/health`
4. Confirm proxy: `curl -x http://127.0.0.1:7897 https://api.binance.com/api/v3/ping`
5. Run one manual trading review via `run_trading_review_now.cmd`
6. For scripted A-shares: verify a GM UUID account with the account probe path under `runtime/gm_strategy/`

## 15. Related Documents

- Bug fix history: [CHANGELOG.md](CHANGELOG.md)
- Operations runbook: [OPERATIONS.md](OPERATIONS.md)
- Agent behavior: [SOUL.md](SOUL.md)
- Tool usage: [TOOLS.md](TOOLS.md)


