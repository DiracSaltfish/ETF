#!/usr/bin/env python3

import asyncio
import json
import struct
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, time as datetime_time
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from etf_monitor_server import ConfigStore, MonitorEngine, create_app
from wind_etf_realtime_ui import ProbeError, SubscriptionSession


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
    def __init__(self):
        self.running = False
        self.tbapi_loaded = False
        self.events = []
        self.fail_terminate = False

    def wind_process_status(self):
        return {
            "running": self.running,
            "pids": [123] if self.running else [],
            "tbapi_loaded": self.tbapi_loaded,
        }

    def launch_wind(self, timeout_seconds):
        self.events.append("launch")
        self.running = True
        self.tbapi_loaded = True
        return self.wind_process_status()

    def terminate_wind(self, timeout_seconds):
        self.events.append("terminate")
        if self.fail_terminate:
            raise ProbeError("Wind 未退出")
        self.running = False
        self.tbapi_loaded = False
        return self.wind_process_status()

    def cleanup_generated_dylibs(self):
        self.events.append("cleanup")
        return {"deleted_count": 2, "deleted_bytes": 1024}

    def subscribe_many(self, symbols, latency_ms):
        self.events.append("subscribe")
        sessions = {
            symbol: SubscriptionSession(symbol, 123, Path(f"/{symbol}.dylib"), index + 1)
            for index, symbol in enumerate(symbols)
        }
        return sessions, {}

    def stop_many(self, sessions):
        self.events.append("stop")
        return {}


class BlockingController(FakeController):
    def __init__(self):
        super().__init__()
        self.running = True
        self.tbapi_loaded = True
        self.subscribe_started = threading.Event()
        self.release_subscribe = threading.Event()

    def subscribe_many(self, symbols, latency_ms):
        self.events.append("subscribe")
        self.subscribe_started.set()
        if not self.release_subscribe.wait(timeout=2):
            raise RuntimeError("test subscription release timed out")
        return {
            symbol: SubscriptionSession(symbol, 123, Path(f"/{symbol}.dylib"), 1)
            for symbol in symbols
        }, {}


