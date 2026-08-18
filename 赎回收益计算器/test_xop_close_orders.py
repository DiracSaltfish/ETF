from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, datetime, time

from xop_close_orders import (
    NEW_YORK,
    build_ib_order,
    build_xop_contract,
    generate_order_specs,
    parse_trade_date,
    validate_order_specs,
    validate_future_trigger,
)


class XopCloseOrdersTest(unittest.TestCase):
    def test_default_template(self) -> None:
        specs = generate_order_specs(date(2026, 7, 6), 990)
        self.assertEqual([item.quantity for item in specs], [200, 200, 200, 200, 190])
        self.assertEqual(
            [item.trigger_time.strftime("%H:%M:%S") for item in specs],
            ["15:58:45", "15:58:52", "15:59:00", "15:59:07", "15:59:15"],
        )
        self.assertEqual(sum(item.quantity for item in specs), 990)
        self.assertEqual(specs[0].condition_time, "20260706 15:58:45 US/Eastern")
        self.assertEqual(specs[-1].order_ref, "XOP_REDEEM_CLOSE_20260706_155915_190")

    def test_two_basket_template(self) -> None:
        specs = generate_order_specs(date(2026, 7, 6), 1980, basket_count=2)
        self.assertEqual([item.quantity for item in specs], [200] * 9 + [180])
        self.assertEqual(
            [item.trigger_time.strftime("%H:%M:%S") for item in specs],
            [
                "15:58:45",
                "15:58:48",
                "15:58:52",
                "15:58:55",
                "15:58:58",
                "15:59:02",
                "15:59:05",
                "15:59:08",
                "15:59:12",
                "15:59:15",
            ],
        )
        self.assertEqual(sum(item.quantity for item in specs), 1980)
        self.assertEqual(specs[-1].condition_time, "20260706 15:59:15 US/Eastern")
        self.assertEqual(specs[-1].order_ref, "XOP_REDEEM_CLOSE_20260706_155915_180")

    def test_custom_quantity_keeps_first_four_slices(self) -> None:
        specs = generate_order_specs(date(2026, 7, 6), 865)
        self.assertEqual([item.quantity for item in specs], [200, 200, 200, 200, 65])

    def test_invalid_date_and_quantity_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_trade_date("2026-07-06")
        with self.assertRaises(ValueError):
            generate_order_specs(date(2026, 7, 5), 990)
        with self.assertRaises(ValueError):
            generate_order_specs(date(2026, 7, 6), 800)
        with self.assertRaises(ValueError):
            generate_order_specs(date(2026, 7, 6), 1800, basket_count=2)
        with self.assertRaises(ValueError):
            generate_order_specs(date(2026, 7, 6), 1980, basket_count=3)

    def test_expired_trigger_is_rejected(self) -> None:
        spec = generate_order_specs(date(2026, 7, 6), 990)[0]
        with self.assertRaises(ValueError):
            validate_future_trigger(spec, now=datetime(2026, 7, 6, 15, 58, 45, tzinfo=NEW_YORK))
        validate_future_trigger(spec, now=datetime(2026, 7, 6, 15, 58, tzinfo=NEW_YORK))

    def test_trigger_after_safe_close_buffer_is_rejected(self) -> None:
        specs = generate_order_specs(date(2026, 7, 6), 990)
        invalid = (*specs[:-1], replace(specs[-1], trigger_time=time(15, 59, 16)))
        with self.assertRaises(ValueError):
            validate_order_specs(invalid, 990)

    def test_ib_objects_match_fixed_template(self) -> None:
        spec = generate_order_specs(date(2026, 7, 6), 990)[0]
        contract = build_xop_contract()
        order = build_ib_order(spec)
        self.assertEqual(contract.symbol, "XOP")
        self.assertEqual(contract.secType, "STK")
        self.assertEqual(contract.exchange, "SMART")
        self.assertEqual(contract.primaryExchange, "ARCA")
        self.assertEqual(order.action, "BUY")
        self.assertEqual(order.orderType, "MKT")
        self.assertEqual(order.totalQuantity, 200)
        self.assertEqual(order.tif, "DAY")
        self.assertFalse(order.outsideRth)
        self.assertTrue(order.transmit)
        self.assertFalse(order.conditionsIgnoreRth)
        self.assertFalse(order.conditionsCancelOrder)
        self.assertEqual(len(order.conditions), 1)
        self.assertTrue(order.conditions[0].isMore)
        self.assertEqual(order.conditions[0].time, "20260706 15:58:45 US/Eastern")


if __name__ == "__main__":
    unittest.main()
