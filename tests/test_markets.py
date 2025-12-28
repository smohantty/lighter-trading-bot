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
        exch_conf.symbol = "DOT"
        
        strat_conf = NoOpConfig(symbol="DOT", type="noop")
        strategy = NoOpStrategy()
        
        # Mock Engine clients
        with patch('src.engine.core.lighter') as mock_lighter:
            # Mock ApiClient
            mock_api_client = MagicMock()
            mock_lighter.ApiClient.return_value = mock_api_client
            
            # Mock SignerClient (avoid real init)
            mock_signer = MagicMock()
            mock_lighter.SignerClient.return_value = mock_signer
            
            # Mock OrderApi and order_book_details response
            mock_order_api = MagicMock()
            mock_lighter.OrderApi.return_value = mock_order_api
            
            # Realistic structure provided by user
            fake_response_dict = {
                "code": 200,
                "order_book_details": [
                    {
                        "symbol": "DOT",
                        "market_id": 11,
                        "market_type": "perp",
                        "base_asset_id": 0,
                        "quote_asset_id": 0,
                        "status": "active",
                        "taker_fee": "0.0000",
                        "maker_fee": "0.0000",
                        "liquidation_fee": "1.0000",
                        "min_base_amount": "2.0",
                        "min_quote_amount": "10.000000",
                        "order_quote_limit": "281474976.710655",
                        "supported_size_decimals": 1,
                        "supported_price_decimals": 5,
                        "supported_quote_decimals": 6,
                        "size_decimals": 1,
                        "price_decimals": 5,
                        "quote_multiplier": 1,
                        "default_initial_margin_fraction": 1000,
                        "min_initial_margin_fraction": 1000,
                        "maintenance_margin_fraction": 600,
                        "closeout_margin_fraction": 400,
                        "last_trade_price": 1.88252,
                        "daily_trades_count": 2491,
                        "daily_base_token_volume": 609113.8,
                        "daily_quote_token_volume": 1117702.641352,
                        "daily_price_low": 1.74415,
                        "daily_price_high": 1.92415,
                        "daily_price_change": 6.813887643764575,
                        "open_interest": 693546.1,
                        "daily_chart": {},
                        "market_config": {
                            "market_margin_mode": 0,
                            "insurance_fund_account_index": 281474976710655,
                            "liquidation_mode": 0,
                            "force_reduce_only": False,
                            "trading_hours": ""
                        }
                    },
                    {
                        "symbol": "WLD",
                        "market_id": 6,
                        "market_type": "perp",
                        "price_decimals": 5,
                        "size_decimals": 1,
                        "min_base_amount": "5.0",
                        "min_quote_amount": "10.0"
                    }
                ],
                "spot_order_book_details": [
                    {
                        "symbol": "ETH/USDC",
                        "market_id": 100,
                        "price_decimals": 4, 
                        "size_decimals": 6,
                        "market_type": "spot",
                        "base_asset_id": 1,
                        "quote_asset_id": 2,
                        "min_base_amount": "1.0",
                        "min_quote_amount": "5.0"
                    }
                ]
            }
            
            # Setup async return
            mock_order_api.order_book_details = AsyncMock(return_value=fake_response_dict)
            
            # Init Engine: config, exchange_config, strategy
            engine = Engine(strat_conf, exch_conf, strategy)
            await engine.initialize()
            
            # Assertions
            # DOT (Perp) -> 11
            self.assertIn("DOT", engine.market_map)
            self.assertEqual(engine.market_map["DOT"], 11)
            
            # Check MarketInfo for DOT
            self.assertIn("DOT", engine.markets)
            dot_info = engine.markets["DOT"]
            self.assertEqual(dot_info.symbol, "DOT")
            self.assertEqual(dot_info.market_type, "perp")
            self.assertEqual(dot_info.price_decimals, 5)
            self.assertEqual(dot_info.sz_decimals, 1)
            self.assertEqual(dot_info.min_base_amount, 2.0)
            
            # WLD (Perp) -> 6
            self.assertIn("WLD", engine.market_map)
            self.assertEqual(engine.market_map["WLD"], 6)
            
            # ETH/USDC (Spot) -> 100
            self.assertIn("ETH/USDC", engine.market_map)
            self.assertEqual(engine.market_map["ETH/USDC"], 100)
            self.assertEqual(engine.markets["ETH/USDC"].price_decimals, 4)

if __name__ == "__main__":
    unittest.main()
