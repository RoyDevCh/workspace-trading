# TOOLS.md - Trading Notes

Use these local rules when calling trading tools:

- `get_portfolio_snapshot`
- Use it for balance, available cash, margin, and current holdings questions before falling back to any shell or exec path.
- Prefer it whenever the operator asks “现在余额多少”, “我持有哪些币”, “还能不能买”, or “当前仓位是什么”.
- Pass `market=crypto` for spot testnet balances, `market=futures` for futures testnet balances, and `market=cn_equity` for the A-share simulation account.
- When the A-share execution mode is `gmtrade`, use the real MyQuant UUID `account_id` if you need non-empty cash/position/account state; friendly aliases can still leave account queries empty.
- Keep `include_zero_positions=false` unless the operator explicitly asks for every empty asset bucket.
- The current spot environment is Binance spot testnet. Do not answer spot-balance questions as if they refer to Hyperliquid.
- If the tool returns `tracked_positions`, summarize those first instead of dumping unrelated synthetic testnet assets.
- For A-shares, `get_portfolio_snapshot` now resolves balances and holdings through the scripted GM account probe path instead of the old synchronous `gmtrade` executor, so it should keep working even when only `gm.api` is installed.

- `probe_gm_account`
- Use it when you want the raw MyQuant scripted account probe response, including the saved stdout/stderr/report paths under `workspace-trading/runtime/gm_strategy/live_once/`.
- Prefer a real UUID `account_id`; aliases may still connect but return empty account state.
- It is the fastest way to verify whether the GM runtime can see cash, positions, and order snapshots before you debug a larger A-share review loop.

- `get_market_data`
- Default scope is only `BTC/USDT` and `ETH/USDT`.
- Default timeframe is `1h`.
- Default lookback is `100`.
- The same tool can also read China A-share data when `symbol` is a 6-digit code such as `510300`, `159915`, or `000001`.
- It can also read crypto futures candles when you pass `market=futures`.
- When the task is about A-shares, pass `market=cn_equity` or `source=akshare` to make the intent explicit.
- When the task is about Binance futures testnet, pass `market=futures`; the bridge will normalize Binance perpetual symbols internally.
- If `source=auto`, trust the resolved source returned by the tool instead of assuming the first candidate succeeded.
- In scheduled loops, prefer `compact=true` so the tool returns a short candle tail plus summary fields instead of flooding the agent context.

- `update_macro_state`
- Call it before any cross-asset comparison, A-share allocation suggestion, or regime discussion.
- It writes the latest macro snapshot to the runtime state file and returns a structured summary with preferred market and scale overrides.

- `get_macro_state`
- Use it to read the latest persisted macro snapshot without recomputing it.
- If the summary says `stale=true`, refresh it with `update_macro_state` before relying on it.

- `get_review_universe`
- Call it once per wake-up after refreshing stale macro state.
- It returns the exact symbols, markets, timeframes, lookbacks, and execution permissions for the current review cycle.
- Pass `market=crypto` or `market=cn_equity` when you are running a split-lane scheduled job so the tool only returns that lane's universe.
- For A-shares, inspect `market_session` and `execution_block_reason` when `execution_allowed=false`; that usually means weekend, midday break, holiday, or after-close, not a tool failure.
- If `execution_allowed=false`, you may still analyze and summarize the symbol, but you must not place a real order for it in the scheduled loop.

- `discover_assets`
- Call it in the hourly discovery run, not inside every 5-minute review.
- Pass `market=crypto` or `market=cn_equity` when discovery is running as a dedicated market lane.
- It scans spot crypto and China A-shares, adds new dynamic symbols into `trading_rules.yaml`, and returns what changed.
- Newly discovered symbols inherit conservative default strategies and smaller initial position scales.
- It may also trim excess non-core symbols when the configured universe cap is exceeded.

- `cleanup_assets`
- Use it to remove stale or underperforming dynamic symbols.
- It never removes core symbols.
- It reads the decision log and uses inactivity / drawdown rules from `auto_discover`.
- It should run before optimization or as part of the discovery maintenance cycle, not during every symbol review.

- `calculate_indicator`
- Prefer `indicator=all` unless the user asks for a specific indicator.
- Treat the returned values as the source of truth for summaries.
- If `market_data` is omitted but `symbol` is provided, the tool will fetch the required data internally. Use that path in scheduled loops to keep context small.
- `calculate_indicator` now supports Bollinger-band style outputs as well as EMA/RSI/MACD. Use `indicator=bollinger` when you need only band context.

