import unittest
from decimal import Decimal
from src.engine.context import MarketInfo

class TestDecimalPrecision(unittest.TestCase):
    def setUp(self):
        self.market_info = MarketInfo(
            symbol="LIT/USDC",
            coin="LIT",
            market_id=1,
            sz_decimals=3,
            price_decimals=4,
            market_type="spot",
            base_asset_id=1,
            quote_asset_id=2,
            min_base_amount=Decimal("0.001"),
            min_quote_amount=Decimal("5.0")
        )

    def test_round_price(self):
        # Precise rounding
        price = Decimal("123.456789")
        rounded = self.market_info.round_price(price)
        self.assertEqual(rounded, Decimal("123.4567"))
        self.assertIsInstance(rounded, Decimal)

        # Float input handling
        price_f = 123.456789
        rounded_f = self.market_info.round_price(price_f)
        self.assertEqual(rounded_f, Decimal("123.4567"))
        self.assertIsInstance(rounded_f, Decimal)

    def test_round_size(self):
        size = Decimal("1.23456")
        rounded = self.market_info.round_size(size)
        self.assertEqual(rounded, Decimal("1.234"))

    def test_truncation_behavior(self):
        # User specified case: 1.23456 with 4 decimals should be 1.2345
        from src.engine.precision import Precision
        p = Precision(4)
        val = Decimal("1.23456")
        self.assertEqual(p.round(val), Decimal("1.2345"))
        
        # Test negative numbers (ROUND_DOWN usually implies towards zero for positive, 
        # but ROUND_FLOOR is strictly lower. ROUND_DOWN matches 'truncate extra digits')
        # -1.23456 -> -1.2345 (ROUND_DOWN) vs -1.2346 (ROUND_FLOOR? No floor is lower)
        # ROUND_DOWN: toward zero. -1.23456 -> -1.2345.
        # ROUND_FLOOR: -infinity. -1.23456 -> -1.2346 (if standard).
        # We used ROUND_DOWN.
        val_neg = Decimal("-1.23456")
        self.assertEqual(p.round(val_neg), Decimal("-1.2345"))


    def test_to_sdk_conversion(self):
        # Price to atoms
        price = Decimal("100.0001")
        sdk_price = self.market_info.to_sdk_price(price)
        self.assertEqual(sdk_price, 1000001)

        # Size to atoms
        size = Decimal("1.001")
        sdk_size = self.market_info.to_sdk_size(size)
        self.assertEqual(sdk_size, 1001)


if __name__ == '__main__':
    unittest.main()
