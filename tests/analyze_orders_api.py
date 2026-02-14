import asyncio
import json
import logging
import os
import sys
from typing import cast

# Add project root to path
sys.path.append(os.getcwd())

from dotenv import load_dotenv

from src.config import ExchangeConfig, SpotGridConfig, load_config
from src.engine.simulation import SimulationEngine
from src.strategy.spot_grid import SpotGridStrategy

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main():
    # Load .env
    load_dotenv()

    # Load Configs
    config_path = "configs/spot_grid.template.yaml"
    if not os.path.exists(config_path):
        logger.error(f"Config file not found: {config_path}")
        return

    try:
        config = load_config(config_path)
        exchange_config = ExchangeConfig.from_env()

        logger.info(f"Target Symbol: {config.symbol}")
        logger.info(f"Account Index: {exchange_config.account_index}")

        # Initialize Strategy & Engine
        if config.type == "spot_grid":
            strategy = SpotGridStrategy(cast(SpotGridConfig, config))
        else:
            logger.error("Only spot_grid supported for this test")
            return

        engine = SimulationEngine(config, exchange_config, strategy)

        logger.info("Initializing Engine...")
        await engine.initialize()

        results = {}

        # 1. Active Orders
        logger.info("Fetching Active Orders...")
        try:
            market_id = engine.market_map[config.symbol]
            account_index = exchange_config.account_index

            # Auth Token is now handled internally by BaseEngine

            active_orders = await engine.get_active_orders(
                market_id=market_id, owner_account_index=account_index
            )
            results["active_orders"] = _serialize(active_orders)
            logger.info(f"Found {len(active_orders or [])} active orders")
        except Exception as e:
            logger.error(f"Error fetching active orders: {e}")
            results["active_error"] = str(e)

        # 2. Inactive Orders
        logger.info("Fetching Inactive Orders...")
        try:
            inactive_orders = await engine.get_inactive_orders(
                limit=10, market_id=market_id, owner_account_index=account_index
            )
            results["inactive_orders"] = _serialize(inactive_orders)
            logger.info(f"Found {len(inactive_orders or [])} inactive orders")
        except Exception as e:
            logger.error(f"Error fetching inactive orders: {e}")
            results["inactive_error"] = str(e)

        # Clean up
        await engine.cleanup()

        # Dump results
        with open("orders_dump.json", "w") as f:
            json.dump(results, f, indent=2)
        logger.info("Dumped results to orders_dump.json")

    except Exception as e:
        logger.error(f"Analysis failed: {e}", exc_info=True)


def _serialize(obj):
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, list):
        return [_serialize(x) for x in obj]
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    return str(obj)


if __name__ == "__main__":
    asyncio.run(main())
