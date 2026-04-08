# Trading Operations

For architecture, deployment, runtime, testing, and handoff details, read `DEVELOPMENT_MANUAL.md` first.

Use the helper scripts under `tools/` when you want to trigger trading workflows on demand from the remote host.

These helpers run the existing OpenClaw cron jobs in `isolated` mode, so they reuse the same production payloads without relying on the long-lived `agent:trading:main` session.

Available commands:

- `tools\\run_trading_review_now.cmd`
  Runs one immediate multi-asset trading review using the same payload as `trading-5min`.
- `tools\\refresh_macro_now.cmd`
  Refreshes macro state once without placing any orders.
- `tools\\run_strategy_optimize_now.cmd`
  Runs one optimization pass and prints the final summary.
- `tools\\run_crypto_testnet_e2e_now.cmd`
  Runs one crypto testnet direct-execution E2E validation through the current bridge path.

Current live strategy assignment:

- `BTC/USDT -> combined`
- `ETH/USDT -> combined`
- `510300 / 159915 -> dynamic`

Current futures status:

- `market=futures` is wired for Binance futures testnet
- futures orders require explicit `leverage` plus `stop_price` or `estimated_loss`
- futures are manual/test mode first and are not in `auto_execution_markets` by default

Current A-share execution notes:

- Preferred no-GUI simulation path is the MyQuant `gm.api` / `gmtrade` runtime exposed through `backtest_strategy`, `start_strategy_simulation`, and related bridge actions.
- Use the real MyQuant UUID `account_id` for account-state checks. Friendly aliases can still leave `get_cash`, `get_position`, and `context.accounts` empty.
- `SHSE.511990` is the current known-good same-session buy+sell validation symbol on the simulation account.
- `SHSE.510300` buy fills are valid, but immediate same-session sell may be blocked by `available_now=0` depending on account and venue rules.

Current operator channel:

- Feishu has been removed from the active trading runtime.
- Operator notifications and chat control now go through Discord only.

## Infrastructure (updated 2026-04-05)

Mihomo proxy startup:

```cmd
schtasks /Run /TN "StartMihomo"
```

OpenClaw gateway startup:

```cmd
schtasks /Run /TN "OpenClaw Gateway"
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

## Troubleshooting Quick Reference

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `fetch_ticker` returns 0 prices | `sensory.sources.ccxt.testnet` is `true` or missing | Set `testnet: false` in sensory.sources.ccxt |
| `ECONNREFUSED 127.0.0.1:7897` | Mihomo not running | `schtasks /Run /TN StartMihomo` |
| `gateway already running` | Stale lock file | Delete `gateway*.lock` in temp dir |
| `JavaScript heap out of memory` | Another process eating RAM | Check `tasklist` for bloated processes |
| mihomo `Parse config error: GeoSite` | `fallback-filter` contains `geosite` key | Remove `geosite` from fallback-filter |
| `MODULE_NOT_FOUND openclaw.mjs` | Stale startup batch file | Use `gateway.cmd` instead of old bat file |

Why this exists:

- Scheduled runs already use `sessionTarget: isolated` and are stable.
- The long-lived interactive session can accumulate too much context over time.
- These helpers provide a safe manual entry point that follows the same isolated path as the scheduler.
