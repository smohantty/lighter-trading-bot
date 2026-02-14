# CLAUDE.md - Long-Term Memory for Claude Code

## Memory Metadata
- **Last refreshed:** 2026-02-14
- **Project status:** Active development (do not assume docs are fully up to date)

## Project Overview
Actively developed high-performance trading bot for the Lighter.xyz DEX. Supports Perpetual and Spot Grid Trading strategies. Written in Python with async I/O and the Lighter SDK.

## Architecture

### Components
- **Entry point:** `main.py` -- CLI args (`--config`, `--dry-run`), config loading, engine launch (live or simulation)
- **Live Engine:** `src/engine/engine.py` -- WebSocket events, order lifecycle, reconciliation every 5 min, degraded mode gating, batch submission (max 49 orders)
- **Simulation Engine:** `src/engine/simulation.py` -- Dry-run and paper trading with simulated fills
- **Base Engine:** `src/engine/base.py` -- Shared: API/Signer init, market metadata loading, balance fetching
- **Strategy Context:** `src/engine/context.py` -- Sandbox for strategies: order queue, balances, market info. Strategies NEVER call exchange APIs directly.
- **Precision:** `src/engine/precision.py` -- Decimal-safe rounding for prices/sizes (ROUND_DOWN)
- **Perp Grid:** `src/strategy/perp_grid.py` -- Leveraged grid trading with LONG/SHORT bias, position tracking, weighted avg entry
- **Spot Grid:** `src/strategy/spot_grid.py` -- Neutral spot grid trading, inventory tracking
- **Strategy Base:** `src/strategy/base.py` -- Abstract interface: `on_tick`, `on_order_filled`, `on_order_failed`
- **Common Utils:** `src/strategy/common.py` -- Grid calculation (arithmetic/geometric/spread-based), trigger checks
- **Types:** `src/strategy/types.py` -- GridZone, GridBias, ZoneMode, Spread, StrategyState, GridState, summaries
- **Models:** `src/model.py` -- Cloid, OrderSide, OrderFill, PendingOrder, OrderFailure, LimitOrderRequest, MarketOrderRequest, CancelOrderRequest, Trade
- **Config:** `src/config.py` -- Pydantic schemas: PerpGridConfig, SpotGridConfig, ExchangeConfig, SimulationConfig
- **Constants:** `src/constants.py` -- All tunable parameters (timeouts, batch limits, TTLs, spreads)
- **Broadcast:** `src/broadcast/server.py` -- WebSocket status server (port 9001), `src/broadcast/types.py` event types
- **UI:** `src/ui/console.py` -- Grid visualization for dry-run mode

### Event Flow
```
WebSocket -> Event Queue -> Engine Message Processor
    -> Strategy.on_tick(price)
    -> Strategy.on_order_filled(fill)
    -> Strategy.on_order_failed(failure)
        -> Context.order_queue
            -> Batch Submitter -> Lighter Exchange
```

### Key Patterns
- All financial math uses Python `Decimal`, market-specific precision via `MarketInfo.round_price()`/`round_size()`
- Strategy state machine: Initializing -> WaitingForTrigger -> AcquiringAssets -> Running
- Reconciliation loop (5 min) catches missed WS events; two-miss rule prevents false positives
- Degraded mode pauses submissions for 30s on API errors (429, 5xx)
- Completed CLOIDs cached 24h for idempotency

## Development Commands
- **Package manager:** `uv` (NEVER use `pip install`)
- **Install deps:** `uv sync`
- **Run live:** `uv run python main.py <config_file>`
- **Dry run:** `uv run python main.py <config_file> --dry-run`
- **Tests:** `uv run pytest tests/`
- **Code quality (MUST run before commits):** `./check_code.sh` (ruff format + ruff check + mypy)
- **Lint only:** `uv run ruff check .`
- **Format only:** `uv run ruff format .`
- **Type check only:** `./run_mypy.sh`
- **Deploy:** `./deployment/start.sh --config <config_path>` (dry-run -> confirm -> tmux)

## Current Baseline (2026-02-14)
- Local tests: `uv run pytest tests -q` -> **50 passed, 3 warnings**
- Current warnings are SDK-side deprecations from `websockets` usage in `lighter` package
- Working tree note: `CODE_REVIEW_REPORT.md` is currently untracked
- `AGENT.md` is now intentionally compact and stable; use it for guardrails and `CLAUDE.md` for evolving project memory

## Code Style Rules
- Use type hints consistently
- Follow existing StrategyContext pattern for order queueing
- Use MarketInfo for all precision calculations
- Do not modify `.env` or `wallet_config.json` with real keys
- Round all order sizes/prices via `MarketInfo.round_size()`/`round_price()`
- Do not generate documentation describing what code does

## Config Files
- Strategy configs: YAML in `configs/` (symbol, grid params, leverage, investment)
- Exchange config: `.env` + `wallet_config.json` (API keys, network)
- Simulation config: `simulation_config.json` (balance modes, execution modes)

## Known Issues (Code Review 2026-02-14)
Full report: `CODE_REVIEW_REPORT.md`

### Critical
- Acquisition order failure permanently stalls bot (no recovery path) -- perp_grid.py & spot_grid.py `on_order_failed`
- Nonce gaps in batch signing on mid-batch errors -- engine.py
- Spot grid entry prices never updated after acquisition fill -- spot_grid.py `_handle_acquisition_fill`
- Partial fills in failed orders silently lost (position drift) -- both strategies
- Division by zero in weighted avg on zero-size fill -- engine.py

### High Priority
- position_side reports "Short" when flat (zero position) -- perp_grid.py:197
- Sell/buy size asymmetry in spot grid (FEE_BUFFER applied only to sells) -- spot_grid.py:491
- Zones permanently dead after MAX_ORDER_RETRIES (5) with no recovery
- account_index==0 treated as invalid (truthiness check) -- base.py:48,61
- WebSocket broadcaster bound to 0.0.0.0:9001 without authentication
- Double cleanup() call paths -- engine.py run()/stop()
- No-op symbol validator (pass instead of raise) -- config.py:85-92

### Test Gaps
- Core engine paths untested: process_order_queue, _handle_user_fills_msg, _handle_open_orders_msg
- Config loading functions untested: load_config, ExchangeConfig.from_env
- SimulationEngine entirely untested
- Geometric grid type untested
- Trigger price workflow untested
- tests/analyze_orders_api.py and tests/test_app.py are integration scripts, not tests
