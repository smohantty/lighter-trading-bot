import unittest
from decimal import Decimal
from unittest.mock import MagicMock

from src.config import SpotGridConfig
from src.engine.context import MarketInfo, StrategyContext
from src.model import Cloid, OrderFill, OrderSide
from src.strategy.spot_grid import SpotGridStrategy
from src.strategy.types import GridType, GridZone, StrategyState


class TestSpotGrid(unittest.TestCase):
    def setUp(self):
        self.spot_config = SpotGridConfig(
            symbol="LIT/USDC",
            grid_range_high=Decimal("2.0"),
            grid_range_low=Decimal("1.0"),
            grid_type=GridType.ARITHMETIC,
            grid_count=3,
            total_investment=Decimal("100.0"),
            type="spot_grid",
        )

        self.market_info = MagicMock(spec=MarketInfo)
        self.market_info.min_quote_amount = Decimal("5.0")
        self.market_info.min_base_amount = Decimal("1.0")
        self.market_info.price_decimals = 4
        self.market_info.sz_decimals = 4
        # Rounding logic with Decimal helper
        self.market_info.round_price.side_effect = lambda x: Decimal(
            str(round(float(x), 4))
        )
        self.market_info.round_size.side_effect = lambda x: Decimal(
            str(round(float(x), 4))
        )
        self.market_info.last_price = Decimal("1.6")

        self.context = MagicMock(spec=StrategyContext)
        self.context.market_info.return_value = self.market_info
        # Default big balance
        # Default big balance
        self.context.get_spot_available.return_value = Decimal("1000.0")
        # Mock place_order to return a Cloid (since strategy expects it now)
        self.context.place_order.side_effect = (
            lambda req: req.cloid if req.cloid else Cloid(12345)
        )

    def test_initialization_rust_logic(self):
        strategy = SpotGridStrategy(self.spot_config)
        strategy.initialize_zones(Decimal("1.6"), self.context)

        self.assertEqual(len(strategy.zones), 2)

        # Check Decimals
        self.assertEqual(strategy.zones[0].buy_price, Decimal("1.0"))
        self.assertEqual(strategy.zones[0].order_side, OrderSide.BUY)

        self.assertEqual(strategy.zones[1].buy_price, Decimal("1.5"))
        self.assertEqual(strategy.zones[1].order_side, OrderSide.BUY)

        self.assertEqual(strategy.inventory_base, Decimal("0.0"))
        self.assertGreater(strategy.inventory_quote, Decimal("0.0"))

    def test_initialization_sell_logic(self):
        strategy = SpotGridStrategy(self.spot_config)
        strategy.initialize_zones(Decimal("0.5"), self.context)

        self.assertEqual(strategy.zones[0].order_side, OrderSide.SELL)
        self.assertEqual(strategy.zones[1].order_side, OrderSide.SELL)

    def test_fill_lifecycle_rust(self):
        strategy = SpotGridStrategy(self.spot_config)
        strategy.state = StrategyState.Running
        strategy.market = self.market_info

        # Manual Zone Setup
        zone = GridZone(
            index=0,
            buy_price=Decimal("1.0"),
            sell_price=Decimal("2.0"),
            size=Decimal("10.0"),
            order_side=OrderSide.BUY,
            mode=None,
            entry_price=Decimal("0.0"),
        )
        strategy.zones = [zone]

        cloid = Cloid(100)
        strategy.active_order_map[cloid] = zone
        zone.cloid = cloid

        fill = OrderFill(
            side=OrderSide.BUY,
            size=Decimal("10.0"),
            price=Decimal("1.0"),
            fee=Decimal("0.1"),
            cloid=cloid,
        )

        strategy.on_order_filled(fill, self.context)

        self.assertEqual(zone.order_side, OrderSide.SELL)
        self.assertEqual(zone.entry_price, Decimal("1.0"))
        self.assertEqual(strategy.inventory_base, Decimal("10.0"))

        args = self.context.place_order.call_args[0][0]
        self.assertEqual(args.side, OrderSide.SELL)
        self.assertEqual(args.price, Decimal("2.0"))  # Upper price

        self.assertEqual(strategy.inventory_base, Decimal("10.0"))

    def test_retry_mechanism(self):
        strategy = SpotGridStrategy(self.spot_config)
        strategy.state = StrategyState.Running

        # Setup Zone
        zone = GridZone(
            index=0,
            buy_price=Decimal("1.0"),
            sell_price=Decimal("2.0"),
            size=Decimal("10.0"),
            order_side=OrderSide.BUY,
            mode=None,
        )
        strategy.zones = [zone]

        # Test Failure Increment
        fail_cloid = Cloid(999)
        strategy.active_order_map[fail_cloid] = zone
        zone.cloid = fail_cloid

        # Simulate Failure
        failure = MagicMock()
        failure.cloid = fail_cloid
        failure.failure_reason = "Test Failure"

        strategy.on_order_failed(failure, self.context)

        self.assertEqual(zone.retry_count, 1)
        self.assertIsNone(zone.cloid)

        # Simulate Max Retries
        from src.constants import MAX_ORDER_RETRIES

        zone.retry_count = MAX_ORDER_RETRIES

        # Should NOT place order if max retries reached
        strategy.refresh_orders(self.context)
        self.assertEqual(self.context.place_order.call_count, 0)

        # Simulate Reset on Fill
        zone.retry_count = 2
        strategy.active_order_map[fail_cloid] = zone
        zone.cloid = fail_cloid

        fill = OrderFill(
            side=OrderSide.BUY,
            size=Decimal("10.0"),
            price=Decimal("1.0"),
            fee=Decimal("0.1"),
            cloid=fail_cloid,
        )
        strategy.on_order_filled(fill, self.context)
        self.assertEqual(zone.retry_count, 0)

    def test_get_summary(self):
        strategy = SpotGridStrategy(self.spot_config)
        strategy.initialize_zones(Decimal("1.6"), self.context)

        summary = strategy.get_summary(self.context)

        self.assertEqual(summary.base_balance, strategy.inventory_base)
        self.assertEqual(summary.quote_balance, strategy.inventory_quote)
        self.assertEqual(summary.base_balance, Decimal("0.0"))
        self.assertGreater(summary.quote_balance, Decimal("0.0"))

    def test_initialization_out_of_range_fallback(self):
        def get_balance(asset):
            if asset == "LIT":
                return Decimal("0.0")
            if asset == "USDC":
                return Decimal("200.0")
            return Decimal("0.0")

        self.context.get_spot_available.side_effect = get_balance

        strategy = SpotGridStrategy(self.spot_config)

        # Should not raise error, but place order with markdown
        # Price 0.5 is below grid (1.0 - 2.0)
        strategy.initialize_zones(Decimal("0.5"), self.context)

        self.assertEqual(self.context.place_order.call_count, 1)
        args = self.context.place_order.call_args[0][0]
        self.assertEqual(args.side, OrderSide.BUY)
        # 0.5 * (1 - 0.001) = 0.4995. Rounding is truncation (ROUND_DOWN) for Precision(4)?
        # 0.4995 -> 0.4995.
        self.assertAlmostEqual(args.price, Decimal("0.4995"))

    def test_acquisition_updates_equity(self):
        strategy = SpotGridStrategy(self.spot_config)
        # 1. Initialize with deficit (mocked via context if possible, or just force state)
        # Force state to AcquiringAssets
        strategy.state = StrategyState.AcquiringAssets
        strategy.initial_equity = Decimal("0")  # Pre-acquisition value

        # 2. Simulate Acquisition Fill
        fill = OrderFill(
            side=OrderSide.BUY,
            size=Decimal("10.0"),
            price=Decimal("1.5"),
            fee=Decimal("0.1"),
            cloid=Cloid(999),
        )

        # Mock requirements
        strategy.required_base = Decimal("10.0")
        strategy.required_quote = Decimal("100.0")
        strategy.initial_avail_base = Decimal("0.0")
        strategy.initial_avail_quote = Decimal("1000.0")

        strategy.acquisition_cloid = fill.cloid
        strategy.current_price = fill.price  # Ensure MTM is correct

        # 3. Handle Fill
        strategy.on_order_filled(fill, self.context)

        # 4. Verify Initial Equity updated
        # Inventory Base = 10.0, Price = 1.5. Value = 15.0.
        # Inventory Quote = min(1000 - 15, 100) = 100.0.
        # Wait, handle_acquisition_fill logic:
        # new_real_quote = 1000 - (10 * 1.5) = 1000 - 15 = 985.
        # inventory_quote = min(985, 100) = 100.
        # Initial Equity = (10 * 1.5) + 100 = 115.0.

        self.assertEqual(strategy.initial_equity, Decimal("115.0"))

        # Verify Profit is negligible (only fees)
        # Current Equity = (10 * 1.5) + 100 = 115.
        # Total Profit = 115 - 115 - 0.1 = -0.1.

        summary = strategy.get_summary(self.context)
        self.assertEqual(summary.total_profit, Decimal("-0.1"))

    def test_trigger_buy_above_waits_then_acquires_on_trigger(self):
        trigger_config = SpotGridConfig(
            symbol="LIT/USDC",
            grid_range_high=Decimal("2.0"),
            grid_range_low=Decimal("1.0"),
            grid_type=GridType.ARITHMETIC,
            grid_count=4,
            total_investment=Decimal("120.0"),
            trigger_price=Decimal("1.5"),
            type="spot_grid",
        )

        def get_balance(asset):
            if asset == "LIT":
                return Decimal("0")
            if asset == "USDC":
                return Decimal("1000")
            return Decimal("0")

        self.context.get_spot_available.side_effect = get_balance

        strategy = SpotGridStrategy(trigger_config)
        strategy.initialize_zones(Decimal("1.52"), self.context)

        self.assertEqual(strategy.state, StrategyState.WaitingForTrigger)
        self.assertEqual(self.context.place_order.call_count, 0)

        strategy.on_tick(Decimal("1.49"), self.context)

        self.assertEqual(strategy.state, StrategyState.AcquiringAssets)
        self.assertEqual(self.context.place_order.call_count, 1)
        req = self.context.place_order.call_args[0][0]
        self.assertEqual(req.side, OrderSide.BUY)

    def test_trigger_sell_below_waits_then_acquires_on_trigger(self):
        trigger_config = SpotGridConfig(
            symbol="LIT/USDC",
            grid_range_high=Decimal("2.0"),
            grid_range_low=Decimal("1.0"),
            grid_type=GridType.ARITHMETIC,
            grid_count=4,
            total_investment=Decimal("120.0"),
            trigger_price=Decimal("1.5"),
            type="spot_grid",
        )

        def get_balance(asset):
            if asset == "LIT":
                return Decimal("1000")
            if asset == "USDC":
                return Decimal("0")
            return Decimal("0")

        self.context.get_spot_available.side_effect = get_balance

        strategy = SpotGridStrategy(trigger_config)
        strategy.initialize_zones(Decimal("1.48"), self.context)

        self.assertEqual(strategy.state, StrategyState.WaitingForTrigger)
        self.assertEqual(self.context.place_order.call_count, 0)

        strategy.on_tick(Decimal("1.51"), self.context)

        self.assertEqual(strategy.state, StrategyState.AcquiringAssets)
        self.assertEqual(self.context.place_order.call_count, 1)
        req = self.context.place_order.call_args[0][0]
        self.assertEqual(req.side, OrderSide.SELL)

    def test_trigger_equal_price_acquires_immediately(self):
        trigger_config = SpotGridConfig(
            symbol="LIT/USDC",
            grid_range_high=Decimal("2.0"),
            grid_range_low=Decimal("1.0"),
            grid_type=GridType.ARITHMETIC,
            grid_count=4,
            total_investment=Decimal("120.0"),
            trigger_price=Decimal("1.5"),
            type="spot_grid",
        )

        def get_balance(asset):
            if asset == "LIT":
                return Decimal("0")
            if asset == "USDC":
                return Decimal("1000")
            return Decimal("0")

        self.context.get_spot_available.side_effect = get_balance

        strategy = SpotGridStrategy(trigger_config)
        strategy.initialize_zones(Decimal("1.5"), self.context)

        self.assertEqual(strategy.state, StrategyState.AcquiringAssets)
        self.assertEqual(self.context.place_order.call_count, 1)
        req = self.context.place_order.call_args[0][0]
        self.assertEqual(req.side, OrderSide.BUY)

    def test_trigger_buy_below_uses_grid_price_not_trigger_price(self):
        trigger_config = SpotGridConfig(
            symbol="LIT/USDC",
            grid_range_high=Decimal("2.0"),
            grid_range_low=Decimal("1.0"),
            grid_type=GridType.ARITHMETIC,
            grid_count=4,
            total_investment=Decimal("120.0"),
            trigger_price=Decimal("1.5"),
            type="spot_grid",
        )

        def get_balance(asset):
            if asset == "LIT":
                return Decimal("0")
            if asset == "USDC":
                return Decimal("1000")
            return Decimal("0")

        self.context.get_spot_available.side_effect = get_balance

        strategy = SpotGridStrategy(trigger_config)
        strategy.initialize_zones(Decimal("1.48"), self.context)

        self.assertEqual(strategy.state, StrategyState.AcquiringAssets)
        req = self.context.place_order.call_args[0][0]

        expected_price = max(
            z.buy_price for z in strategy.zones if z.buy_price < strategy.current_price
        )
        self.assertEqual(req.side, OrderSide.BUY)
        self.assertEqual(req.price, expected_price)
        self.assertNotEqual(req.price, trigger_config.trigger_price)

    def test_post_fill_waits_if_trigger_still_not_hit(self):
        trigger_config = SpotGridConfig(
            symbol="LIT/USDC",
            grid_range_high=Decimal("2.0"),
            grid_range_low=Decimal("1.0"),
            grid_type=GridType.ARITHMETIC,
            grid_count=4,
            total_investment=Decimal("120.0"),
            trigger_price=Decimal("1.5"),
            type="spot_grid",
        )

        def get_balance(asset):
            if asset == "LIT":
                return Decimal("0")
            if asset == "USDC":
                return Decimal("1000")
            return Decimal("0")

        self.context.get_spot_available.side_effect = get_balance

        strategy = SpotGridStrategy(trigger_config)
        strategy.initialize_zones(Decimal("1.48"), self.context)
        self.assertEqual(strategy.state, StrategyState.AcquiringAssets)

        acquisition_cloid = strategy.acquisition_cloid
        self.assertIsNotNone(acquisition_cloid)

        fill = OrderFill(
            side=OrderSide.BUY,
            size=strategy.acquisition_target_size,
            price=Decimal("1.48"),
            fee=Decimal("0.1"),
            cloid=acquisition_cloid,
        )
        strategy.on_order_filled(fill, self.context)

        self.assertEqual(strategy.state, StrategyState.WaitingForTrigger)
        self.assertIsNone(strategy.acquisition_cloid)


if __name__ == "__main__":
    unittest.main()
