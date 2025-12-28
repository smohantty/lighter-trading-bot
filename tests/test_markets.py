import unittest
from unittest.mock import MagicMock, AsyncMock, patch
from src.engine.core import Engine
from src.config import ExchangeConfig, NoOpConfig
from src.strategy.noop import NoOpStrategy

class TestMarketLoading(unittest.IsolatedAsyncioTestCase):
    async def test_market_map_parsing(self):
        # Mock Config
        exch_conf = MagicMock(spec=ExchangeConfig)
        exch_conf.base_url = "https://test.com"
        exch_conf.account_index = 10
        exch_conf.agent_key_index = 1
        exch_conf.agent_private_key = "00"*32 # Valid hex
        exch_conf.symbol = "ETH/USDC"
        
        strat_conf = NoOpConfig(symbol="ETH/USDC", type="noop")
        strategy = NoOpStrategy()
        
        # Mock Engine clients
        with patch('src.engine.core.lighter') as mock_lighter:
            # Mock ApiClient
            mock_api_client = MagicMock()
            mock_lighter.ApiClient.return_value = mock_api_client
            
            # Mock SignerClient (avoid real init)
            mock_signer = MagicMock()
            mock_lighter.SignerClient.return_value = mock_signer
            
            # Mock OrderApi and order_books response
            mock_order_api = MagicMock()
            mock_lighter.OrderApi.return_value = mock_order_api
            
            # The structure user provided
            fake_response_dict = {
                "code": 200,
                "order_book_details": [
                    {
                        "symbol": "ETH",  # Perp
                        "market_id": 1,
                        "price_decimals": 2,
                        "size_decimals": 4,
                        "market_type": "perp",
                        "base_asset_id": 0,
                        "quote_asset_id": 0,
                        "min_base_amount": "0.1",
                        "min_quote_amount": "10.0"
                    },
                    {
                        "symbol": "ETH/USDC", # Spot
                        "market_id": 100,
                        "price_decimals": 2, 
                        "size_decimals": 4,
                        "market_type": "spot",
                        "base_asset_id": 1,
                        "quote_asset_id": 2,
                        "min_base_amount": "1.0",
                        "min_quote_amount": "5.0"
                    }
                ]
            }
            
            # Setup async return
            mock_order_api.order_books = AsyncMock(return_value=fake_response_dict)
            
            # Init Engine: config, exchange_config, strategy
            engine = Engine(strat_conf, exch_conf, strategy)
            await engine.initialize()
            
            # Assertions
            # ETH (Perp) -> 1
            self.assertIn("ETH", engine.market_map)
            self.assertEqual(engine.market_map["ETH"], 1)
            
            # ETH/USDC (Spot) -> 100
            self.assertIn("ETH/USDC", engine.market_map)
            self.assertEqual(engine.market_map["ETH/USDC"], 100)
            
            # Ensure they are distinct
            self.assertNotEqual(engine.market_map["ETH"], engine.market_map["ETH/USDC"])
            
            # Check MarketInfo
            self.assertIn("ETH", engine.markets)
            self.assertEqual(engine.markets["ETH"].symbol, "ETH")
            self.assertEqual(engine.markets["ETH"].market_type, "perp")
            self.assertEqual(engine.markets["ETH"].min_base_amount, 0.1)
            
            self.assertIn("ETH/USDC", engine.markets)
            self.assertEqual(engine.markets["ETH/USDC"].symbol, "ETH/USDC")
            self.assertEqual(engine.markets["ETH/USDC"].market_type, "spot")
            self.assertEqual(engine.markets["ETH/USDC"].min_base_amount, 1.0)

if __name__ == "__main__":
    unittest.main()
