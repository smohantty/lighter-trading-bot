import asyncio
import logging
import sys
import os
from dotenv import load_dotenv

# Add root to python path
sys.path.append(os.getcwd())

from src.config import ExchangeConfig
import lighter

# Logging Setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("test_app")

async def main():
    logger.info("Starting Test Application...")
    load_dotenv()

    # 1. Load Configuration
    try:
        config = ExchangeConfig.from_env()
        logger.info(f"Loaded Configuration:")
        logger.info(f"  Network: {config.network}")
        logger.info(f"  Base URL: {config.base_url}")
        logger.info(f"  Account Index: {config.account_index}")
        logger.info(f"  API Key Index: {config.api_key_index}")
        logger.info(f"  Master Address: {config.master_account_address}")
    except Exception as e:
        logger.error(f"Failed to load configuration: {e}")
        return

    # 2. Initialize Clients
    api_client = None
    signer_client = None
    
    try:
        # ApiClient
        # Configure host
        api_config = lighter.Configuration(host=config.base_url)
        api_client = lighter.ApiClient(configuration=api_config)
        logger.info("Initialized ApiClient.")

        # SignerClient
        signer_client = lighter.SignerClient(
            url=config.base_url,
            account_index=config.account_index,
            api_private_keys={config.api_key_index: config.private_key}
        )
        logger.info("Initialized SignerClient.")

    except Exception as e:
        logger.error(f"Failed to initialize clients: {e}")
        if api_client: await api_client.close()
        if signer_client: await signer_client.close()
        return

    # 3. Verify Signer
    logger.info("Verifying Signer Client (Check Key)...")
    try:
        err = signer_client.check_client()
        if err:
             logger.error(f"Signer verification failed: {err}")
        else:
             logger.info("Signer verification PASSED.")
    except Exception as e:
        logger.error(f"Signer verification exception: {e}")

    # 4. Query Account Data
    logger.info("Querying Account Balance...")
    try:
        account_api = lighter.AccountApi(api_client)
        # Query by Index
        account_info = await account_api.account(by="index", value=str(config.account_index))
        
        # Dump info
        # Check if it's a model or dict. Assuming model with __dict__ or just printing str()
        logger.info(f"Account Data Received.")
        logger.info(f"Details: {account_info}")

    except Exception as e:
        logger.error(f"Failed to query account: {e}")

    # Cleanup
    if signer_client:
        await signer_client.close()
    if api_client:
        await api_client.close()
    
    logger.info("Test Application Finished.")

if __name__ == "__main__":
    asyncio.run(main())
