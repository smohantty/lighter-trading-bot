import asyncio
import logging
import sys
import os
from dotenv import load_dotenv

# Add root to python path
sys.path.append(os.getcwd())

from src.config import load_config, ExchangeConfig
from src.strategy.noop import NoOpStrategy
from src.engine.engine import Engine

# Logging Setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("test_noop")

async def main():
    logger.info("Starting NoOp Strategy Test...")
    load_dotenv()
    
    # 1. Load Config
    try:
        config = load_config("noop_config.yaml")
        logger.info(f"Loaded config: {config}")
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return

    # 2. Load Exchange Config
    try:
        exchange_config = ExchangeConfig.from_env()
        logger.info("Loaded Exchange Config.")
    except Exception as e:
        logger.error(f"Failed to load exchange config: {e}")
        return

    # 3. Init Strategy
    if config.type == "noop":
        strategy = NoOpStrategy()
    else:
        logger.error(f"Unexpected strategy type: {config.type}")
        return

    # 4. Init Engine
    logger.info("Initializing Engine with NoOpStrategy...")
    try:
        engine = Engine(config, exchange_config, strategy)
        await engine.initialize()
        logger.info("Engine Initialized Successfully.")
    except Exception as e:
        logger.error(f"Engine initialization failed: {e}")
        return
        
    # 5. Run (Briefly or just init check)
    # Since running connects to WS/Rest, we might want to stop early or just ensure init is fine.
    # Check if we can initialize.
    
    logger.info("Test PASSED.")

if __name__ == "__main__":
    asyncio.run(main())
