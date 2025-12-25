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

    config_account_index = int(exchange_config.wallet_address)
    logger.info(f"Target Account Index: {config_account_index}")
    logger.info(f"Using Private Key: {exchange_config.private_key[:6]}... (masked)")

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

    # Query Account
    logger.info("Fetching Account Data...")
    try:
        account_api = lighter.AccountApi(client)
        # Fetch by ID (index)
        # SDK method: account_api.account(by="index", value=config_account_index)?
        # Let's check API signature. Usually get_account or account.
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
        
        signer = lighter.SignerClient(
            url="https://testnet.zklighter.elliot.ai", # Defaulting to Testnet for check
            account_index=config_account_index,
            api_private_keys={0: exchange_config.private_key} # Assuming index 0
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