- `backtest_strategy`
- Use it for the new MyQuant / `gm.api` strategy path when you want a scripted backtest instead of the built-in crypto/A-share heuristic optimizer.
- It renders a strategy template, appends the OpenClaw bootstrap automatically, runs it in a dedicated Python runtime, and returns parsed metrics plus the saved report/log paths.
- Prefer passing `template_file`, `params`, and a `backtest` block. Keep the gm token in `GM_TOKEN` or pass it in the payload; do not hardcode it into the template file.
- Prefer a real UUID `account_id` for any payload that also needs account-state validation.

- `start_strategy_simulation`
- Use it to launch a long-running MyQuant simulation process from a rendered template without any GUI automation.
- It records a state file, PID file, rendered script path, and stdout/stderr log paths under `workspace-trading/runtime/gm_strategy/`.
- Pair it with `get_strategy_simulation_status` and `stop_strategy_simulation` instead of managing the process manually.
- Before trusting balances or holdings, probe the same UUID account through the GM account-probe path; aliases are not sufficient.
- For same-session roundtrip validation, `SHSE.511990` is the current known-good symbol.

- `get_strategy_simulation_status`
- Use it to check whether the last scripted MyQuant simulation process is still running.
- It reads the persisted state file and verifies the PID against the OS process table.

- `stop_strategy_simulation`
- Use it to stop the scripted MyQuant simulation process that was launched by `start_strategy_simulation`.
- Pass `force=true` only when the process ignores a normal stop request.

- `generate_signal`
- Use it after your own rule-based reasoning as a structured cross-check.
- It is strategy-aware. The current live strategies are `combined`, `trend_following`, and `mean_reversion`, resolved per symbol from `trading_rules.yaml`.
- Read `strategy_name`, `strategy_kind`, and the returned indicator snapshot before you summarize the decision.
- For `market=futures`, treat `sell` as a potential short-entry signal rather than always describing it as a spot exit.
- If `strategy_kind=mean_reversion`, reason from Bollinger bands + RSI. Do not veto a mean-reversion buy just because EMA fast is still below EMA slow.
- If `strategy_kind=combined`, reason from the short/long moving-average cross plus the RSI overbought guard.
- If your own reasoning and tool output disagree, choose `hold`.

- `check_risk`
- Call it only for actionable `buy` or `sell` decisions.
- Never call it for a final `hold`.
- Never say risk was blocked unless the tool explicitly returns `allowed=false`.
- For A-shares, a blocked result may come from the market-session guard before any desktop execution is attempted.
- For futures, include `leverage`, plus either `stop_price` or `estimated_loss`. Futures risk uses a dedicated leverage cap, margin budget, daily loss budget, and daily trade-count limit.

- `optimize_strategy`
- Use it for the nightly self-optimization pass, not inside every 5-minute wake-up.
- Pass `market=crypto` or `market=cn_equity` when nightly maintenance is running in a dedicated market lane.
- It may update per-symbol order sizes after enough executed-trade evidence exists.
- If `optimizer_mode=gm_strategy` or both `template_file` and `param_grid` are provided, it switches into the scripted MyQuant grid-search path.
- It must preserve the symbol's assigned strategy family unless the operator explicitly changes `trading_rules.yaml`.
- It must not weaken approval rules or hard risk ceilings.

- `place_order`
- If `trading.auto_approve=true`, `place_order` executes immediately after risk passes.
- Manual approval recovery is currently disabled in the Discord-only runtime, so `place_order` should be treated as direct execution after risk passes.
- Never bypass `place_order` by calling exchange APIs directly.
- The same tool routes spot crypto orders to `ccxt`, futures orders to `ccxt_futures`, and A-share orders to the configured `gmtrade` runtime based on market context or `execution_mode`.
- Futures support is testnet-first. Do not assume it belongs in `auto_execution_markets` unless the rules explicitly enable that.
- Use the MyQuant `gmtrade` / `gm.api` route for all current A-share simulation and validation work.
- For A-share orders, prefer passing `market=cn_equity`; buy quantities should already be whole-share lots.
- If a same-session A-share sell is rejected, check `available_now` first. `SHSE.510300` may still be unsellable immediately after buy, while `SHSE.511990` is currently validated for buy+sell roundtrip.

- `record_trading_decision`
- Write exactly one decision entry per symbol per wake-up.
- Keep the message concise and factual.
- If data is incomplete or uncertain, record `hold`.

## Operator Notes

- When an operator wants to trigger a manual run from the remote host, prefer the helper scripts in `tools/` so the run goes through the same `isolated` cron path as production scheduling.
- Use the market-specific helper when you want only one lane (`run_crypto_review_now.cmd`, `run_cn_equity_review_now.cmd`, and the matching discovery / optimization scripts).
- Do not rely on a long-lived trading conversation for repeated manual reviews when an isolated run will achieve the same goal with less context bloat.
