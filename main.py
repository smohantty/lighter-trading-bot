import argparse
import asyncio
import logging
import os
import signal
import sys
from typing import cast

from dotenv import load_dotenv

from src.config import ExchangeConfig, PerpGridConfig, SpotGridConfig, load_config
from src.engine.engine import Engine
from src.logging_utils import configure_logging
from src.strategy.base import Strategy
from src.strategy.perp_grid import PerpGridStrategy
from src.strategy.spot_grid import SpotGridStrategy

# Module-level logger (handlers configured dynamically in main)
logger = logging.getLogger("main")


def setup_logging(is_simulation: bool = False) -> str:
    """Configure process-wide logging and return run identifier."""
    return configure_logging(is_simulation=is_simulation)


async def main():
    load_dotenv()

    # Parse Args first to determine logging mode
    parser = argparse.ArgumentParser(description="Lighter Trading Bot")
    parser.add_argument(
        "strategy_config_file",
        nargs="?",
        help="Path to the strategy configuration file (YAML)",
    )
    parser.add_argument(
        "--config", help="Path to the strategy configuration file (YAML)"
    )

    # Simulation mode
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run in simulation mode (configure via LIGHTER_SIMULATION_CONFIG_FILE)",
    )

    args = parser.parse_args()

    # Setup logging based on mode (simulation uses separate log file)
    run_id = setup_logging(is_simulation=args.dry_run)
    logger.info(
        "Logging initialized run_id=%s mode=%s",
        run_id,
        "simulation" if args.dry_run else "live",
    )

    strategy_config_file = args.config or args.strategy_config_file

    if not strategy_config_file:
        parser.print_help()
        return

    if not os.path.exists(strategy_config_file):
        logger.error(f"Strategy config file not found at {strategy_config_file}")
        return

    try:
        config = load_config(strategy_config_file)
        logger.info(
            "Loaded strategy config symbol=%s type=%s config_path=%s",
            config.symbol,
            config.type,
            strategy_config_file,
        )
    except Exception as e:
        logger.error("Failed to load strategy config: %s", e, exc_info=True)
        return

    # Load Exchange Config
    try:
        exchange_config = ExchangeConfig.from_env()
    except Exception as e:
        logger.error("Failed to load exchange config: %s", e, exc_info=True)
        return

    # Init Strategy
    if config.type == "perp_grid":
        strategy: Strategy = PerpGridStrategy(cast(PerpGridConfig, config))
    elif config.type == "spot_grid":
        strategy = SpotGridStrategy(cast(SpotGridConfig, config))
    else:
        logger.error(f"Strategy type {config.type} not supported yet in main.")
        return

    logger.info(
        "Starting bot symbol=%s strategy=%s network=%s dry_run=%s",
        config.symbol,
        config.type,
        exchange_config.network,
        args.dry_run,
    )

    # Start
    try:
        if args.dry_run:
            await _run_simulation(config, exchange_config, strategy)
        else:
            await _run_live(config, exchange_config, strategy)

    except asyncio.CancelledError:
        logger.info("Main task cancelled.")
    except Exception as e:
        logger.error("Bot execution failed: %s", e, exc_info=True)
        raise


async def _run_simulation(config, exchange_config, strategy):
    from src.config import load_simulation_config
    from src.engine.simulation import SimulationEngine
    from src.ui.console import ConsoleRenderer

    # Load simulation config from LIGHTER_SIMULATION_CONFIG_FILE (or defaults)
    sim_config = load_simulation_config()

    logger.info(
        "[SIMULATION] Mode balance=%s execution=%s tick_ms=%s simulate_fills=%s",
        sim_config.balance_mode,
        sim_config.execution_mode,
        sim_config.tick_interval_ms,
        sim_config.simulate_fills,
    )
    if sim_config.balance_overrides:
        logger.info(
            "[SIMULATION] Balance overrides configured assets=%s",
            sorted(sim_config.balance_overrides.keys()),
        )

    sim_engine = SimulationEngine(config, exchange_config, strategy, sim_config)

    # Setup signal handler for continuous mode
    if sim_config.execution_mode == "continuous":
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, sim_engine.stop)

    exit_code = 0
    try:
        await sim_engine.initialize()

        if sim_config.execution_mode == "single_step":
            await sim_engine.run_single_step()
            ConsoleRenderer.render(
                config,
                sim_engine.get_summary(),
                sim_engine.get_grid_state(),
                sim_engine.get_orders(),
            )
        else:
            # Continuous mode - run with periodic console updates
            async def render_loop():
                while sim_engine._running:
                    await asyncio.sleep(5)  # Render every 5 seconds
                    ConsoleRenderer.render(
                        config,
                        sim_engine.get_summary(),
                        sim_engine.get_grid_state(),
                        sim_engine.get_orders(),
                        current_price=sim_engine.get_current_price(),
                    )

            # Run both engine and renderer
            await asyncio.gather(sim_engine.run(), render_loop())

    except ValueError as e:
        # Clean exit for expected validation errors (e.g., price out of grid range)
        logger.error("Simulation failed: %s", e)
        exit_code = 1
    except Exception as e:
        logger.error("Simulation failed unexpectedly: %s", e, exc_info=True)
        exit_code = 1
    finally:
        await sim_engine.cleanup()

    if exit_code:
        sys.exit(exit_code)


async def _run_live(config, exchange_config, strategy):
    from src.broadcast.server import StatusBroadcaster

    # Init Broadcast
    broadcast_host = os.getenv("LIGHTER_BROADCAST_HOST", "0.0.0.0")
    broadcast_port = int(os.getenv("LIGHTER_BROADCAST_PORT", "9001"))
    logger.info(
        "Live mode broadcaster configured host=%s port=%d",
        broadcast_host,
        broadcast_port,
    )
    broadcaster = StatusBroadcaster(host=broadcast_host, port=broadcast_port)

    # Init Engine
    engine = Engine(config, exchange_config, strategy, broadcaster)

    # Setup Signal Handlers
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(engine.stop()))

    try:
        await engine.run()
    finally:
        await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())
