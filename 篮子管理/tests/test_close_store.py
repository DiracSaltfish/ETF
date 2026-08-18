from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from basket_models import BasketDocument, BasketItem, SubmittedOrder
from close_only import build_close_only_plan
from close_store import (
    ensure_campaign,
    load_active_campaign_basis,
    load_active_campaign_orders,
    load_store,
    mark_campaign_complete,
    record_event,
    record_submitted_order,
)
from tests.test_close_only import ACCOUNT, neutral_positions


class CloseStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store_path = Path(self.temp_dir.name) / "sessions.json"
        self.env = patch.dict(os.environ, {"BASKET_CLOSE_STORE_PATH": str(self.store_path)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.basket = BasketDocument(
            Path("/tmp/store_basket.xlsx"),
            "store",
            (
                BasketItem("XOP", "BUY", 200),
                BasketItem("AAA", "SELL", 100),
                BasketItem("BBB", "SELL", 50),
            ),
        )
        self.plan = build_close_only_plan(
            self.basket,
            neutral_positions(),
            account=ACCOUNT,
            tranche_percent=25,
        )

    def test_campaign_and_order_audit_are_persisted(self) -> None:
        ensure_campaign(self.plan, basket_path=str(self.basket.path))
        record_event(self.plan.basis.campaign_id, event="TEST", message="before order")
        order = SubmittedOrder(
            symbol="AAA",
            action="BUY",
            quantity=25,
            order_type="LMT",
            tif="DAY",
            limit_price=10.0,
            order_id=12,
            perm_id=34,
            status="Submitted",
            con_id=101,
            order_ref=f"{self.plan.basis.campaign_id}-001",
        )
        record_submitted_order(self.plan.basis.campaign_id, plan_id=self.plan.plan_id, order=order)
        restored = load_active_campaign_basis(
            account=ACCOUNT,
            base_symbol="XOP",
            basket_path=str(self.basket.path),
        )

        self.assertEqual(self.plan.basis, restored)
        restored_orders = load_active_campaign_orders(
            account=ACCOUNT,
            base_symbol="XOP",
            basket_path=str(self.basket.path),
        )
        self.assertIsNotNone(restored_orders)
        self.assertEqual(order, restored_orders[1][0])
        payload = load_store()
        campaign = payload["campaigns"][0]
        self.assertEqual("WORKING", campaign["status"])
        self.assertEqual(order.order_ref, campaign["orders"][0]["order_ref"])

    def test_complete_campaign_is_not_resumed(self) -> None:
        ensure_campaign(self.plan, basket_path=str(self.basket.path))
        mark_campaign_complete(self.plan.basis.campaign_id)
        restored = load_active_campaign_basis(
            account=ACCOUNT,
            base_symbol="XOP",
            basket_path=str(self.basket.path),
        )
        self.assertIsNone(restored)

    def test_corrupt_store_fails_closed(self) -> None:
        self.store_path.write_text("{not-json", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "无法读取"):
            load_store()

    def test_wrong_store_version_fails_closed(self) -> None:
        self.store_path.write_text(
            json.dumps({"version": 999, "campaigns": []}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "格式无效"):
            load_store()

    def test_multiple_active_campaigns_fail_closed(self) -> None:
        ensure_campaign(self.plan, basket_path=str(self.basket.path))
        payload = load_store()
        duplicate = dict(payload["campaigns"][0])
        duplicate["campaign_id"] = "UW-DUPLICATE"
        duplicate["basis"] = dict(duplicate["basis"])
        duplicate["basis"]["campaign_id"] = "UW-DUPLICATE"
        payload["campaigns"].append(duplicate)
        self.store_path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "多个未完成"):
            load_active_campaign_basis(
                account=ACCOUNT,
                base_symbol="XOP",
                basket_path=str(self.basket.path),
            )


if __name__ == "__main__":
    unittest.main()
