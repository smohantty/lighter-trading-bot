
import unittest
from decimal import Decimal
from src.config import SpotGridConfig, GridType

class TestConfigSpread(unittest.TestCase):
    def test_valid_spread_config(self):
        config = SpotGridConfig(
            symbol="ETH/USDC",
            upper_price=Decimal("2000"),
            lower_price=Decimal("1000"),
            grid_type=GridType.GEOMETRIC,
            total_investment=Decimal("1000"),
            spread_bips=Decimal("100"), # 1%
            grid_count=None
        )
        config.validate()
        self.assertEqual(config.spread_bips, Decimal("100"))
        
    def test_invalid_mixed_config(self):
        # Arithmetic + Spread = Error
        config = SpotGridConfig(
            symbol="ETH/USDC",
            upper_price=Decimal("2000"),
            lower_price=Decimal("1000"),
            grid_type=GridType.ARITHMETIC,
            total_investment=Decimal("1000"),
            spread_bips=Decimal("100"),
            grid_count=None
        )
        with self.assertRaises(ValueError):
            config.validate()
            
    def test_missing_params(self):
        config = SpotGridConfig(
            symbol="ETH/USDC",
            upper_price=Decimal("2000"),
            lower_price=Decimal("1000"),
            grid_type=GridType.GEOMETRIC,
            total_investment=Decimal("1000"),
            spread_bips=None,
            grid_count=None
        )
        with self.assertRaisesRegex(ValueError, "Either grid_count or spread_bips must be provided"):
            config.validate()

    def test_spread_priority_implied(self):
        # If both are provided, spread check runs first?
        # My code checks grid_count is None and spread is None first. 
        # Then spread logic. Then grid logic.
        
        config = SpotGridConfig(
            symbol="ETH/USDC",
            upper_price=Decimal("2000"),
            lower_price=Decimal("1000"),
            grid_type=GridType.GEOMETRIC,
            total_investment=Decimal("1000"),
            spread_bips=Decimal("100"),
            grid_count=10
        )
        # Should be valid technically if logic allows both (though we might want to enforce XOR).
        # My implementation allows both but checks specific constraints on each if present.
        config.validate()

if __name__ == '__main__':
    unittest.main()