class FirstCallFaultController(FakeController):
    def __init__(self):
        super().__init__()
        self.running = True
        self.tbapi_loaded = True
        self.subscribe_calls = 0

    def subscribe_many(self, symbols, latency_ms):
        self.events.append("subscribe")
        self.subscribe_calls += 1
        if self.subscribe_calls == 1:
            raise ProbeError(
                "lldb 执行失败：Expression execution was interrupted: "
                "EXC_BAD_ACCESS (code=1, address=0x0)."
            )
        return {
            symbol: SubscriptionSession(symbol, 123, Path(f"/{symbol}.dylib"), 1)
            for symbol in symbols
        }, {}


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

    def test_default_collection_schedule_uses_two_stage_0915_start(self) -> None:
        schedule = ConfigStore.defaults()["schedule"]
        self.assertEqual(schedule["daily_reset"], "09:00")
        self.assertEqual(schedule["warmup"], "09:15:05")
        self.assertEqual(schedule["start"], "09:15:30")
        self.assertEqual(schedule["stop"], "15:00")
        self.assertEqual(schedule["policy_version"], 6)

    def test_default_wind_lifecycle_is_0910_to_1500(self) -> None:
        lifecycle = ConfigStore.defaults()["wind"]
        self.assertEqual(lifecycle["launch"], "09:10")
        self.assertEqual(lifecycle["shutdown"], "15:00")
        self.assertEqual(lifecycle["subscription_ready_stable_seconds"], 30)
        self.assertEqual(lifecycle["subscription_warmup_settle_seconds"], 2)
        self.assertTrue(lifecycle["cleanup_generated_dylibs"])
        self.assertTrue(lifecycle["close_on_host_exit"])

    def test_subscription_warmup_window_and_precise_scheduler_wakeup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = MonitorEngine(
                ConfigStore(Path(directory) / "config.json"),
                controller=FakeController(),
            )
            timezone = ZoneInfo("Asia/Shanghai")
            before = datetime(2026, 8, 14, 9, 15, 4, tzinfo=timezone)
            warmup = datetime(2026, 8, 14, 9, 15, 5, tzinfo=timezone)
            formal = datetime(2026, 8, 14, 9, 15, 30, tzinfo=timezone)
            self.assertFalse(engine._subscription_warmup_due(before))
            self.assertTrue(engine._subscription_warmup_due(warmup))
            self.assertFalse(engine._subscription_warmup_due(formal))
            self.assertAlmostEqual(engine._schedule_sleep_seconds(before), 1.0)

    def test_tbapi_warmup_uses_one_temporary_subscription(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                controller = FakeController()
                controller.running = True
                controller.tbapi_loaded = True
                engine = MonitorEngine(
                    ConfigStore(Path(directory) / "config.json"),
                    capture_dir=Path(directory),
                    controller=controller,
                )
                result = await engine.warmup_tbapi("test")
                self.assertFalse(result["monitoring"])
                self.assertEqual(controller.events, ["subscribe", "stop"])
                self.assertEqual(engine.tbapi_warmup_pid, 123)
                await engine.start_monitoring("test")
                self.assertTrue(engine.monitoring)
                self.assertEqual(
                    controller.events, ["subscribe", "stop", "subscribe"]
                )

        asyncio.run(scenario())

    def test_first_call_null_fault_is_consumed_then_formal_subscribe_succeeds(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                path.write_text(
                    json.dumps(
                        {
                            "symbols": ["159518", "159866"],
                            "wind": {"subscription_warmup_settle_seconds": 0},
                            "pcf": {"enabled": False},
                        }
                    ),
                    encoding="utf-8",
                )
                controller = FirstCallFaultController()
                engine = MonitorEngine(
                    ConfigStore(path),
                    capture_dir=Path(directory),
                    controller=controller,
                )
                result = await engine.start_monitoring("test")
                self.assertTrue(result["monitoring"])
                self.assertEqual(controller.events, ["subscribe", "subscribe"])
                self.assertIsNone(result["last_error"])
                self.assertEqual(engine.tbapi_warmup_pid, 123)

        asyncio.run(scenario())

    def test_wind_schedule_windows_exclude_weekends(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            engine = MonitorEngine(ConfigStore(path), controller=FakeController())
            self.assertFalse(engine._wind_launch_window(datetime(2026, 8, 14, 9, 9)))
            self.assertTrue(engine._wind_launch_window(datetime(2026, 8, 14, 9, 10)))
            self.assertTrue(engine._wind_shutdown_due(datetime(2026, 8, 14, 15, 0)))
            self.assertFalse(engine._wind_launch_window(datetime(2026, 8, 15, 10, 0)))
            self.assertFalse(
                engine._wind_ready_for_subscription(
                    {"running": True, "tbapi_loaded": False}
                )
            )
            self.assertTrue(
                engine._wind_ready_for_subscription(
                    {"running": True, "tbapi_loaded": True}
                )
            )

    def test_wind_lifecycle_orders_stop_quit_then_cleanup(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                path.write_text(
                    json.dumps(
                        {
                            "symbols": ["159518"],
                            "schedule": {"enabled": False},
                            "wind": {
                                "enabled": False,
                                "subscription_warmup_settle_seconds": 0,
                            },
                            "pcf": {"enabled": False},
                        }
                    ),
                    encoding="utf-8",
                )
                controller = FakeController()
                engine = MonitorEngine(
                    ConfigStore(path),
                    capture_dir=Path(directory),
                    controller=controller,
                )
                await engine.start_wind("schedule")
                persisted_after_start = ConfigStore(path).data["wind"]
                self.assertEqual(
                    persisted_after_start["last_managed_day"], date.today().isoformat()
                )
                await engine.start_monitoring("test")
                result = await engine.shutdown_wind_and_cleanup("test")
                self.assertFalse(result["monitoring"])
                self.assertEqual(result["wind"]["state"], "cleaned")
                self.assertEqual(result["wind"]["cleanup_deleted_count"], 2)
                persisted_after_stop = ConfigStore(path).data["wind"]
                self.assertEqual(
                    persisted_after_stop["last_shutdown_day"], date.today().isoformat()
                )
                self.assertEqual(
                    controller.events,
                    [
                        "cleanup",
                        "launch",
                        "subscribe",
                        "stop",
                        "subscribe",
                        "stop",
                        "terminate",
                        "cleanup",
                    ],
                )

        asyncio.run(scenario())

    def test_subscription_and_shutdown_share_one_operation_lock(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                path.write_text(
                    json.dumps(
                        {
                            "symbols": ["159518"],
                            "schedule": {"enabled": False},
                            "wind": {
                                "enabled": False,
                                "subscription_warmup_settle_seconds": 0,
                            },
                            "pcf": {"enabled": False},
                        }
                    ),
                    encoding="utf-8",
                )
                controller = BlockingController()
                engine = MonitorEngine(ConfigStore(path), controller=controller)
                subscribe_task = asyncio.create_task(engine.start_monitoring("test"))
                started = await asyncio.to_thread(
                    controller.subscribe_started.wait, 1
                )
                self.assertTrue(started)
                shutdown_task = asyncio.create_task(
                    engine.shutdown_wind_and_cleanup("test")
                )
                await asyncio.sleep(0.02)
                self.assertNotIn("terminate", controller.events)
                controller.release_subscribe.set()
                await subscribe_task
                await shutdown_task
                self.assertEqual(
                    controller.events,
                    [
                        "subscribe",
                        "stop",
                        "subscribe",
                        "stop",
                        "terminate",
                        "cleanup",
                    ],
                )

        asyncio.run(scenario())

    def test_failed_wind_termination_never_runs_post_exit_cleanup(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                path.write_text(
                    json.dumps(
                        {
                            "symbols": ["159518"],
                            "schedule": {"enabled": False},
                            "wind": {"enabled": False},
                            "pcf": {"enabled": False},
                        }
                    ),
                    encoding="utf-8",
                )
                controller = FakeController()
                engine = MonitorEngine(ConfigStore(path), controller=controller)
                await engine.start_wind("test")
                controller.fail_terminate = True
                with self.assertRaisesRegex(ProbeError, "Wind 未退出"):
                    await engine.shutdown_wind_and_cleanup("test")
                self.assertEqual(controller.events, ["cleanup", "launch", "terminate"])
                self.assertEqual(engine.wind_state["state"], "error")

        asyncio.run(scenario())

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
            self.assertEqual(schedule["daily_reset"], "09:00")
            self.assertEqual(schedule["warmup"], "09:15:05")
            self.assertEqual(schedule["start"], "09:15:30")
            self.assertEqual(schedule["stop"], "15:00")
            self.assertEqual(schedule["policy_version"], 6)

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

    def test_daily_reset_clears_state_and_rejects_pre_0900_capture(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config_path = root / "config.json"
                config_path.write_text(
                    json.dumps(
                        {
                            "symbols": ["159518"],
                            "pcf": {"enabled": False},
                        }
                    ),
                    encoding="utf-8",
                )
                capture_path = root / "wind_tbapi_live_159518_SZ.json"
                old_capture = make_capture("159518.SZ", 1_000_000, 0)
                old_capture["callback_epoch_ms"] = int(
                    datetime(
                        2026, 8, 17, 8, 59, 30, tzinfo=ZoneInfo("Asia/Shanghai")
                    ).timestamp()
                    * 1000
                )
                capture_path.write_text(json.dumps(old_capture), encoding="utf-8")
                engine = MonitorEngine(
                    ConfigStore(config_path),
                    capture_dir=root,
                    controller=FakeController(),
                )
                state = engine.states["159518.SZ"]
                state.values = {"etfbuyamount": 9_000_000}
                state.updated_at = time.time()
                state.last_change = [{"text": "旧数据"}]
                engine.baselines["159518.SZ"] = dict(state.values)

                reset_now = datetime(
                    2026, 8, 17, 9, 0, 1, tzinfo=ZoneInfo("Asia/Shanghai")
                )
                await engine.reset_daily_realtime_state(now=reset_now)
                self.assertEqual(state.values, {})
                self.assertIsNone(state.updated_at)
                self.assertEqual(state.last_change, [])
                self.assertEqual(engine.baselines, {})
                self.assertEqual(engine.realtime_reset_day, date(2026, 8, 17))
                self.assertEqual(await engine.poll_once(), [])
                self.assertEqual(state.values, {})

                new_capture = make_capture("159518.SZ", 1_000_000, 0)
                new_capture["callback_epoch_ms"] = int(
                    datetime(
                        2026, 8, 17, 9, 16, 1, tzinfo=ZoneInfo("Asia/Shanghai")
                    ).timestamp()
                    * 1000
                )
                capture_path.write_text(json.dumps(new_capture), encoding="utf-8")
                await engine.poll_once()
                self.assertEqual(state.values["etfbuyamount"], 1_000_000)

        asyncio.run(scenario())

    def test_change_history_persists_callback_time_and_is_queryable(self) -> None:
        async def scenario() -> None:
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
                first = make_capture("159518.SZ", 1_000_000, 0)
                capture_path.write_text(json.dumps(first), encoding="utf-8")
                engine = MonitorEngine(
                    ConfigStore(config_path),
                    capture_dir=root,
                    controller=FakeController(),
                )
                await engine.poll_once()
                event_at = datetime(
                    2026, 8, 17, 14, 24, 0, 619000,
                    tzinfo=ZoneInfo("Asia/Shanghai"),
                )
                second = make_capture("159518.SZ", 2_000_000, 0)
                second["callback_epoch_ms"] = int(event_at.timestamp() * 1000)
                capture_path.write_text(json.dumps(second), encoding="utf-8")
                await engine.poll_once()

                payload = await engine.change_history(
                    date(2026, 8, 17), symbol="159518", limit=10
                )
                self.assertEqual(len(payload["items"]), 1)
                item = payload["items"][0]
                self.assertEqual(item["event_time"], "2026-08-17T14:24:00.619+08:00")
                self.assertEqual(item["current"]["etfbuyamount"], 2_000_000)
                self.assertEqual(item["changes"][0]["field"], "etfbuyamount")

        asyncio.run(scenario())

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
                        "wind": {
                            "enabled": False,
                            "subscription_warmup_settle_seconds": 0,
                        },
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
            controller = FakeController()
            controller.running = True
            controller.tbapi_loaded = True
            app = create_app(config_path, capture_dir=root, controller=controller)
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

                history = client.get(
                    f"/api/v1/history?date={date.today().isoformat()}&symbol=159518"
                )
                self.assertEqual(history.status_code, 200)
                self.assertEqual(history.json()["type"], "history")
                self.assertEqual(len(history.json()["items"]), 1)
                self.assertEqual(
                    history.json()["items"][0]["changes"][0]["field"],
                    "etfbuyamount",
                )

                stopped = client.post("/api/v1/monitor/stop")
                self.assertEqual(stopped.status_code, 200)
                self.assertFalse(stopped.json()["monitoring"])

                wind_started = client.post("/api/v1/wind/start")
                self.assertEqual(wind_started.status_code, 200)
                self.assertTrue(wind_started.json()["wind"]["running"])
                wind_stopped = client.post("/api/v1/wind/shutdown-cleanup")
                self.assertEqual(wind_stopped.status_code, 200)
                self.assertEqual(wind_stopped.json()["wind"]["state"], "cleaned")

            with TestClient(app, client=("192.168.1.50", 50001)) as remote:
                self.assertEqual(remote.get("/api/v1/snapshot").status_code, 200)
                self.assertEqual(remote.post("/api/v1/monitor/start").status_code, 403)
                self.assertEqual(remote.post("/api/v1/monitor/stop").status_code, 403)
                self.assertEqual(remote.get("/api/v1/wind/status").status_code, 403)
                self.assertEqual(remote.post("/api/v1/wind/start").status_code, 403)
                self.assertEqual(
                    remote.post("/api/v1/wind/shutdown-cleanup").status_code, 403
                )
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
