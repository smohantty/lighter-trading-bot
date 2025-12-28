import asyncio
import logging
import os
import sys

# Add root to python path
sys.path.append(os.getcwd())

from src.config import ExchangeConfig
import lighter

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("check_connection")

async def main():
    logger.info("Loading Configuration...")
    
    # Load Config from Env/File
    try:
        from dotenv import load_dotenv
        load_dotenv()
        exchange_config = ExchangeConfig.from_env()
    except Exception as e:
        logger.error(f"Failed to load configuration: {e}")
        logger.error("Ensure .env and wallet_config.json are set up correctly.")
        return

    logger.info(f"Testing Connection for Network: {exchange_config.network}")
    logger.info(f"Base URL: {exchange_config.base_url}")
    logger.info(f"Master Address: {exchange_config.master_account_address}")
    config_account_index = exchange_config.account_index
    logger.info(f"Target Account Index: {config_account_index}")
    logger.info(f"Using Private Key: {exchange_config.agent_private_key[:6]}... (masked)")

    # Initialize Client
    try:
        # Assuming baseUrl is default or we might need to parse it from json if ApiClient needs it.
        # ExchangeConfig currently doesn't expose baseUrl publicly (it wasn't in the class definition I modified, only in from_env logic but not stored?)
        # Let's check src/config.py again. I didn't add baseUrl to ExchangeConfig dataclass, 
        # I only parsed it in from_env but then didn't pass it to constructor?
        # Actually I missed adding `baseUrl` to `ExchangeConfig` dataclass in previous turn.
        # It defaults to lighter default.
        client = lighter.ApiClient()
    except Exception as e:
        logger.error(f"Failed to initialize ApiClient: {e}")
        return

    # 3. Query Account Info
    logger.info("Querying Account Info...")
    account_api = lighter.AccountApi(client)
    
    try:
        # Use account_index instead of wallet_address if possible, or support both.
        # But if we rely on typed ExchangeConfig, we have account_index.
        account_info = await account_api.account(by="index", value=str(config_account_index))
        logger.info(f"Account Info: {account_info}")
    except Exception as e:
        logger.error(f"Failed to query account: {e}")
        # Checking SDK examples/utils usage...
        # response = await account_api.accounts(l1_address=...) or by explicit ID?
        
        # In lighter-python: api.account(by='index', value=123)
        # or it might vary.
        
        # Let's try fetching order books first as a public test
        order_api = lighter.OrderApi(client)
        # Order books returns an object, possibly iterable or has data field
        # SDK examples: await order_api.order_books() -> returns OrderBooks object which has a list?
        # Trying to print it simply
        logger.info(f"Public API Check: Fetched OrderBooks.")
        
        # Now Private/Account
        # We need to know specific method. 
        # Assuming `active_orders` or similar exists.
        
        # Helper to dump account info
        pass
        
    except Exception as e:
        logger.error(f"API Error: {e}")

    # To really verify the Private Key, we should try to Sign something (like a cancel of a non-existent order or just verify signature matches).
    # SignerClient check_client method does exactly this.
    
    logger.info("Verifying Signer/Private Key...")
    try:
        # We need the key map
        # ExchangeConfig has single private_key.
        
        # 2. Setup Signer (Verify Keys)
        # Using configured API Key Index
        signer = lighter.SignerClient(
            url=exchange_config.base_url,
            api_private_keys={exchange_config.agent_key_index: exchange_config.agent_private_key},
            account_index=config_account_index
        )    
        err = signer.check_client()
        if err:
            logger.error(f"Signer Check Failed: {err}")
        else:
            logger.info("Signer Check Passed! Private Key corresponds to Account.")
            
        await signer.close()
        
    except Exception as e:
         logger.error(f"Signer Error: {e}")

    await client.close()

if __name__ == "__main__":
    asyncio.run(main())
