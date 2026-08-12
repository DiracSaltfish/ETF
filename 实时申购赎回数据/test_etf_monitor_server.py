#!/usr/bin/env python3

import asyncio
import json
import struct
import tempfile
import time
import unittest
from datetime import date, time as datetime_time
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
    def test_pcf_auto_fetch_never_starts_before_0830(self) -> None:
        self.assertEqual(
            MonitorEngine._pcf_auto_fetch_start({"fetch_start": "08:00"}),
            datetime_time(8, 30),
        )
        self.assertEqual(
            MonitorEngine._pcf_auto_fetch_start({"fetch_start": "09:00"}),
            datetime_time(9, 0),
        )

    def test_default_collection_schedule_is_0915_to_1500(self) -> None:
        schedule = ConfigStore.defaults()["schedule"]
        self.assertEqual(schedule["start"], "09:15")
        self.assertEqual(schedule["stop"], "15:00")
        self.assertEqual(schedule["policy_version"], 4)

    def test_legacy_schedule_is_migrated_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "schedule": {
                            "enabled": True,
                            "policy_version": 3,
                            "start": "09:10",
                            "stop": "15:00",
                        }
                    }
                ),
                encoding="utf-8",
            )
            schedule = ConfigStore(path).data["schedule"]
            self.assertEqual(schedule["start"], "09:15")
            self.assertEqual(schedule["stop"], "15:00")
            self.assertEqual(schedule["policy_version"], 4)

    def test_legacy_pcf_start_is_persistently_migrated_to_0830(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps({"pcf": {"enabled": True, "fetch_start": "08:00"}}),
                encoding="utf-8",
            )
            pcf = ConfigStore(path).data["pcf"]
            self.assertEqual(pcf["fetch_start"], "08:30")
            self.assertEqual(pcf["policy_version"], 2)

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
            with TestClient(app, client=("127.0.0.1", 50000)) as client:
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
                self.assertEqual(
                    app.state.engine._change_details(
                        {"etfbuyamount": 1_000_000, "etfsellamount": 0},
                        {"etfbuyamount": 2_000_000, "etfsellamount": 0},
                    )[0]["text"],
                    "申购份额 100 0000 → 200 0000",
                )

                renamed = client.put(
                    "/api/v1/symbols/159518/name", json={"name": "油气套利"}
                )
                self.assertEqual(renamed.status_code, 200)
                self.assertEqual(renamed.json()["items"][0]["name"], "油气套利")
                self.assertEqual(ConfigStore(config_path).symbol_name("159518"), "油气套利")

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

            with TestClient(app, client=("192.168.1.50", 50001)) as remote:
                self.assertEqual(remote.get("/api/v1/snapshot").status_code, 200)
                self.assertEqual(remote.post("/api/v1/monitor/start").status_code, 403)
                self.assertEqual(remote.post("/api/v1/monitor/stop").status_code, 403)
                self.assertEqual(remote.post("/api/v1/pcf/refresh").status_code, 403)
                self.assertEqual(
                    remote.put(
                        "/api/v1/watchlist", json={"symbols": ["159518"]}
                    ).status_code,
                    403,
                )
                self.assertEqual(
                    remote.put(
                        "/api/v1/symbols/159518/name", json={"name": "远程改名"}
                    ).status_code,
                    403,
                )

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

    def test_slow_capture_read_is_bounded_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "symbols": ["159518"],
                        "schedule": {"enabled": False},
                        "pcf": {"enabled": False},
                    }
                ),
                encoding="utf-8",
            )
            engine = MonitorEngine(
                ConfigStore(config_path),
                capture_dir=root,
                controller=FakeController(),
            )
            engine.CAPTURE_READ_TIMEOUT_SECONDS = 0.01
            calls = 0

            def slow_read(symbol):
                nonlocal calls
                calls += 1
                time.sleep(0.05)
                return (
                    {
                        "etfbuynumber": 1,
                        "etfbuyamount": 1_000_000,
                        "etfsellnumber": 0,
                        "etfsellamount": 0,
                        "netamount": 1_000_000,
                    },
                    time.time(),
                )

            engine._read_capture = slow_read

            async def scenario():
                started = time.monotonic()
                self.assertEqual(await engine.poll_once(), [])
                self.assertLess(time.monotonic() - started, 0.04)
                self.assertIn("读取超时", engine.last_error)
                self.assertEqual(calls, 1)
                await asyncio.sleep(0.06)
                self.assertEqual(await engine.poll_once(), [])
                self.assertEqual(calls, 1)
                self.assertEqual(
                    engine.states["159518.SZ"].values["etfbuyamount"],
                    1_000_000,
                )

            asyncio.run(scenario())

    def test_quiet_poll_keeps_latest_intraday_opportunity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "symbols": ["159518"],
                        "schedule": {"enabled": False},
                        "pcf": {"enabled": False},
                    }
                ),
                encoding="utf-8",
            )
            capture_path = root / "wind_tbapi_live_159518_SZ.json"
            capture_path.write_text(
                json.dumps(make_capture("159518.SZ", 1_000_000, 0)),
                encoding="utf-8",
            )
            engine = MonitorEngine(
                ConfigStore(config_path),
                capture_dir=root,
                controller=FakeController(),
            )
            engine.states["159518.SZ"].pcf = {
                "status": "ready",
                "trading_day": date.today().isoformat(),
                "creation_redemption_unit": 1_000_000,
                "creation_allowed": True,
                "redemption_allowed": True,
            }

            async def scenario():
                await engine.poll_once()
                capture_path.write_text(
                    json.dumps(make_capture("159518.SZ", 1_000_000, 1_000_000)),
                    encoding="utf-8",
                )
                await engine.poll_once()
                opportunity = dict(engine.states["159518.SZ"].opportunity)
                self.assertEqual(opportunity["kind"], "creation")
                await engine.poll_once()
                self.assertEqual(
                    engine.states["159518.SZ"].opportunity,
                    opportunity,
                )

            asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
