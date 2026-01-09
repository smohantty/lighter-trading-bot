import unittest
from decimal import Decimal

from src.strategy.common import (
    calculate_grid_prices,
    calculate_grid_prices_by_spread,
)
from src.strategy.types import GridType


class TestStrategyCommon(unittest.TestCase):
    def test_calculate_grid_prices_by_spread_basic(self):
        lower = Decimal("100")
        upper = Decimal("150")
        spread = Decimal("1000")  # 10%

        # 100 * 1.1 = 110
        # 110 * 1.1 = 121
        # 121 * 1.1 = 133.1
        # 133.1 * 1.1 = 146.41
        # 146.41 * 1.1 = 161.051 (Exceeds 150)

        prices = calculate_grid_prices_by_spread(lower, upper, spread)
        self.assertEqual(len(prices), 5)
        self.assertEqual(prices[0], Decimal("100"))
        self.assertEqual(prices[1], Decimal("110"))
        self.assertEqual(prices[2], Decimal("121"))
        self.assertEqual(prices[3], Decimal("133.1"))
        self.assertEqual(prices[4], Decimal("146.41"))

    def test_calculate_grid_prices_by_spread_reverse_compatibility(self):
        # We need to test that if we generate a grid with a certain spread,
        # using the resulting count and Geometric GridType on the original range
        # (adjusted for the actual last price) gives similar results.

        # Note: Geometric grid calculates ratio based on (upper/lower)^(1/(n-1)).
        # If we use strict calculate_grid_prices_by_spread, the last price might not be exactly upper_price.
        # So we should use the LAST price from our list as the upper_price for the geometric check
        # to ensure the math aligns.

        lower = Decimal("100")
        upper = Decimal("200")
        spread = Decimal("500")  # 5%

        spread_prices = calculate_grid_prices_by_spread(lower, upper, spread)

        n = len(spread_prices)
        if n < 2:
            self.fail("Generated grid too small for reverse test")

        real_upper = spread_prices[-1]

        # Now generate using Geometric
        geo_prices = calculate_grid_prices(GridType.GEOMETRIC, lower, real_upper, n)

        self.assertEqual(len(spread_prices), len(geo_prices))

        for p1, p2 in zip(spread_prices, geo_prices, strict=True):
            # Allow for very minor floating point differences if any, though Decimals should be precise
            self.assertAlmostEqual(p1, p2, places=10)

    def test_single_price_condition(self):
        lower = Decimal("100")
        upper = Decimal("100")  # Equal
        prices = calculate_grid_prices_by_spread(lower, upper, Decimal("100"))
        self.assertEqual(
            len(prices), 0
        )  # Should be empty as implementation returns empty if lower >= upper?
        # Wait, implementation says if lower >= upper return [].
        # If lower == upper, loop condition while <= runs?
        # But the early check `if lower_price >= upper_price` returns [].
        # Logic check: if range is 100-100, we probably can't have a spread.
        pass

    def test_small_spread(self):
        lower = Decimal("10000")
        upper = Decimal("10005")
        spread = Decimal("1")  # 1 bip = 0.01% = 0.0001
        # 10000 * 1.0001 = 10001

        prices = calculate_grid_prices_by_spread(lower, upper, spread)
        self.assertEqual(prices[0], Decimal("10000"))
        self.assertEqual(prices[1], Decimal("10001"))
        self.assertTrue(prices[-1] <= upper)


if __name__ == "__main__":
    unittest.main()
