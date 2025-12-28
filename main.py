import asyncio
import logging
import os
import sys
from dotenv import load_dotenv

from src.config import load_config, ExchangeConfig
from src.strategy.perp_grid import PerpGridStrategy
from src.strategy.spot_grid import SpotGridStrategy
from src.strategy.noop import NoOpStrategy
from src.engine.core import Engine

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("main")

async def main():
    load_dotenv()
    
    # Load Config
    config_path = os.getenv("CONFIG_PATH", "config.yaml")
    if not os.path.exists(config_path):
        logger.error(f"Config file not found at {config_path}")
        return

    try:
        config = load_config(config_path)
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

    # Init Engine
    engine = Engine(config, exchange_config, strategy)
    
    # Start
    try:
        await engine.run()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception as e:
        logger.error(f"Bot execution failed: {e}")
        raise

if __name__ == "__main__":
    asyncio.run(main())
