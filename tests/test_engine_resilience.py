from decimal import Decimal
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock

from src.config import ExchangeConfig, SpotGridConfig
from src.constants import MAX_BATCH_SIZE
from src.engine.context import MarketInfo, StrategyContext
from src.engine.engine import Engine
from src.model import LimitOrderRequest, OrderSide, PendingOrder
from src.strategy.types import GridType


class TestEngineResilience(IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = SpotGridConfig(
            symbol="LIT/USDC",
            upper_price=Decimal("2.0"),
            lower_price=Decimal("1.0"),
            grid_type=GridType.ARITHMETIC,
            grid_count=10,
            total_investment=Decimal("100.0"),
        )

        self.exchange_config = ExchangeConfig(
            master_account_address="0xabc",
            account_index=1,
            agent_private_key="test-key",
            agent_key_index=0,
            network="testnet",
            base_url="https://example.com",
        )

        self.strategy = MagicMock()
        self.engine = Engine(self.config, self.exchange_config, self.strategy)

        market = MarketInfo(
            symbol=self.config.symbol,
            coin="LIT",
            market_id=2049,
            sz_decimals=2,
            price_decimals=4,
            market_type="spot",
            base_asset_id=1,
            quote_asset_id=2,
            min_base_amount=Decimal("0.1"),
            min_quote_amount=Decimal("1.0"),
        )

        self.engine.ctx = StrategyContext({self.config.symbol: market})
        self.engine.market_map[self.config.symbol] = 2049
        self.engine.reverse_market_map[2049] = self.config.symbol
        self.engine.api_client = MagicMock()
        self.engine.account_index = 1

    def _queue_limit_orders(self, count: int):
        assert self.engine.ctx is not None
        for _ in range(count):
            self.engine.ctx.place_order(
                LimitOrderRequest(
                    symbol=self.config.symbol,
                    side=OrderSide.BUY,
                    price=Decimal("1.2345"),
                    sz=Decimal("1.0"),
                    reduce_only=False,
                )
            )

    def _configure_signer(self, send_side_effect):
        signer = MagicMock()
        signer.nonce_manager = MagicMock()
        signer.nonce_manager.next_nonce.return_value = (0, 1000)

        signer.ORDER_TYPE_LIMIT = 1
        signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME = 1
        signer.ORDER_TYPE_MARKET = 2
        signer.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL = 3

        signer.sign_create_order.return_value = (1, "signed", None, None)
        signer.sign_cancel_order.return_value = (2, "signed_cancel", None, None)
        signer.send_tx_batch = AsyncMock(side_effect=send_side_effect)

        self.engine.signer_client = signer
        return signer

    async def test_process_order_queue_skips_and_preserves_queue_in_degraded_mode(self):
        self._queue_limit_orders(1)
        assert self.engine.ctx is not None

        self.engine.degraded_mode = True
        before = len(self.engine.ctx.order_queue)

        await self.engine.process_order_queue()

        self.assertEqual(len(self.engine.ctx.order_queue), before)
        self.assertEqual(len(self.engine.pending_orders), 0)

    async def test_partial_batch_failure_requeues_remaining_and_enters_degraded_mode(
        self,
    ):
        total_orders = MAX_BATCH_SIZE + 1
        self._queue_limit_orders(total_orders)

        self._configure_signer(
            [
                SimpleNamespace(
                    code=200,
                    tx_hash=["ok"] * MAX_BATCH_SIZE,
                    message="accepted",
                ),
                Exception("(502)"),
            ]
        )

        await self.engine.process_order_queue()

        assert self.engine.ctx is not None
        self.assertEqual(len(self.engine.pending_orders), MAX_BATCH_SIZE)
        self.assertEqual(len(self.engine.ctx.order_queue), 1)
        self.assertTrue(self.engine.degraded_mode)
        self.strategy.on_order_failed.assert_not_called()

    async def test_non_transient_batch_reject_reports_failures_without_requeue(self):
        self._queue_limit_orders(1)

        self._configure_signer(
            [SimpleNamespace(code=400, tx_hash=[], message="bad request")]
        )

        await self.engine.process_order_queue()

        assert self.engine.ctx is not None
        self.assertEqual(len(self.engine.pending_orders), 0)
        self.assertEqual(len(self.engine.ctx.order_queue), 0)
        self.assertFalse(self.engine.degraded_mode)

        self.strategy.on_order_failed.assert_called_once()
        failure = self.strategy.on_order_failed.call_args[0][0]
        self.assertEqual(failure.failure_reason, "batch_rejected_400")

    async def test_reconciliation_fetch_failure_enters_degraded_mode(self):
        assert self.engine.ctx is not None

        order = LimitOrderRequest(
            symbol=self.config.symbol,
            side=OrderSide.BUY,
            price=Decimal("1.2345"),
            sz=Decimal("1.0"),
            reduce_only=False,
        )
        cloid = self.engine.ctx.place_order(order)
        self.engine.pending_orders[cloid] = PendingOrder(
            target_size=order.sz,
            side=order.side,
            price=order.price,
            created_at=0.0,
        )

        active_orders_mock = AsyncMock(return_value=None)
        object.__setattr__(self.engine, "get_active_orders", active_orders_mock)

        await self.engine.handle_reconciliation()

        self.assertTrue(self.engine.degraded_mode)

    async def test_degraded_mode_recovers_with_no_pending_orders_after_probe(self):
        self.engine.pending_orders.clear()
        self.engine.degraded_mode = True
        self.engine.degraded_until_ts = 0.0
        self.engine.healthy_reconciliation_snapshots = 0

        active_orders_mock = AsyncMock(return_value=[])
        inactive_orders_mock = AsyncMock(return_value=[])
        object.__setattr__(self.engine, "get_active_orders", active_orders_mock)
        object.__setattr__(self.engine, "get_inactive_orders", inactive_orders_mock)

        await self.engine.handle_reconciliation()

        self.assertFalse(self.engine.degraded_mode)
        self.assertGreaterEqual(self.engine.healthy_reconciliation_snapshots, 1)
        active_orders_mock.assert_awaited_once()
        inactive_orders_mock.assert_awaited_once()

    async def test_reconciliation_requires_two_misses_before_marking_lost(self):
        assert self.engine.ctx is not None

        order = LimitOrderRequest(
            symbol=self.config.symbol,
            side=OrderSide.SELL,
            price=Decimal("1.4000"),
            sz=Decimal("1.25"),
            reduce_only=False,
        )
        cloid = self.engine.ctx.place_order(order)
        self.engine.pending_orders[cloid] = PendingOrder(
            target_size=order.sz,
            side=order.side,
            filled_size=Decimal("0"),
            weighted_avg_px=Decimal("0"),
            accumulated_fees=Decimal("0"),
            reduce_only=order.reduce_only,
            created_at=0.0,
            price=order.price,
        )

        active_orders_mock = AsyncMock(return_value=[])
        inactive_orders_mock = AsyncMock(return_value=[])
        object.__setattr__(self.engine, "get_active_orders", active_orders_mock)
        object.__setattr__(self.engine, "get_inactive_orders", inactive_orders_mock)

        await self.engine.handle_reconciliation()
        self.assertIn(cloid, self.engine.pending_orders)
        self.strategy.on_order_failed.assert_not_called()

        await self.engine.handle_reconciliation()
        self.assertNotIn(cloid, self.engine.pending_orders)
        self.strategy.on_order_failed.assert_called_once()
        failure = self.strategy.on_order_failed.call_args[0][0]
        self.assertEqual(failure.failure_reason, "lost_in_reconciliation")

    async def test_reconciliation_empty_snapshots_do_not_trigger_lost_cascade(self):
        assert self.engine.ctx is not None

        for _ in range(5):
            order = LimitOrderRequest(
                symbol=self.config.symbol,
                side=OrderSide.BUY,
                price=Decimal("1.2000"),
                sz=Decimal("1.00"),
                reduce_only=False,
            )
            cloid = self.engine.ctx.place_order(order)
            self.engine.pending_orders[cloid] = PendingOrder(
                target_size=order.sz,
                side=order.side,
                created_at=0.0,
                price=order.price,
            )

        active_orders_mock = AsyncMock(return_value=[])
        inactive_orders_mock = AsyncMock(return_value=[])
        object.__setattr__(self.engine, "get_active_orders", active_orders_mock)
        object.__setattr__(self.engine, "get_inactive_orders", inactive_orders_mock)

        await self.engine.handle_reconciliation()

        self.assertTrue(self.engine.degraded_mode)
        self.assertEqual(len(self.engine.pending_orders), 5)
        self.strategy.on_order_failed.assert_not_called()

    async def test_reconciliation_filled_uses_inactive_fill_amount_and_price(self):
        assert self.engine.ctx is not None

        order = LimitOrderRequest(
            symbol=self.config.symbol,
            side=OrderSide.BUY,
            price=Decimal("1.3000"),
            sz=Decimal("2.0"),
            reduce_only=False,
        )
        cloid = self.engine.ctx.place_order(order)
        self.engine.pending_orders[cloid] = PendingOrder(
            target_size=order.sz,
            side=order.side,
            filled_size=Decimal("0"),
            weighted_avg_px=Decimal("0"),
            reduce_only=order.reduce_only,
            created_at=0.0,
            price=order.price,
        )

        active_orders_mock = AsyncMock(return_value=[])
        inactive_orders_mock = AsyncMock(
            return_value=[
                SimpleNamespace(
                    client_order_index=cloid.as_int(),
                    status="filled",
                    filled_base_amount="1.5",
                    filled_quote_amount="2.25",
                )
            ]
        )
        object.__setattr__(self.engine, "get_active_orders", active_orders_mock)
        object.__setattr__(self.engine, "get_inactive_orders", inactive_orders_mock)

        await self.engine.handle_reconciliation()

        self.assertNotIn(cloid, self.engine.pending_orders)
        self.strategy.on_order_filled.assert_called_once()
        fill = self.strategy.on_order_filled.call_args[0][0]
        self.assertEqual(fill.size, Decimal("1.5"))
        self.assertEqual(fill.price, Decimal("1.5"))
        self.assertEqual(fill.cloid, cloid)

    async def test_reconciliation_filled_fallback_uses_target_size_and_limit_price(
        self,
    ):
        assert self.engine.ctx is not None

        order = LimitOrderRequest(
            symbol=self.config.symbol,
            side=OrderSide.BUY,
            price=Decimal("1.3010"),
            sz=Decimal("2.0"),
            reduce_only=False,
        )
        cloid = self.engine.ctx.place_order(order)
        self.engine.pending_orders[cloid] = PendingOrder(
            target_size=order.sz,
            side=order.side,
            filled_size=Decimal("0"),
            weighted_avg_px=Decimal("0"),
            reduce_only=order.reduce_only,
            created_at=0.0,
            price=order.price,
        )

        active_orders_mock = AsyncMock(return_value=[])
        inactive_orders_mock = AsyncMock(
            return_value=[
                SimpleNamespace(
                    client_order_index=cloid.as_int(),
                    status="filled",
                    filled_base_amount="0",
                    filled_quote_amount="0",
                )
            ]
        )
        object.__setattr__(self.engine, "get_active_orders", active_orders_mock)
        object.__setattr__(self.engine, "get_inactive_orders", inactive_orders_mock)

        await self.engine.handle_reconciliation()

        self.strategy.on_order_filled.assert_called_once()
        fill = self.strategy.on_order_filled.call_args[0][0]
        self.assertEqual(fill.size, order.sz)
        self.assertEqual(fill.price, order.price)
