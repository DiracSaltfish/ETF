#!/usr/bin/env python3

import json
import struct
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from etf_monitor_server import ConfigStore, MonitorEngine, create_app
from wind_etf_realtime_ui import SubscriptionSession


HERE = Path(__file__).resolve().parent


def make_capture(windcode: str, buy_amount: int, sell_amount: int) -> dict:
    with (HERE / "sample_wind_tbapi_probe_sub_1.json").open(
        "r", encoding="utf-8"
    ) as handle:
        capture = json.load(handle)
    raw = bytearray.fromhex(capture["buffer_58"]["hex"])
    base = 12
    struct.pack_into("<q", raw, base + 4, buy_amount)
    struct.pack_into("<q", raw, base + 24, sell_amount)
    code = windcode.encode("ascii")
    raw[base + 40 : base + 104] = code + bytes(64 - len(code))
    capture["buffer_58"]["hex"] = raw.hex()
    capture["callback_epoch_ms"] = int(time.time() * 1000)
    return capture


class FakeController:
    def subscribe_many(self, symbols, latency_ms):
        sessions = {
            symbol: SubscriptionSession(symbol, 123, Path(f"/{symbol}.dylib"), index + 1)
            for index, symbol in enumerate(symbols)
        }
        return sessions, {}

    def stop_many(self, sessions):
        return {}


class MonitorServerTest(unittest.TestCase):
    def test_auth_snapshot_websocket_change_and_control(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "symbols": ["159518"],
                        "network": {"host": "127.0.0.1", "port": 6787},
                        "schedule": {"enabled": False},
                        "pcf": {"enabled": False},
                    }
                ),
                encoding="utf-8",
            )
            capture_path = root / "wind_tbapi_live_159518_SZ.json"
            capture_path.write_text(
                json.dumps(make_capture("159518.SZ", 3_000_000, 2_000_000)),
                encoding="utf-8",
            )
            app = create_app(config_path, capture_dir=root, controller=FakeController())
            with TestClient(app) as client:
                web = client.get("/")
                self.assertEqual(web.status_code, 200)
                self.assertIn("ETF 变化监控", web.text)
                self.assertIn("启用声音", web.text)
                self.assertEqual(client.get("/api/v1/health").status_code, 200)
                snapshot = client.get("/api/v1/snapshot")
                self.assertEqual(snapshot.status_code, 200)
                item = snapshot.json()["items"][0]
                self.assertEqual(item["values"]["netamount"], 1_000_000)
                self.assertRegex(item["updated_at"], r"^\d{2}:\d{2}:\d{2}$")

                started = client.post("/api/v1/monitor/start")
                self.assertEqual(started.status_code, 200)
                self.assertTrue(started.json()["monitoring"])

                with client.websocket_connect(
                    "/ws/v1/changes"
                ) as websocket:
                    initial = websocket.receive_json()
                    self.assertEqual(initial["type"], "snapshot")
                    capture_path.write_text(
                        json.dumps(make_capture("159518.SZ", 4_000_000, 2_000_000)),
                        encoding="utf-8",
                    )
                    websocket.send_json({"type": "get_snapshot"})
                    first = websocket.receive_json()
                    second = websocket.receive_json()
                    events = {first["type"]: first, second["type"]: second}
                    self.assertIn("change", events)
                    self.assertIn("snapshot", events)
                    changes = events["change"]["items"][0]["changes"]
                    fields = {change["field"] for change in changes}
                    self.assertEqual(fields, {"etfbuyamount"})

                stopped = client.post("/api/v1/monitor/stop")
                self.assertEqual(stopped.status_code, 200)
                self.assertFalse(stopped.json()["monitoring"])

    def test_only_purchase_and_redemption_shares_trigger_change_events(self) -> None:
        previous = {
            "etfbuyamount": 3_000_000,
            "etfsellamount": 2_000_000,
            "netamount": 1_000_000,
            "etfbuymoney": 0,
            "etfsellmoney": 0,
        }
        derived_or_money_only = dict(
            previous,
            netamount=9_999_999,
            etfbuymoney=123,
            etfsellmoney=456,
        )
        self.assertEqual(
            MonitorEngine._change_details(previous, derived_or_money_only), []
        )

        purchase_changed = dict(previous, etfbuyamount=4_000_000, netamount=2_000_000)
        details = MonitorEngine._change_details(previous, purchase_changed)
        self.assertEqual([detail["field"] for detail in details], ["etfbuyamount"])


if __name__ == "__main__":
    unittest.main()
