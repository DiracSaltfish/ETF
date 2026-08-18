from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, datetime, time

import xop_close_orders as orders


class XopCloseOrdersTest(unittest.TestCase):
    def test_redemption_buy_template_is_unchanged(self) -> None:
        specs = orders.generate_order_specs(date(2030, 1, 7), 990)

        self.assertEqual([item.quantity for item in specs], [200, 200, 200, 200, 190])
        self.assertEqual([item.action for item in specs], ["BUY"] * 5)
        self.assertEqual(
            [item.trigger_time.strftime("%H:%M:%S") for item in specs],
            ["15:58:45", "15:58:52", "15:59:00", "15:59:07", "15:59:15"],
        )
        self.assertEqual(specs[-1].order_ref, "XOP_REDEEM_CLOSE_20300107_155915_190")

        account_specs = orders.generate_order_specs(
            date(2030, 1, 7), 990, account="U1234567"
        )
        self.assertEqual(
            account_specs[-1].order_ref,
            "XOP_REDEEM_CLOSE_20300107_155915_190",
        )

    def test_creation_sell_balances_four_orders(self) -> None:
        specs = orders.generate_order_specs(
            date(2030, 1, 7),
            990,
            intent=orders.XopCloseIntent.CREATION_SELL,
            trigger_times=orders.creation_trigger_times("balanced"),
            account="U1234567",
        )

        self.assertEqual([item.quantity for item in specs], [248, 248, 247, 247])
        self.assertEqual([item.action for item in specs], ["SELL"] * 4)
        self.assertEqual(
            [item.trigger_time.strftime("%H:%M:%S") for item in specs],
            ["15:59:00", "15:59:15", "15:59:30", "15:59:45"],
        )
        self.assertEqual(
            specs[0].order_ref,
            "XOP_CREATE_CLOSE_20300107_155900_248_U1234567",
        )

    def test_redemption_buy_can_use_four_balanced_orders(self) -> None:
        specs = orders.generate_order_specs(
            date(2030, 1, 7),
            990,
            intent=orders.XopCloseIntent.REDEMPTION_BUY,
            trigger_times=orders.four_order_trigger_times("balanced"),
        )

        self.assertEqual([item.quantity for item in specs], [248, 248, 247, 247])
        self.assertEqual([item.action for item in specs], ["BUY"] * 4)
        self.assertEqual(
            [item.trigger_time.strftime("%H:%M:%S") for item in specs],
            ["15:59:00", "15:59:15", "15:59:30", "15:59:45"],
        )
        self.assertEqual(
            specs[0].order_ref,
            "XOP_REDEEM_CLOSE_20300107_155900_248",
        )

    def test_redemption_buy_can_use_four_conservative_orders(self) -> None:
        specs = orders.generate_order_specs(
            date(2030, 1, 7),
            990,
            intent=orders.XopCloseIntent.REDEMPTION_BUY,
            trigger_times=orders.four_order_trigger_times("conservative"),
        )
        self.assertEqual(
            [item.trigger_time.strftime("%H:%M:%S") for item in specs],
            ["15:58:30", "15:58:45", "15:59:00", "15:59:15"],
        )

    def test_creation_sell_scales_baskets_before_balancing(self) -> None:
        specs = orders.generate_order_specs(
            date(2030, 1, 7),
            12 * 990,
            basket_count=12,
            intent=orders.XopCloseIntent.CREATION_SELL,
            trigger_times=orders.creation_trigger_times("balanced"),
        )
        self.assertEqual([item.quantity for item in specs], [2970] * 4)

    def test_conservative_creation_time_plan(self) -> None:
        specs = orders.generate_order_specs(
            date(2030, 1, 7),
            990,
            intent=orders.XopCloseIntent.CREATION_SELL,
            trigger_times=orders.creation_trigger_times("conservative"),
        )
        self.assertEqual(
            [item.trigger_time.strftime("%H:%M:%S") for item in specs],
            ["15:58:30", "15:58:45", "15:59:00", "15:59:15"],
        )

    def test_creation_rejects_non_preset_trigger_count(self) -> None:
        with self.assertRaisesRegex(ValueError, "4个美东触发时间"):
            orders.generate_order_specs(
                date(2030, 1, 7),
                990,
                intent=orders.XopCloseIntent.CREATION_SELL,
                trigger_times=(time(15, 59), time(15, 59, 15), time(15, 59, 30)),
            )

    def test_redemption_rejects_non_preset_four_times(self) -> None:
        with self.assertRaisesRegex(ValueError, "预设的4个美东触发时间"):
            orders.generate_order_specs(
                date(2030, 1, 7),
                990,
                intent=orders.XopCloseIntent.REDEMPTION_BUY,
                trigger_times=(
                    time(15, 58, 55),
                    time(15, 59, 10),
                    time(15, 59, 25),
                    time(15, 59, 40),
                ),
            )

    def test_mixed_intents_and_invalid_creation_times_are_rejected(self) -> None:
        redemption = orders.generate_order_specs(date(2030, 1, 7), 990)
        mixed = (*redemption[:-1], replace(redemption[-1], intent=orders.XopCloseIntent.CREATION_SELL))
        with self.assertRaises(ValueError):
            orders.validate_order_specs(mixed, 990)

        creation = orders.generate_order_specs(
            date(2030, 1, 7),
            990,
            intent=orders.XopCloseIntent.CREATION_SELL,
        )
        invalid = (*creation[:-1], replace(creation[-1], trigger_time=time(15, 59, 46)))
        with self.assertRaises(ValueError):
            orders.validate_order_specs(invalid, 990, expected_count=4)

    def test_ib_order_uses_strategy_action_and_account(self) -> None:
        spec = orders.generate_order_specs(
            date(2030, 1, 7),
            990,
            intent=orders.XopCloseIntent.CREATION_SELL,
            account="U1234567",
        )[0]
        order = orders.build_ib_order(spec)

        self.assertEqual(order.action, "SELL")
        self.assertEqual(order.account, "U1234567")
        self.assertEqual(order.orderType, "MKT")
        self.assertEqual(order.totalQuantity, 248)
        self.assertEqual(order.conditions[0].time, "20300107 15:59:00 US/Eastern")
        self.assertFalse(order.outsideRth)

    def test_expired_trigger_is_rejected(self) -> None:
        spec = orders.generate_order_specs(
            date(2030, 1, 7),
            990,
            intent=orders.XopCloseIntent.CREATION_SELL,
        )[0]
        with self.assertRaises(ValueError):
            orders.validate_future_trigger(
                spec,
                now=datetime(2030, 1, 7, 15, 59, tzinfo=orders.NEW_YORK),
            )


if __name__ == "__main__":
    unittest.main()
