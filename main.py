import asyncio
import logging
import os
import sys
import argparse
import signal
from dotenv import load_dotenv

from src.config import load_config, ExchangeConfig
from src.strategy.perp_grid import PerpGridStrategy
from src.strategy.spot_grid import SpotGridStrategy
from src.strategy.noop import NoOpStrategy
from src.engine.engine import Engine

# Setup Logging
from logging.handlers import TimedRotatingFileHandler

# Ensure logs directory exists
os.makedirs("logs", exist_ok=True)

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        TimedRotatingFileHandler("logs/lighter-trading-bot.log", when="midnight", interval=1, backupCount=30)
    ],
    force=True
)
logger = logging.getLogger("main")

async def main():
    load_dotenv()
    
    # Parse Args
    parser = argparse.ArgumentParser(description="Lighter Trading Bot")
    parser.add_argument("strategy_config_file", nargs='?', help="Path to the strategy configuration file (YAML)")
    parser.add_argument("--config", help="Path to the strategy configuration file (YAML)")
    parser.add_argument("--dry-run", action="store_true", help="Perform a dry run simulation without executing orders.")
    args = parser.parse_args()

    strategy_config_file = args.config or args.strategy_config_file
    
    if not strategy_config_file:
        parser.print_help()
        return

    if not os.path.exists(strategy_config_file):
        logger.error(f"Strategy config file not found at {strategy_config_file}")
        return

    try:
        config = load_config(strategy_config_file)
        logger.info(f"Loaded config for {config.symbol} ({config.type})")
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return

    # Load Exchange Config
    try:
        exchange_config = ExchangeConfig.from_env()
    except Exception as e:
        logger.error(f"Failed to load exchange config: {e}")
        return

    # Init Strategy
    if config.type == "perp_grid":
        strategy = PerpGridStrategy(config)
    elif config.type == "spot_grid":
        strategy = SpotGridStrategy(config)
    elif config.type == "noop":
        strategy = NoOpStrategy()
    else:
        logger.error(f"Strategy type {config.type} not supported yet in main.")
        return

    # Start
    try:
        if args.dry_run:
            await _run_simulation(config, exchange_config, strategy)
        else:
            await _run_live(config, exchange_config, strategy)

    except asyncio.CancelledError:
        logger.info("Main task cancelled.")
    except Exception as e:
        logger.error(f"Bot execution failed: {e}")
        raise



async def _run_simulation(config, exchange_config, strategy):
    from src.engine.simulation import SimulationEngine
    from src.ui.console import ConsoleRenderer
    
    sim_engine = SimulationEngine(config, exchange_config, strategy)
    try:
        await sim_engine.initialize()
        await sim_engine.run_single_step()
        
        ConsoleRenderer.render(config, sim_engine.get_summary(), sim_engine.get_grid_state(), sim_engine.get_orders())
    except Exception as e:
        logger.error(f"Simulation Failed: {e}", exc_info=True)
        sys.exit(1)
    finally:
        await sim_engine.cleanup()

async def _run_live(config, exchange_config, strategy):
    from src.broadcast.server import StatusBroadcaster
    
    # Init Broadcast
    broadcaster = StatusBroadcaster(host="0.0.0.0", port=9001)
    
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
