# AGENT.md - Stable Instructions for Coding Agents

## Purpose
This file contains stable, high-signal rules for coding agents working in this repo.

For evolving project context (architecture details, current risks, baseline status, active priorities), read `CLAUDE.md`.

## Canonical Paths
- Entrypoint: `main.py`
- Live engine: `src/engine/engine.py`
- Shared engine utilities: `src/engine/base.py`
- Simulation engine: `src/engine/simulation.py`
- Strategy context: `src/engine/context.py`
- Strategies: `src/strategy/perp_grid.py`, `src/strategy/spot_grid.py`
- Config schemas/loaders: `src/config.py`

## Non-Negotiable Workflow
1. Use `uv` for dependency and command execution.
2. Do not use `pip install` directly.
3. Before commits, run `./check_code.sh`.
4. Do not commit changes that fail format, lint, or type checks.

## Core Engineering Rules
1. Strategies must enqueue orders through `StrategyContext`; do not call exchange APIs directly from strategy code.
2. Use market precision helpers for all order sizing/pricing:
   - `MarketInfo.round_size()`
   - `MarketInfo.round_price()`
3. Keep trading math in `Decimal`; avoid float-based calculations for business logic.
4. Preserve typed interfaces and existing async patterns.

## Safety Rules
1. Never commit real credentials or secret-bearing config.
2. Do not modify `.env` or `wallet_config.json` with real keys in a committable way.
3. Treat order lifecycle and reconciliation code as high-risk; prioritize correctness over refactors.

## Common Commands
- Install deps: `uv sync`
- Run bot: `uv run python main.py --config <config_path>`
- Dry run: `uv run python main.py --dry-run --config <config_path>`
- Tests: `uv run pytest tests/`
- Full quality gate: `./check_code.sh`
- Lint: `uv run ruff check .`
- Format: `uv run ruff format .`
- Types: `./run_mypy.sh`

