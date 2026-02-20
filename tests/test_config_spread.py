import unittest
from decimal import Decimal

from pydantic import ValidationError

from src.config import SpotGridConfig
from src.strategy.types import GridType


class TestConfigSpread(unittest.TestCase):
    def test_valid_spread_config(self):
        # Should not raise
        SpotGridConfig(
            symbol="ETH/USDC",
            grid_range_high=Decimal("2000"),
            grid_range_low=Decimal("1000"),
            grid_type=GridType.GEOMETRIC,
            total_investment=Decimal("1000"),
            spread_bips=Decimal("100"),  # 1%
            grid_count=None,
        )

    def test_invalid_mixed_config(self):
        # Arithmetic + Spread = Error
        with self.assertRaises(ValidationError) as cm:
            SpotGridConfig(
                symbol="ETH/USDC",
                grid_range_high=Decimal("2000"),
                grid_range_low=Decimal("1000"),
                grid_type=GridType.ARITHMETIC,
                total_investment=Decimal("1000"),
                spread_bips=Decimal("100"),
                grid_count=None,
            )
        self.assertIn(
            "spread_bips can only be used with GEOMETRIC grid type", str(cm.exception)
        )

    def test_missing_params(self):
        with self.assertRaises(ValidationError) as cm:
            SpotGridConfig(
                symbol="ETH/USDC",
                grid_range_high=Decimal("2000"),
                grid_range_low=Decimal("1000"),
                grid_type=GridType.GEOMETRIC,
                total_investment=Decimal("1000"),
                spread_bips=None,
                grid_count=None,
            )
        self.assertIn(
            "Either grid_count or spread_bips must be provided", str(cm.exception)
        )

    def test_spread_too_small(self):
        with self.assertRaises(ValidationError) as cm:
            SpotGridConfig(
                symbol="ETH/USDC",
                grid_range_high=Decimal("2000"),
                grid_range_low=Decimal("1000"),
                grid_type=GridType.GEOMETRIC,
                total_investment=Decimal("1000"),
                spread_bips=Decimal("10"),  # < 15
                grid_count=None,
            )
        self.assertIn("spread_bips must be at least 15 bips", str(cm.exception))

    def test_spread_priority_implied(self):
        # If both are provided, Pydantic validator checks both.
        # My implementation allows both but checks specific constraints on each if present.
        # This test just checked validation pass/fail.

        # Valid case where both present (if permitted by validator logic)
        SpotGridConfig(
            symbol="ETH/USDC",
            grid_range_high=Decimal("2000"),
            grid_range_low=Decimal("1000"),
            grid_type=GridType.GEOMETRIC,
            total_investment=Decimal("1000"),
            spread_bips=Decimal("100"),
            grid_count=10,
        )


if __name__ == "__main__":
    unittest.main()
