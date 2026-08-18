#!/usr/bin/env python3
"""Long-running Mac host service for Shenzhen ETF change distribution."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from etf_pcf_service import PcfService, classify_intraday_opportunity
from wind_etf_realtime_ui import (
    APP_DIR,
    ProbeController,
    ProbeError,
    SubscriptionSession,
    WIND_TEMP_DIR,
    display_symbol,
    normalize_symbol,
    safe_code,
)
from wind_tbapi_frame_parser import FrameFormatError, decode_probe_capture


if getattr(sys, "frozen", False):
    SERVER_DATA_DIR = Path.home() / "Library" / "Application Support" / "ETFDelivery"
else:
    SERVER_DATA_DIR = APP_DIR
DEFAULT_CONFIG_PATH = SERVER_DATA_DIR / "config" / "etf_monitor_server.json"
DEFAULT_LOG_PATH = SERVER_DATA_DIR / "logs" / "server.log"
DEFAULT_HISTORY_DIR = SERVER_DATA_DIR / "history"
WEB_INDEX_PATH = APP_DIR / "web" / "monitor.html"
PROTOCOL_VERSION = 1
ALERT_FIELDS = [
    ("etfbuyamount", "申购份额", False),
    ("etfsellamount", "赎回份额", False),
]
# `netamount` is always derived from the two share fields above.  It remains
# available in snapshots and opportunity calculations, but must not be a
# separate alert trigger.  Wind's buy/sell money fields are intentionally not
# included: they can legitimately remain zero in this data source.
LOGGER = logging.getLogger("etf-monitor")


def configure_file_logging(path: Path = DEFAULT_LOG_PATH) -> None:
    """Install one bounded UTF-8 log file handler for service diagnostics."""

    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if any(getattr(handler, "baseFilename", None) == str(path) for handler in root.handlers):
        return
    handler = RotatingFileHandler(
        path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def now_iso() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def timestamp_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(
        timestamp, ZoneInfo("Asia/Shanghai")
    ).isoformat(timespec="milliseconds")


def optional_iso_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def signed_number(value: Any) -> str:
    return share_number(value, signed=True)


def plain_number(value: Any) -> str:
    return share_number(value)


def share_number(value: Any, signed: bool = False) -> str:
    """Format share counts in four-digit Chinese units."""

    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "—"
    rounded = int(round(value))
    digits = str(abs(rounded))
    groups: list[str] = []
    while digits:
        groups.append(digits[-4:])
        digits = digits[:-4]
    text = " ".join(reversed(groups)) or "0"
    if rounded < 0:
        return f"-{text}"
    if signed and rounded > 0:
        return f"+{text}"
    return text


class ConfigStore:
    def __init__(self, path: Path = DEFAULT_CONFIG_PATH) -> None:
        self.path = path
        self.data = self._load_or_create()

    @staticmethod
    def defaults() -> dict[str, Any]:
        return {
            "symbols": ["159518", "159393"],
            "symbol_names": {},
            "refresh_interval_seconds": 1.0,
            "network": {
                "host": "0.0.0.0",
                "port": 6787,
            },
            "schedule": {
                "enabled": True,
                "policy_version": 6,
                "timezone": "Asia/Shanghai",
                "weekdays": [0, 1, 2, 3, 4],
                "daily_reset": "09:00",
                "warmup": "09:15:05",
                "start": "09:15:30",
                "stop": "15:00",
                "last_reset_day": "",
            },
            "wind": {
                "enabled": True,
                "policy_version": 1,
                "timezone": "Asia/Shanghai",
                "weekdays": [0, 1, 2, 3, 4],
                "launch": "09:10",
                "shutdown": "15:00",
                "startup_timeout_seconds": 30,
                "terminate_timeout_seconds": 30,
                "relaunch_cooldown_seconds": 120,
                "shutdown_retry_seconds": 120,
                "subscription_retry_seconds": 30,
                "subscription_ready_stable_seconds": 30,
                "subscription_warmup_settle_seconds": 2,
                "cleanup_generated_dylibs": True,
                "close_on_host_exit": True,
                "last_managed_day": "",
                "last_shutdown_day": "",
                "manual_override_day": "",
            },
            "pcf": {
                "enabled": True,
                "policy_version": 2,
                "timezone": "Asia/Shanghai",
                "weekdays": [0, 1, 2, 3, 4],
                "fetch_start": "08:30",
                "fetch_end": "23:00",
                "retry_interval_seconds": 900,
                "max_auto_attempts_per_symbol_per_day": 8,
                "min_request_interval_seconds": 8,
                "cache_dir": "",
            },
            "history": {
                "enabled": True,
                "directory": "",
                "retention_days": 120,
                "max_query_items": 5000,
            },
        }

    def _load_or_create(self) -> dict[str, Any]:
        defaults = self.defaults()
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if not isinstance(loaded, dict):
                    raise ValueError("配置根节点必须是对象")
                data = self._merge(defaults, loaded)
                loaded_schedule = loaded.get("schedule")
                if not isinstance(loaded_schedule, dict):
                    loaded_schedule = {}
                if int(loaded_schedule.get("policy_version", 1)) < 6:
                    # One-time migration to the two-stage 09:15 subscription
                    # policy.  The warm-up primes Wind's lazily-created TBAPI2
                    # module; the full watchlist is subscribed afterwards.
                    # The host owns collection; remote clients are read-only.
                    data["schedule"]["daily_reset"] = "09:00"
                    data["schedule"]["warmup"] = "09:15:05"
                    data["schedule"]["start"] = "09:15:30"
                    data["schedule"]["stop"] = "15:00"
                    data["schedule"]["policy_version"] = 6
                loaded_pcf = loaded.get("pcf")
                if not isinstance(loaded_pcf, dict):
                    loaded_pcf = {}
                if int(loaded_pcf.get("policy_version", 1)) < 2:
                    data["pcf"]["fetch_start"] = "08:30"
                    data["pcf"]["policy_version"] = 2
            except (OSError, ValueError, json.JSONDecodeError):
                backup = self.path.with_suffix(
                    self.path.suffix + f".invalid-{int(time.time())}"
                )
                try:
                    self.path.replace(backup)
                except OSError:
                    pass
                data = defaults
        else:
            data = defaults
        self.data = data
        self.save()
        return data

    @staticmethod
    def _merge(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
        result = dict(base)
        for key, value in update.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = ConfigStore._merge(result[key], value)
            else:
                result[key] = value
        return result

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(self.data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        temp.replace(self.path)

    @property
    def symbols(self) -> list[str]:
        result: list[str] = []
        for code in self.data.get("symbols", []):
            try:
                symbol = normalize_symbol(str(code))
            except ValueError:
                continue
            if symbol not in result:
                result.append(symbol)
        return result

    def set_symbols(self, symbols: list[str]) -> list[str]:
        normalized: list[str] = []
        for code in symbols:
            symbol = normalize_symbol(code)
            if symbol not in normalized:
                normalized.append(symbol)
        if not normalized:
            raise ValueError("观察列表不能为空")
        self.data["symbols"] = [display_symbol(symbol) for symbol in normalized]
        self.save()
        return normalized

    def symbol_name(self, symbol: str) -> str:
        code = display_symbol(normalize_symbol(symbol))
        names = self.data.get("symbol_names")
        if not isinstance(names, dict):
            return ""
        return str(names.get(code) or "").strip()

    def set_symbol_name(self, symbol: str, name: str) -> str:
        normalized = normalize_symbol(symbol)
        if normalized not in self.symbols:
            raise ValueError("只能修改当前观察列表中的标的名称")
        clean = " ".join(str(name).split()).strip()
        if len(clean) > 40:
            raise ValueError("标的名称不能超过 40 个字符")
        names = self.data.setdefault("symbol_names", {})
        if not isinstance(names, dict):
            names = {}
            self.data["symbol_names"] = names
        code = display_symbol(normalized)
        if clean:
            names[code] = clean
        else:
            names.pop(code, None)
        self.save()
        return clean


class ChangeHistoryStore:
    """Append-only daily change journal for observed share callbacks."""

    def __init__(
        self,
        directory: Path,
        *,
        enabled: bool = True,
        retention_days: int = 120,
        max_query_items: int = 5000,
    ) -> None:
        self.directory = directory
        self.enabled = enabled
        self.retention_days = max(1, int(retention_days))
        self.max_query_items = max(1, min(20_000, int(max_query_items)))
        self._lock = threading.Lock()
        self._last_pruned_day: date | None = None

    def _path_for(self, history_day: date) -> Path:
        return self.directory / f"changes_{history_day.isoformat()}.jsonl"

    @staticmethod
    def _event_day(record: dict[str, Any]) -> date:
        try:
            return datetime.fromisoformat(str(record.get("event_time"))).date()
        except (TypeError, ValueError):
            return datetime.now(ZoneInfo("Asia/Shanghai")).date()

    def _prune_locked(self, today: date) -> None:
        if self._last_pruned_day == today:
            return
        self._last_pruned_day = today
        cutoff = today - timedelta(days=self.retention_days - 1)
        try:
            candidates = list(self.directory.glob("changes_????-??-??.jsonl"))
        except OSError:
            return
        for path in candidates:
            try:
                file_day = date.fromisoformat(path.stem.removeprefix("changes_"))
            except ValueError:
                continue
            if file_day >= cutoff:
                continue
            try:
                if not path.is_symlink() and path.is_file():
                    path.unlink()
            except OSError:
                LOGGER.warning("failed to prune change history %s", path)

    def append(self, record: dict[str, Any]) -> None:
        if not self.enabled:
            return
        history_day = self._event_day(record)
        payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._prune_locked(history_day)
            with self._path_for(history_day).open("a", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()

    def query(
        self,
        history_day: date,
        *,
        symbol: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        normalized = normalize_symbol(symbol) if symbol else None
        bounded_limit = max(1, min(int(limit), self.max_query_items))
        path = self._path_for(history_day)
        with self._lock:
            try:
                with path.open("r", encoding="utf-8") as handle:
                    lines = handle.readlines()
            except FileNotFoundError:
                return []
            except OSError as exc:
                raise ProbeError(f"变化历史读取失败：{exc}") from exc
        result: list[dict[str, Any]] = []
        for line in reversed(lines):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            if normalized and str(item.get("windcode") or "").upper() != normalized:
                continue
            result.append(item)
            if len(result) >= bounded_limit:
                break
        return result


@dataclass
class SymbolState:
    symbol: str
    custom_name: str = ""
    status: str = "waiting"
    sub_id: int | None = None
    values: dict[str, Any] = field(default_factory=dict)
    updated_at: float | None = None
    last_change_at: str | None = None
    last_change: list[dict[str, Any]] = field(default_factory=list)
    pcf: dict[str, Any] = field(default_factory=dict)
    opportunity: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        age = None
        if self.updated_at is not None:
            age = max(0.0, time.time() - self.updated_at)
        return {
            "symbol": display_symbol(self.symbol),
            "windcode": self.symbol,
            "name": self.custom_name or str(self.pcf.get("fund_name") or ""),
            "custom_name": self.custom_name,
            "status": self.status,
            "sub_id": self.sub_id,
            "values": dict(self.values),
            "updated_at": (
                datetime.fromtimestamp(
                    self.updated_at, ZoneInfo("Asia/Shanghai")
                ).strftime("%H:%M:%S")
                if self.updated_at is not None
                else None
            ),
            "age_seconds": age,
            "last_change_at": self.last_change_at,
            "last_change": list(self.last_change),
            "pcf": dict(self.pcf),
            "opportunity": dict(self.opportunity),
        }


class ConnectionManager:
    def __init__(self) -> None:
        self.connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self.connections.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self.connections.discard(websocket)

    async def broadcast(self, event: dict[str, Any]) -> None:
        async with self._lock:
            targets = list(self.connections)
        dead: list[WebSocket] = []
        for websocket in targets:
            try:
                await websocket.send_json(event)
            except Exception:
                dead.append(websocket)
        if dead:
            async with self._lock:
                for websocket in dead:
                    self.connections.discard(websocket)


class MonitorEngine:
    CAPTURE_READ_TIMEOUT_SECONDS = 2.0

    def __init__(
        self,
        config: ConfigStore,
        capture_dir: Path = WIND_TEMP_DIR,
        controller: ProbeController | None = None,
        pcf_service: PcfService | None = None,
        history_store: ChangeHistoryStore | None = None,
    ) -> None:
        self.config = config
        self.capture_dir = capture_dir
        self.controller = controller or ProbeController()
        pcf_config = config.data.get("pcf", {})
        configured_cache = str(pcf_config.get("cache_dir") or "").strip()
        pcf_cache_dir = (
            Path(configured_cache).expanduser()
            if configured_cache
            else SERVER_DATA_DIR / "pcf_cache"
        )
        self.pcf_service = pcf_service or PcfService(
            pcf_cache_dir,
            enabled=bool(pcf_config.get("enabled", True)),
            min_request_interval_seconds=int(
                pcf_config.get("min_request_interval_seconds", 8)
            ),
        )
        history_config = config.data.get("history", {})
        if not isinstance(history_config, dict):
            history_config = {}
        configured_history = str(history_config.get("directory") or "").strip()
        if configured_history:
            history_dir = Path(configured_history).expanduser()
        elif config.path.parent.name == "config":
            history_dir = config.path.parent.parent / "history"
        else:
            history_dir = config.path.parent / "history"
        self.history_store = history_store or ChangeHistoryStore(
            history_dir,
            enabled=bool(history_config.get("enabled", True)),
            retention_days=int(history_config.get("retention_days", 120)),
            max_query_items=int(history_config.get("max_query_items", 5000)),
        )
        self.manager = ConnectionManager()
        self.sessions: dict[str, SubscriptionSession] = {}
        self.states: dict[str, SymbolState] = {
            symbol: SymbolState(symbol, custom_name=config.symbol_name(symbol))
            for symbol in config.symbols
        }
        self.baselines: dict[str, dict[str, Any]] = {}
        self.monitoring = False
        self.started_by: str | None = None
        self.last_error: str | None = None
        # One lock serializes every Wind process and subscription mutation.
        # Keeping a single lock avoids check-then-act races between manual UI
        # requests and the scheduler's close/restart path.
        self.operation_lock = asyncio.Lock()
        self.poll_lock = asyncio.Lock()
        self.pcf_lock = asyncio.Lock()
        self.pcf_last_attempt: dict[str, float] = {}
        self.pcf_attempt_counts: dict[tuple[date, str], int] = {}
        self.capture_read_tasks: dict[
            str, asyncio.Task[tuple[dict[str, Any], float]]
        ] = {}
        self.tasks: list[asyncio.Task[Any]] = []
        self.shutting_down = False
        self.wind_state: dict[str, Any] = {
            "state": "unknown",
            "label": "正在检测 Wind",
            "running": False,
            "tbapi_loaded": False,
            "pid_count": 0,
            "last_action": None,
            "last_error": None,
            "cleanup_deleted_count": 0,
            "cleanup_deleted_bytes": 0,
        }
        wind_runtime = config.data.get("wind", {})
        if not isinstance(wind_runtime, dict):
            wind_runtime = {}
        self.wind_managed_day = optional_iso_date(
            wind_runtime.get("last_managed_day")
        )
        self.wind_shutdown_day = optional_iso_date(
            wind_runtime.get("last_shutdown_day")
        )
        self.wind_manual_override_day = optional_iso_date(
            wind_runtime.get("manual_override_day")
        )
        self.last_wind_launch_attempt = 0.0
        self.last_wind_shutdown_attempt = 0.0
        self.last_subscription_attempt = 0.0
        self.wind_tbapi_ready_since = 0.0
        # Wind lazily creates its internal TBAPI2 module on the first native
        # subscription call.  That first call can fault after performing the
        # initialization side effect; remember the PID/day that has been
        # warmed so the formal full-watchlist call is the second invocation.
        self.tbapi_warmup_pid: int | None = None
        self.tbapi_warmup_day: date | None = None
        self.tbapi_warmup_attempt_pid: int | None = None
        self.tbapi_warmup_attempt_day: date | None = None
        schedule_runtime = config.data.get("schedule", {})
        if not isinstance(schedule_runtime, dict):
            schedule_runtime = {}
        self.realtime_reset_day = optional_iso_date(
            schedule_runtime.get("last_reset_day")
        )
        self.capture_not_before_epoch = 0.0
        self._restore_daily_capture_cutoff()

    @property
    def interval(self) -> float:
        return max(0.5, float(self.config.data["refresh_interval_seconds"]))

    def _schedule_config(self) -> dict[str, Any]:
        value = self.config.data.get("schedule", {})
        return value if isinstance(value, dict) else {}

    def _schedule_now(self) -> datetime:
        timezone = ZoneInfo(
            str(self._schedule_config().get("timezone", "Asia/Shanghai"))
        )
        return datetime.now(timezone)

    def _daily_reset_time(self) -> datetime_time:
        return datetime_time.fromisoformat(
            str(self._schedule_config().get("daily_reset", "09:00"))
        )

    def _restore_daily_capture_cutoff(self) -> None:
        now = self._schedule_now()
        if self.realtime_reset_day != now.date():
            return
        reset_at = datetime.combine(
            now.date(), self._daily_reset_time(), tzinfo=now.tzinfo
        )
        if now >= reset_at:
            self.capture_not_before_epoch = reset_at.timestamp()

    def _persist_schedule_runtime(self) -> None:
        schedule = self._schedule_config()
        schedule["last_reset_day"] = (
            self.realtime_reset_day.isoformat() if self.realtime_reset_day else ""
        )
        self.config.data["schedule"] = schedule
        self.config.save()

    def _daily_realtime_reset_due(self, now: datetime) -> bool:
        schedule = self._schedule_config()
        if not schedule.get("enabled", True):
            return False
        if now.weekday() not in schedule.get("weekdays", [0, 1, 2, 3, 4]):
            return False
        return (
            now.time().replace(tzinfo=None) >= self._daily_reset_time()
            and self.realtime_reset_day != now.date()
        )

    async def reset_daily_realtime_state(
        self, *, now: datetime | None = None, reason: str = "schedule"
    ) -> dict[str, Any]:
        async with self.operation_lock:
            current = now or self._schedule_now()
            reset_at = datetime.combine(
                current.date(), self._daily_reset_time(), tzinfo=current.tzinfo
            )
            # Establish the cutoff first so an already-running reader can no
            # longer reintroduce yesterday's final callback after the reset.
            self.capture_not_before_epoch = reset_at.timestamp()
            pending_reads = list(self.capture_read_tasks.values())
            self.capture_read_tasks.clear()
            for task in pending_reads:
                task.cancel()
            if pending_reads:
                await asyncio.gather(*pending_reads, return_exceptions=True)
            self.baselines.clear()
            for symbol, state in self.states.items():
                state.values = {}
                state.updated_at = None
                state.last_change_at = None
                state.last_change = []
                state.opportunity = {}
                if symbol in self.sessions:
                    state.status = "monitoring"
                    state.sub_id = self.sessions[symbol].sub_id
                else:
                    state.status = "waiting"
                    state.sub_id = None
            self.last_error = None
            self.realtime_reset_day = current.date()
            self._persist_schedule_runtime()
            LOGGER.info(
                "daily realtime state reset for %s (%s)",
                current.date().isoformat(),
                reason,
            )
            event = self.snapshot_event()
            await self.manager.broadcast(event)
            return event

    async def _maybe_reset_daily_realtime_state(
        self, now: datetime | None = None
    ) -> bool:
        current = now or self._schedule_now()
        if not self._daily_realtime_reset_due(current):
            return False
        await self.reset_daily_realtime_state(now=current)
        return True

    def snapshot_event(self) -> dict[str, Any]:
        return {
            "type": "snapshot",
            "protocol": PROTOCOL_VERSION,
            "server_time": now_iso(),
            "monitoring": self.monitoring,
            "started_by": self.started_by,
            "last_error": self.last_error,
            "wind": dict(self.wind_state),
            "items": [
                self.states.setdefault(symbol, SymbolState(symbol)).to_dict()
                for symbol in self.config.symbols
            ],
        }

    def status_event(self, message: str) -> dict[str, Any]:
        return {
            "type": "status",
            "protocol": PROTOCOL_VERSION,
            "server_time": now_iso(),
            "monitoring": self.monitoring,
            "message": message,
            "wind": dict(self.wind_state),
        }

    async def start_background_tasks(self) -> None:
        await self._load_cached_pcf()
        await self.refresh_wind_status()
        await self._maybe_reset_daily_realtime_state()
        self.tasks = [
            asyncio.create_task(self._poll_loop(), name="capture-poll"),
            asyncio.create_task(self._schedule_loop(), name="schedule"),
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
        ]
        if self.pcf_service.enabled:
            self.tasks.append(asyncio.create_task(self._pcf_loop(), name="pcf-daily"))

    async def shutdown(self) -> None:
        self.shutting_down = True
        for task in self.tasks:
            task.cancel()
        for task in self.capture_read_tasks.values():
            task.cancel()
        await asyncio.gather(
            *self.tasks,
            *self.capture_read_tasks.values(),
            return_exceptions=True,
        )
        self.capture_read_tasks.clear()
        if self._wind_config().get("close_on_host_exit", True):
            try:
                await self.shutdown_wind_and_cleanup("host-shutdown")
            except Exception:
                LOGGER.exception("Wind cleanup during host shutdown failed")
        elif self.sessions:
            await self.stop_monitoring("shutdown")

    @staticmethod
    def _is_expected_tbapi_warmup_fault(exc: BaseException) -> bool:
        """Recognize Wind's reproducible first-call lazy-init null fault."""

        detail = str(exc)
        return "EXC_BAD_ACCESS" in detail and (
            "address=0x0" in detail
            or "address = 0x0" in detail
            or "address=0x0000000000000000" in detail
        )

    @staticmethod
    def _status_pid(status: dict[str, Any]) -> int | None:
        pids = status.get("pids")
        if not isinstance(pids, list):
            return None
        valid = [int(pid) for pid in pids if isinstance(pid, int) and pid > 0]
        return valid[0] if len(valid) == 1 else None

    def _tbapi_warmup_is_current(self, pid: int, today: date) -> bool:
        return self.tbapi_warmup_pid == pid and self.tbapi_warmup_day == today

    async def warmup_tbapi(self, reason: str = "schedule") -> dict[str, Any]:
        """Prime Wind TBAPI2 with one temporary subscription.

        The first native call may raise EXC_BAD_ACCESS after initializing the
        process-global TBAPI module.  That exact failure is an expected warm-up
        result; unrelated failures remain visible.
        """

        async with self.operation_lock:
            await self._warmup_tbapi_locked(reason, force=False)
            return self.snapshot_event()

    async def _warmup_tbapi_locked(self, reason: str, *, force: bool) -> bool:
        if self.monitoring and self.sessions:
            return False
        status = await self._controller_wind_status()
        pid = self._status_pid(status)
        today = self._wind_now().date()
        if not status.get("running") or not status.get("tbapi_loaded") or pid is None:
            raise ProbeError("Wind TBAPI 尚未就绪，暂不能执行订阅预热。")
        if self._tbapi_warmup_is_current(pid, today):
            return False
        if (
            not force
            and self.tbapi_warmup_attempt_pid == pid
            and self.tbapi_warmup_attempt_day == today
        ):
            return False

        symbols = self.config.symbols
        if not symbols:
            raise ProbeError("观察列表为空，无法执行 TBAPI 预热。")
        symbol = symbols[0]
        self.tbapi_warmup_attempt_pid = pid
        self.tbapi_warmup_attempt_day = today
        self.wind_state.update(
            state="warming",
            label=f"正在预热 Wind TBAPI（{display_symbol(symbol)}）",
            last_error=None,
        )
        await self.manager.broadcast(self.status_event("正在执行 TBAPI 预热"))

        expected_first_fault = False
        try:
            sessions, errors = await asyncio.to_thread(
                self.controller.subscribe_many,
                [symbol],
                int(self.interval * 1000),
            )
            if not sessions:
                detail = errors.get(symbol, "未返回临时订阅")
                raise ProbeError(f"TBAPI 预热未建立临时订阅：{detail}")
            stop_errors = await asyncio.to_thread(
                self.controller.stop_many, sessions
            )
            if stop_errors:
                raise ProbeError(f"TBAPI 预热临时订阅停止失败：{stop_errors}")
        except Exception as exc:
            if not self._is_expected_tbapi_warmup_fault(exc):
                self.wind_state.update(
                    state="error",
                    label="Wind TBAPI 预热失败",
                    last_error=str(exc),
                )
                await self.manager.broadcast(
                    self.status_event(f"TBAPI 预热失败：{exc}")
                )
                raise
            expected_first_fault = True
            LOGGER.info(
                "TBAPI first-call warm-up fault consumed for pid=%s symbol=%s",
                pid,
                symbol,
            )

        self.tbapi_warmup_pid = pid
        self.tbapi_warmup_day = today
        self.last_error = None
        self.wind_state.update(
            state="ready",
            label="TBAPI 预热完成，等待正式订阅",
            running=True,
            tbapi_loaded=True,
            pid_count=1,
            last_error=None,
            last_action=(
                f"{now_iso()} TBAPI 预热完成（{reason}"
                f"{'，已吸收首次初始化异常' if expected_first_fault else ''}）"
            ),
        )
        await self.manager.broadcast(
            self.status_event("TBAPI 预热完成，等待正式订阅")
        )
        return True

    async def start_monitoring(self, reason: str = "manual") -> dict[str, Any]:
        async with self.operation_lock:
            if self.monitoring and self.sessions:
                return self.snapshot_event()
            warmed_now = await self._warmup_tbapi_locked(reason, force=True)
            if warmed_now:
                settle_seconds = max(
                    0.0,
                    float(
                        self._wind_config().get(
                            "subscription_warmup_settle_seconds", 2
                        )
                    ),
                )
                if settle_seconds:
                    await asyncio.sleep(settle_seconds)
            return await self._start_monitoring_locked(reason)

    async def _start_monitoring_locked(self, reason: str) -> dict[str, Any]:
        if self.monitoring and self.sessions:
            return self.snapshot_event()
        self.wind_state.update(state="subscribing", label="正在建立 Wind 订阅")
        symbols = self.config.symbols
        for symbol in symbols:
            state = self.states.setdefault(symbol, SymbolState(symbol))
            state.status = "connecting"
        self.baselines.clear()
        await self.manager.broadcast(self.status_event("正在建立批量订阅"))
        try:
            sessions, errors = await asyncio.to_thread(
                self.controller.subscribe_many,
                symbols,
                int(self.interval * 1000),
            )
        except Exception as exc:
            LOGGER.exception("subscription failed")
            self.last_error = f"订阅失败：{exc}"
            await self.refresh_wind_status(preserve_lifecycle_action=False)
            if self.wind_state.get("running"):
                self.wind_state.update(
                    state="running", label="Wind 已运行，订阅尚未就绪"
                )
            self.wind_state["last_error"] = str(exc)
            for symbol in symbols:
                self.states[symbol].status = "error"
            await self.manager.broadcast(self.status_event(f"订阅失败：{exc}"))
            raise
        self.sessions = sessions
        self.monitoring = bool(sessions)
        self.started_by = reason if self.monitoring else None
        if self.monitoring:
            self.last_error = None
            self.wind_state.update(
                state="monitoring",
                label="Wind 监控中",
                running=True,
                last_error=None,
            )
        for symbol in symbols:
            state = self.states.setdefault(symbol, SymbolState(symbol))
            if symbol in sessions:
                state.status = "monitoring"
                state.sub_id = sessions[symbol].sub_id
            else:
                state.status = "error"
                state.sub_id = None
                state.last_change = [
                    {"field": "error", "message": errors.get(symbol, "")}
                ]
        await self.manager.broadcast(
            self.status_event(f"正在监控 {len(sessions)}/{len(symbols)} 个标的")
        )
        await self.poll_once()
        return self.snapshot_event()

    async def stop_monitoring(self, reason: str = "manual") -> dict[str, Any]:
        async with self.operation_lock:
            return await self._stop_monitoring_locked(reason)

    async def _stop_monitoring_locked(self, reason: str) -> dict[str, Any]:
        sessions = dict(self.sessions)
        if sessions:
            errors = await asyncio.to_thread(self.controller.stop_many, sessions)
        else:
            errors = {}
        self.sessions.clear()
        self.monitoring = False
        self.started_by = None
        if not reason.startswith("wind-shutdown:"):
            await self.refresh_wind_status()
        for symbol, state in self.states.items():
            state.status = "stopped" if symbol not in errors else "error"
            state.sub_id = None
        await self.manager.broadcast(self.status_event(f"监控已停止：{reason}"))
        return self.snapshot_event()

    async def update_watchlist(self, symbols: list[str]) -> dict[str, Any]:
        async with self.operation_lock:
            was_monitoring = self.monitoring
            if was_monitoring:
                await self._stop_monitoring_locked("更新观察列表")
            normalized = self.config.set_symbols(symbols)
            self.states = {
                symbol: self.states.get(
                    symbol,
                    SymbolState(
                        symbol, custom_name=self.config.symbol_name(symbol)
                    ),
                )
                for symbol in normalized
            }
            self.baselines = {
                symbol: baseline
                for symbol, baseline in self.baselines.items()
                if symbol in self.states
            }
            pcf_task = asyncio.create_task(
                self.refresh_pcf(normalized), name="pcf-watchlist-update"
            )
            self.tasks.append(pcf_task)
            if was_monitoring:
                return await self._start_monitoring_locked("watchlist-update")
            event = self.snapshot_event()
            await self.manager.broadcast(event)
            return event

    async def update_symbol_name(self, symbol: str, name: str) -> dict[str, Any]:
        normalized = normalize_symbol(symbol)
        clean = self.config.set_symbol_name(normalized, name)
        state = self.states.setdefault(normalized, SymbolState(normalized))
        state.custom_name = clean
        event = self.snapshot_event()
        await self.manager.broadcast(event)
        return event

    async def change_history(
        self, history_day: date, *, symbol: str | None = None, limit: int = 500
    ) -> dict[str, Any]:
        items = await asyncio.to_thread(
            self.history_store.query,
            history_day,
            symbol=symbol,
            limit=limit,
        )
        return {
            "type": "history",
            "protocol": PROTOCOL_VERSION,
            "server_time": now_iso(),
            "date": history_day.isoformat(),
            "symbol": display_symbol(normalize_symbol(symbol)) if symbol else None,
            "items": items,
        }

    def _candidate_captures(self, symbol: str) -> list[Path]:
        try:
            with os.scandir(self.capture_dir):
                pass
        except PermissionError:
            self.last_error = (
                "无法读取 Wind 数据目录。请在 macOS“系统设置 → 隐私与安全性 → "
                "完全磁盘访问权限”中允许 ETF监控主机。"
            )
            return []
        except FileNotFoundError:
            self.last_error = "未找到 Wind 数据目录，请先启动并登录 Wind。"
            return []
        except OSError as exc:
            LOGGER.warning("Wind capture directory scan interrupted: %s", exc)
            return []
        live = self.capture_dir / f"wind_tbapi_live_{safe_code(symbol)}.json"
        candidates = [live] if live.exists() else []
        try:
            candidates.extend(
                sorted(
                    self.capture_dir.glob("wind_tbapi_probe_sub_*.json"),
                    key=lambda path: path.stat().st_mtime_ns,
                    reverse=True,
                )
            )
        except PermissionError:
            self.last_error = (
                "无法读取 Wind 数据目录。请在 macOS“系统设置 → 隐私与安全性 → "
                "完全磁盘访问权限”中允许 ETF监控主机。"
            )
        except OSError as exc:
            LOGGER.warning("Wind capture glob interrupted: %s", exc)
        return candidates

    def _read_capture(self, symbol: str) -> tuple[dict[str, Any], float]:
        for path in self._candidate_captures(symbol):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    capture = json.load(handle)
                timestamp_ms = capture.get("callback_epoch_ms")
                timestamp = (
                    float(timestamp_ms) / 1000.0
                    if timestamp_ms is not None
                    else path.stat().st_mtime
                )
                if timestamp < self.capture_not_before_epoch:
                    continue
                decoded = decode_probe_capture(capture)
                for values in decoded["rows"]:
                    if str(values.get("windcode", "")).upper() == symbol:
                        buy = values.get("etfbuyamount")
                        sell = values.get("etfsellamount")
                        if isinstance(buy, (int, float)) and isinstance(
                            sell, (int, float)
                        ):
                            values["netamount"] = buy - sell
                        else:
                            values["netamount"] = None
                        self.last_error = None
                        return values, timestamp
            except (OSError, json.JSONDecodeError, FrameFormatError):
                continue
        raise ProbeError(f"尚无 {display_symbol(symbol)} 数据")

    def _capture_read_done(
        self,
        symbol: str,
        task: asyncio.Task[tuple[dict[str, Any], float]],
    ) -> None:
        if task.cancelled():
            return
        # Always retrieve the terminal exception.  A timeout can race with a
        # worker finishing, otherwise asyncio reports "Task exception was
        # never retrieved" even though the next polling cycle is healthy.
        try:
            error = task.exception()
        except (asyncio.CancelledError, Exception):
            error = None
        if error is not None and self.capture_read_tasks.get(symbol) is task:
            self.capture_read_tasks.pop(symbol, None)

    async def _read_capture_async(
        self, symbol: str
    ) -> tuple[dict[str, Any], float]:
        """Read one symbol without allowing macOS TCC to stall the API."""

        task = self.capture_read_tasks.get(symbol)
        if task is None:
            task = asyncio.create_task(
                asyncio.to_thread(self._read_capture, symbol),
                name=f"capture-read-{symbol}",
            )
            self.capture_read_tasks[symbol] = task
            task.add_done_callback(
                lambda item, code=symbol: self._capture_read_done(code, item)
            )
        try:
            return await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self.CAPTURE_READ_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            self.last_error = (
                "Wind 数据目录读取超时。请在 macOS“系统设置 → "
                "隐私与安全性 → 完全磁盘访问权限”中允许 ETF监控主机。"
            )
            raise ProbeError(self.last_error) from exc
        finally:
            if task.done():
                self.capture_read_tasks.pop(symbol, None)

    @staticmethod
    def _change_details(
        previous: dict[str, Any], current: dict[str, Any]
    ) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        for field_name, label, signed in ALERT_FIELDS:
            old = previous.get(field_name)
            new = current.get(field_name)
            if old == new:
                continue
            formatter = signed_number if signed else plain_number
            changes.append(
                {
                    "field": field_name,
                    "label": label,
                    "old": old,
                    "new": new,
                    "text": f"{label} {formatter(old)} → {formatter(new)}",
                }
            )
        return changes

    async def poll_once(self) -> list[dict[str, Any]]:
        async with self.poll_lock:
            return await self._poll_once_locked()

    async def _poll_once_locked(self) -> list[dict[str, Any]]:
        change_items: list[dict[str, Any]] = []
        symbols = list(self.config.symbols)
        read_results = await asyncio.gather(
            *(self._read_capture_async(symbol) for symbol in symbols),
            return_exceptions=True,
        )
        for symbol, read_result in zip(symbols, read_results):
            state = self.states.setdefault(symbol, SymbolState(symbol))
            if isinstance(read_result, BaseException):
                continue
            values, timestamp = read_result
            if timestamp < self.capture_not_before_epoch:
                continue
            current = {
                "etfbuynumber": values.get("etfbuynumber"),
                "etfbuyamount": values.get("etfbuyamount"),
                "etfsellnumber": values.get("etfsellnumber"),
                "etfsellamount": values.get("etfsellamount"),
                "netamount": values.get("netamount"),
            }
            previous = self.baselines.get(symbol)
            state.values = current
            state.updated_at = timestamp
            if state.status in {"waiting", "stopped"} and not self.monitoring:
                state.status = "cached"
            self.baselines[symbol] = current
            if previous is None:
                state.opportunity = classify_intraday_opportunity(
                    None, current, state.pcf
                )
                continue
            changes = self._change_details(previous, current)
            if not changes:
                # Opportunity is an event-derived state.  A quiet polling
                # cycle must not erase the latest intraday signal; clients
                # keep that signal visible until a later share change or
                # their own baseline reset.
                continue
            state.opportunity = classify_intraday_opportunity(
                previous, current, state.pcf
            )
            state.last_change_at = timestamp_iso(timestamp)
            state.last_change = changes
            history_record = {
                "event_time": state.last_change_at,
                "observed_at": now_iso(),
                "symbol": display_symbol(symbol),
                "windcode": symbol,
                "name": state.custom_name or str(state.pcf.get("fund_name") or ""),
                "previous": dict(previous),
                "current": dict(current),
                "changes": list(changes),
                "opportunity": dict(state.opportunity),
            }
            try:
                self.history_store.append(history_record)
            except OSError:
                LOGGER.exception("failed to append change history for %s", symbol)
            LOGGER.info(
                "share change %s at %s: %s",
                symbol,
                state.last_change_at,
                "；".join(str(change.get("text") or "") for change in changes),
            )
            change_items.append(
                {
                    "symbol": display_symbol(symbol),
                    "windcode": symbol,
                    "changes": changes,
                    "current": state.to_dict(),
                }
            )
        if change_items:
            event = {
                "type": "change",
                "protocol": PROTOCOL_VERSION,
                "server_time": now_iso(),
                "items": change_items,
            }
            await self.manager.broadcast(event)
        return change_items

    async def _load_cached_pcf(self) -> None:
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        for symbol in self.config.symbols:
            payload = await asyncio.to_thread(
                self.pcf_service.load_cached_symbol, symbol, today
            )
            if payload is None:
                continue
            state = self.states.setdefault(symbol, SymbolState(symbol))
            state.pcf = self.pcf_service.summary_for(symbol)
            if not state.opportunity:
                state.opportunity = classify_intraday_opportunity(
                    None, state.values, state.pcf, reference_day=today
                )

    async def refresh_pcf(
        self,
        symbols: list[str] | None = None,
        *,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        """Refresh the Shenzhen PCF queue sequentially under the shared limiter."""

        target_symbols = symbols or self.config.symbols
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        async with self.pcf_lock:
            for symbol in target_symbols:
                self.pcf_last_attempt[symbol] = time.time()
                attempt_key = (today, symbol)
                self.pcf_attempt_counts[attempt_key] = (
                    self.pcf_attempt_counts.get(attempt_key, 0) + 1
                )
                payload = await asyncio.to_thread(
                    self.pcf_service.ensure_symbol,
                    symbol,
                    today,
                    force_refresh=force_refresh,
                )
                state = self.states.setdefault(symbol, SymbolState(symbol))
                state.pcf = self.pcf_service.summary_for(symbol)
                if not state.opportunity:
                    state.opportunity = classify_intraday_opportunity(
                        None, state.values, state.pcf, reference_day=today
                    )
                if payload.get("status") == "error":
                    LOGGER.warning("PCF refresh failed for %s: %s", symbol, payload.get("error"))
        event = self.snapshot_event()
        await self.manager.broadcast(event)
        return event

    async def pcf_detail(
        self, symbol: str, *, allow_refresh: bool = False
    ) -> dict[str, Any]:
        normalized = normalize_symbol(symbol)
        if normalized not in self.config.symbols:
            raise ValueError("该标的不在观察列表中")
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        detail = self.pcf_service.detail_for(normalized)
        needs_refresh = (
            detail is None
            or detail.get("trading_day") != today.isoformat()
            or detail.get("status") != "ready"
        )
        if needs_refresh and allow_refresh:
            await self.refresh_pcf([normalized])
            detail = self.pcf_service.detail_for(normalized)
        if detail is None:
            raise ValueError("PCF 尚未就绪")
        result = dict(detail)
        state = self.states.setdefault(normalized, SymbolState(normalized))
        result["custom_name"] = state.custom_name
        result["name"] = state.custom_name or str(result.get("fund_name") or "")
        result["opportunity"] = state.opportunity or classify_intraday_opportunity(
            None, state.values, state.pcf, reference_day=today
        )
        return result

    @staticmethod
    def _pcf_auto_fetch_start(pcf: dict[str, Any]) -> datetime_time:
        configured_start = datetime_time.fromisoformat(
            str(pcf.get("fetch_start", "08:30"))
        )
        return max(configured_start, datetime_time(8, 30))

    def _inside_pcf_window(self) -> bool:
        pcf = self.config.data.get("pcf", {})
        if not pcf.get("enabled", True):
            return False
        timezone = ZoneInfo(str(pcf.get("timezone", "Asia/Shanghai")))
        now = datetime.now(timezone)
        if now.weekday() not in pcf.get("weekdays", [0, 1, 2, 3, 4]):
            return False
        # SZSE does not reliably publish the current trading day's PCF before
        # 08:30.  Keep this floor even for installations carrying the legacy
        # 08:00 setting; later user-configured starts are still respected.
        start = self._pcf_auto_fetch_start(pcf)
        end = datetime_time.fromisoformat(str(pcf.get("fetch_end", "23:00")))
        return start <= now.time().replace(tzinfo=None) <= end

    async def _pcf_loop(self) -> None:
        await asyncio.sleep(0.25)
        while True:
            try:
                if self._inside_pcf_window():
                    pcf = self.config.data.get("pcf", {})
                    retry = max(60, int(pcf.get("retry_interval_seconds", 900)))
                    max_attempts = max(
                        1, int(pcf.get("max_auto_attempts_per_symbol_per_day", 8))
                    )
                    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
                    due = []
                    for symbol in self.config.symbols:
                        if self.pcf_service.is_cached(symbol, today):
                            if self.pcf_service.detail_for(symbol) is None:
                                payload = await asyncio.to_thread(
                                    self.pcf_service.load_cached_symbol, symbol, today
                                )
                                if payload is not None:
                                    state = self.states.setdefault(symbol, SymbolState(symbol))
                                    state.pcf = self.pcf_service.summary_for(symbol)
                            continue
                        last_attempt = self.pcf_last_attempt.get(symbol, 0.0)
                        attempts = self.pcf_attempt_counts.get((today, symbol), 0)
                        if attempts < max_attempts and time.time() - last_attempt >= retry:
                            due.append(symbol)
                    if due:
                        await self.refresh_pcf(due)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("daily PCF loop failed")
            await asyncio.sleep(60)

    async def _poll_loop(self) -> None:
        while True:
            try:
                # Keep cached values in memory outside the collection window.
                # Avoid touching Wind's protected container all night when no
                # subscription can produce a new callback.
                if self.monitoring or self._inside_schedule():
                    await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.exception("capture polling failed")
                await self.manager.broadcast(self.status_event(f"读取错误：{exc}"))
            await asyncio.sleep(self.interval)

    def _wind_config(self) -> dict[str, Any]:
        value = self.config.data.get("wind", {})
        return value if isinstance(value, dict) else {}

    def _persist_wind_runtime(self) -> None:
        config = self._wind_config()
        config["last_managed_day"] = (
            self.wind_managed_day.isoformat() if self.wind_managed_day else ""
        )
        config["last_shutdown_day"] = (
            self.wind_shutdown_day.isoformat() if self.wind_shutdown_day else ""
        )
        config["manual_override_day"] = (
            self.wind_manual_override_day.isoformat()
            if self.wind_manual_override_day
            else ""
        )
        self.config.data["wind"] = config
        self.config.save()

    def _wind_now(self) -> datetime:
        timezone = ZoneInfo(str(self._wind_config().get("timezone", "Asia/Shanghai")))
        return datetime.now(timezone)

    def _wind_is_workday(self, now: datetime) -> bool:
        return now.weekday() in self._wind_config().get(
            "weekdays", [0, 1, 2, 3, 4]
        )

    def _wind_launch_window(self, now: datetime) -> bool:
        config = self._wind_config()
        if not config.get("enabled", True) or not self._wind_is_workday(now):
            return False
        launch = datetime_time.fromisoformat(str(config.get("launch", "09:10")))
        shutdown = datetime_time.fromisoformat(str(config.get("shutdown", "15:00")))
        current = now.time().replace(tzinfo=None)
        return launch <= current < shutdown

    def _wind_shutdown_due(self, now: datetime) -> bool:
        config = self._wind_config()
        if not config.get("enabled", True) or not self._wind_is_workday(now):
            return False
        shutdown = datetime_time.fromisoformat(str(config.get("shutdown", "15:00")))
        return now.time().replace(tzinfo=None) >= shutdown

    @staticmethod
    def _wind_ready_for_subscription(status: dict[str, Any]) -> bool:
        return bool(status.get("running") and status.get("tbapi_loaded"))

    def _wind_subscription_stable(self, status: dict[str, Any]) -> bool:
        if not self._wind_ready_for_subscription(status):
            return False
        stable_seconds = max(
            0.0,
            float(
                self._wind_config().get(
                    "subscription_ready_stable_seconds", 30
                )
            ),
        )
        return (
            self.wind_tbapi_ready_since > 0
            and time.monotonic() - self.wind_tbapi_ready_since >= stable_seconds
        )

    async def _controller_wind_status(self) -> dict[str, Any]:
        method = getattr(self.controller, "wind_process_status", None)
        if method is None:
            return {"running": False, "pids": [], "tbapi_loaded": False}
        value = await asyncio.to_thread(method)
        return value if isinstance(value, dict) else {}

    async def refresh_wind_status(
        self, *, preserve_lifecycle_action: bool = True
    ) -> dict[str, Any]:
        try:
            status = await self._controller_wind_status()
            running = bool(status.get("running"))
            tbapi_loaded = bool(status.get("tbapi_loaded"))
            if running and tbapi_loaded:
                if self.wind_tbapi_ready_since <= 0:
                    self.wind_tbapi_ready_since = time.monotonic()
            else:
                self.wind_tbapi_ready_since = 0.0
            pids = status.get("pids")
            pid_count = len(pids) if isinstance(pids, list) else int(running)
            transient = self.wind_state.get("state") in {
                "launching",
                "warming",
                "subscribing",
                "stopping",
                "quitting",
                "cleaning",
            }
            if (
                preserve_lifecycle_action
                and self.operation_lock.locked()
                and transient
            ):
                self.wind_state.update(
                    running=running,
                    tbapi_loaded=tbapi_loaded,
                    pid_count=pid_count,
                )
                return dict(self.wind_state)
            if self.monitoring and running:
                state, label = "monitoring", "Wind 监控中"
            elif running and tbapi_loaded:
                state, label = "ready", "Wind 已就绪"
            elif running:
                state, label = "running", "Wind 已运行，等待 TBAPI 就绪"
            else:
                state, label = "stopped", "Wind 未运行"
            self.wind_state.update(
                state=state,
                label=label,
                running=running,
                tbapi_loaded=tbapi_loaded,
                pid_count=pid_count,
            )
        except Exception as exc:
            self.wind_state.update(
                state="error",
                label="Wind 状态检测失败",
                last_error=str(exc),
            )
        return dict(self.wind_state)

    async def start_wind(self, reason: str = "manual") -> dict[str, Any]:
        async with self.operation_lock:
            self.wind_state.update(
                state="launching",
                label="正在启动 Wind",
                last_action=f"{now_iso()} 启动（{reason}）",
                last_error=None,
            )
            await self.manager.broadcast(self.status_event("正在启动 Wind"))
            try:
                status = await self._controller_wind_status()
                if not status.get("running"):
                    if self._wind_config().get("cleanup_generated_dylibs", True):
                        cleanup = getattr(
                            self.controller, "cleanup_generated_dylibs", None
                        )
                        if cleanup is not None:
                            cleanup_result = await asyncio.to_thread(cleanup)
                            self.wind_state["cleanup_deleted_count"] = int(
                                cleanup_result.get("deleted_count", 0)
                            )
                            self.wind_state["cleanup_deleted_bytes"] = int(
                                cleanup_result.get("deleted_bytes", 0)
                            )
                    launch = getattr(self.controller, "launch_wind", None)
                    if launch is None:
                        raise ProbeError("当前控制器不支持启动 Wind。")
                    timeout = float(
                        self._wind_config().get("startup_timeout_seconds", 30)
                    )
                    await asyncio.to_thread(launch, timeout)
                now = self._wind_now()
                if reason == "schedule" or self._wind_launch_window(now):
                    self.wind_managed_day = now.date()
                    if self.wind_shutdown_day == now.date():
                        self.wind_shutdown_day = None
                    if self.wind_manual_override_day != now.date():
                        self.wind_manual_override_day = None
                elif reason == "local-manual" and self._wind_shutdown_due(now):
                    # A user explicitly starting Wind after the daily shutdown
                    # is an intentional exception; do not fight that choice.
                    self.wind_manual_override_day = now.date()
                self._persist_wind_runtime()
                await self.refresh_wind_status(preserve_lifecycle_action=False)
                self.wind_state["last_action"] = f"{now_iso()} Wind 已启动（{reason}）"
                await self.manager.broadcast(self.status_event("Wind 已启动"))
                return self.snapshot_event()
            except Exception as exc:
                self.wind_state.update(
                    state="error",
                    label="Wind 启动失败",
                    last_error=str(exc),
                )
                await self.manager.broadcast(
                    self.status_event(f"Wind 启动失败：{exc}")
                )
                raise

    async def shutdown_wind_and_cleanup(
        self, reason: str = "manual"
    ) -> dict[str, Any]:
        async with self.operation_lock:
            self.wind_state.update(
                state="stopping",
                label="正在停止订阅",
                last_action=f"{now_iso()} 关闭并清理（{reason}）",
                last_error=None,
            )
            await self.manager.broadcast(self.status_event("正在停止订阅并关闭 Wind"))
            try:
                if self.monitoring or self.sessions:
                    await self._stop_monitoring_locked(f"wind-shutdown:{reason}")
                status = await self._controller_wind_status()
                if status.get("running"):
                    self.wind_state.update(
                        state="quitting", label="正在关闭 Wind"
                    )
                    await self.manager.broadcast(self.status_event("正在关闭 Wind"))
                    terminate = getattr(self.controller, "terminate_wind", None)
                    if terminate is None:
                        raise ProbeError("当前控制器不支持关闭 Wind。")
                    timeout = float(
                        self._wind_config().get("terminate_timeout_seconds", 30)
                    )
                    await asyncio.to_thread(terminate, timeout)
                status = await self._controller_wind_status()
                if status.get("running"):
                    raise ProbeError("Wind 尚未完全退出，拒绝清理 dylib。")

                cleanup_result = {"deleted_count": 0, "deleted_bytes": 0}
                if self._wind_config().get("cleanup_generated_dylibs", True):
                    self.wind_state.update(
                        state="cleaning", label="正在清理过期 dylib"
                    )
                    cleanup = getattr(self.controller, "cleanup_generated_dylibs", None)
                    if cleanup is None:
                        raise ProbeError("当前控制器不支持清理临时 dylib。")
                    cleanup_result = await asyncio.to_thread(cleanup)
                self.wind_shutdown_day = self._wind_now().date()
                self.wind_manual_override_day = None
                self._persist_wind_runtime()
                self.wind_state.update(
                    state="cleaned",
                    label="Wind 已关闭并清理",
                    running=False,
                    tbapi_loaded=False,
                    pid_count=0,
                    cleanup_deleted_count=int(
                        cleanup_result.get("deleted_count", 0)
                    ),
                    cleanup_deleted_bytes=int(
                        cleanup_result.get("deleted_bytes", 0)
                    ),
                    last_action=f"{now_iso()} 已关闭并清理（{reason}）",
                    last_error=None,
                )
                count = self.wind_state["cleanup_deleted_count"]
                await self.manager.broadcast(
                    self.status_event(f"Wind 已关闭，清理 {count} 个过期 dylib")
                )
                return self.snapshot_event()
            except Exception as exc:
                self.wind_state.update(
                    state="error",
                    label="关闭或清理失败",
                    last_error=str(exc),
                )
                await self.manager.broadcast(
                    self.status_event(f"关闭或清理 Wind 失败：{exc}")
                )
                raise

    def _inside_schedule(self) -> bool:
        schedule = self.config.data["schedule"]
        if not schedule.get("enabled", True):
            return False
        timezone = ZoneInfo(str(schedule.get("timezone", "Asia/Shanghai")))
        now = datetime.now(timezone)
        if now.weekday() not in schedule.get("weekdays", [0, 1, 2, 3, 4]):
            return False
        start = datetime_time.fromisoformat(
            str(schedule.get("start", "09:15:30"))
        )
        stop = datetime_time.fromisoformat(str(schedule.get("stop", "15:00")))
        return start <= now.time().replace(tzinfo=None) < stop

    def _subscription_warmup_due(self, now: datetime) -> bool:
        schedule = self._schedule_config()
        if not schedule.get("enabled", True):
            return False
        if now.weekday() not in schedule.get("weekdays", [0, 1, 2, 3, 4]):
            return False
        warmup = datetime_time.fromisoformat(
            str(schedule.get("warmup", "09:15:05"))
        )
        start = datetime_time.fromisoformat(
            str(schedule.get("start", "09:15:30"))
        )
        current = now.time().replace(tzinfo=None)
        return warmup <= current < start

    def _schedule_sleep_seconds(self, now: datetime) -> float:
        """Wake exactly at lifecycle boundaries without busy polling all day."""

        schedule = self._schedule_config()
        if not schedule.get("enabled", True):
            return 15.0
        if now.weekday() not in schedule.get("weekdays", [0, 1, 2, 3, 4]):
            return 15.0
        wind = self._wind_config()
        clock_values = [
            str(schedule.get("daily_reset", "09:00")),
            str(wind.get("launch", "09:10")),
            str(schedule.get("warmup", "09:15:05")),
            str(schedule.get("start", "09:15:30")),
            str(schedule.get("stop", "15:00")),
            str(wind.get("shutdown", "15:00")),
        ]
        future_deltas: list[float] = []
        for value in clock_values:
            boundary = datetime.combine(
                now.date(), datetime_time.fromisoformat(value), tzinfo=now.tzinfo
            )
            delta = (boundary - now).total_seconds()
            if delta > 0.05:
                future_deltas.append(delta)
        return max(0.2, min(15.0, min(future_deltas, default=15.0)))

    def _past_schedule_stop(self) -> bool:
        schedule = self.config.data["schedule"]
        if not schedule.get("enabled", True):
            return False
        timezone = ZoneInfo(str(schedule.get("timezone", "Asia/Shanghai")))
        now = datetime.now(timezone)
        if now.weekday() not in schedule.get("weekdays", [0, 1, 2, 3, 4]):
            return False
        stop = datetime_time.fromisoformat(str(schedule.get("stop", "15:00")))
        return now.time().replace(tzinfo=None) >= stop

    async def _schedule_loop(self) -> None:
        while True:
            try:
                now = self._wind_now()
                await self._maybe_reset_daily_realtime_state(now)
                wind_status = await self.refresh_wind_status()
                inside = self._inside_schedule()
                if self.monitoring and self.sessions:
                    expected_pids = {session.pid for session in self.sessions.values()}
                    process_alive = True
                    for pid in expected_pids:
                        try:
                            os.kill(pid, 0)
                        except (ProcessLookupError, PermissionError):
                            process_alive = False
                            break
                    if not process_alive:
                        self.sessions.clear()
                        self.monitoring = False
                        self.started_by = None
                        for state in self.states.values():
                            state.status = "reconnecting"
                            state.sub_id = None
                        await self.manager.broadcast(
                            self.status_event("检测到主程序重启，等待自动重建订阅")
                        )

                wind_config = self._wind_config()
                wind_automatic = bool(wind_config.get("enabled", True))
                if self._wind_launch_window(now) and not wind_status.get("running"):
                    cooldown = max(
                        15.0,
                        float(wind_config.get("relaunch_cooldown_seconds", 120)),
                    )
                    if time.monotonic() - self.last_wind_launch_attempt >= cooldown:
                        self.last_wind_launch_attempt = time.monotonic()
                        try:
                            await self.start_wind("schedule")
                            wind_status = dict(self.wind_state)
                        except Exception as exc:
                            LOGGER.warning("scheduled Wind launch failed: %s", exc)
                elif self._wind_launch_window(now) and wind_status.get("running"):
                    runtime_changed = self.wind_managed_day != now.date()
                    self.wind_managed_day = now.date()
                    # A running, verified Wind instance inside the managed window
                    # belongs to today's lifecycle even if the host service was
                    # restarted after 09:10.  Clear a stale same-day shutdown
                    # marker so the 15:00 close remains guaranteed.
                    if self.wind_shutdown_day == now.date():
                        self.wind_shutdown_day = None
                        runtime_changed = True
                    if runtime_changed:
                        self._persist_wind_runtime()

                if (
                    self._subscription_warmup_due(now)
                    and not self.monitoring
                    and self._wind_subscription_stable(wind_status)
                ):
                    try:
                        await self.warmup_tbapi("schedule-09:15:05")
                        wind_status = dict(self.wind_state)
                    except Exception as exc:
                        LOGGER.warning("scheduled TBAPI warm-up failed: %s", exc)

                if (
                    inside
                    and not self.monitoring
                    and self._wind_subscription_stable(wind_status)
                ):
                    retry_seconds = max(
                        15.0,
                        float(wind_config.get("subscription_retry_seconds", 30)),
                    )
                    if time.monotonic() - self.last_subscription_attempt >= retry_seconds:
                        self.last_subscription_attempt = time.monotonic()
                        try:
                            await self.start_monitoring("schedule")
                        except Exception as exc:
                            LOGGER.warning(
                                "scheduled subscription retry failed: %s", exc
                            )
                elif self._wind_shutdown_due(now) or (
                    not wind_automatic and self._past_schedule_stop()
                ):
                    if (
                        wind_automatic
                        and self.wind_shutdown_day != now.date()
                        and self.wind_manual_override_day != now.date()
                    ):
                        retry_seconds = max(
                            30.0,
                            float(wind_config.get("shutdown_retry_seconds", 120)),
                        )
                        if (
                            time.monotonic() - self.last_wind_shutdown_attempt
                            >= retry_seconds
                        ):
                            self.last_wind_shutdown_attempt = time.monotonic()
                            try:
                                await self.shutdown_wind_and_cleanup("schedule-end")
                            except Exception as exc:
                                LOGGER.error(
                                    "scheduled Wind shutdown failed: %s", exc
                                )
                    elif not wind_automatic and self.monitoring:
                        await self.stop_monitoring("schedule-end")
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("schedule loop failed")
            await asyncio.sleep(self._schedule_sleep_seconds(self._wind_now()))

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            await self.manager.broadcast(
                {
                    "type": "heartbeat",
                    "protocol": PROTOCOL_VERSION,
                    "server_time": now_iso(),
                    "monitoring": self.monitoring,
                    "wind": dict(self.wind_state),
                }
            )


class WatchlistRequest(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=200)


class SymbolNameRequest(BaseModel):
    name: str = Field(default="", max_length=40)


def require_loopback(request: Request) -> None:
    """Reject data-source mutations coming from LAN clients."""

    host = request.client.host if request.client is not None else ""
    try:
        allowed = ipaddress.ip_address(host).is_loopback
    except ValueError:
        allowed = host == "localhost"
    if not allowed:
        raise HTTPException(
            status_code=403,
            detail="该操作只允许在 Mac-home 主机本机执行。",
        )


def create_app(
    config_path: Path = DEFAULT_CONFIG_PATH,
    capture_dir: Path = WIND_TEMP_DIR,
    controller: ProbeController | None = None,
    pcf_service: PcfService | None = None,
    history_store: ChangeHistoryStore | None = None,
) -> FastAPI:
    config = ConfigStore(config_path)
    engine = MonitorEngine(
        config,
        capture_dir=capture_dir,
        controller=controller,
        pcf_service=pcf_service,
        history_store=history_store,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await engine.start_background_tasks()
        try:
            yield
        finally:
            await engine.shutdown()

    app = FastAPI(title="ETF Monitor", version="1.0", lifespan=lifespan)
    app.state.config = config
    app.state.engine = engine

    @app.get("/", include_in_schema=False)
    async def web_monitor() -> FileResponse:
        return FileResponse(WEB_INDEX_PATH)

    @app.get("/api/v1/health")
    async def health() -> dict[str, Any]:
        await engine.refresh_wind_status()
        return {
            "ok": True,
            "protocol": PROTOCOL_VERSION,
            "server_time": now_iso(),
            "monitoring": engine.monitoring,
            "started_by": engine.started_by,
            "inside_schedule": engine._inside_schedule(),
            "symbols": len(config.symbols),
            "connections": len(engine.manager.connections),
            "last_error": engine.last_error,
            "pcf_enabled": engine.pcf_service.enabled,
            "pcf_cached": sum(
                1 for state in engine.states.values() if state.pcf.get("status") == "ready"
            ),
            "wind": dict(engine.wind_state),
        }

    @app.get("/api/v1/snapshot")
    async def snapshot() -> dict[str, Any]:
        await engine.refresh_wind_status()
        await engine.poll_once()
        return engine.snapshot_event()

    @app.get("/api/v1/watchlist")
    async def watchlist() -> dict[str, Any]:
        return {"symbols": [display_symbol(s) for s in config.symbols]}

    @app.get("/api/v1/history")
    async def change_history(
        history_date: str | None = Query(default=None, alias="date"),
        symbol: str | None = Query(default=None),
        limit: int = Query(default=500, ge=1, le=20_000),
    ) -> dict[str, Any]:
        try:
            target_day = (
                date.fromisoformat(history_date)
                if history_date
                else datetime.now(ZoneInfo("Asia/Shanghai")).date()
            )
            if symbol:
                normalize_symbol(symbol)
            return await engine.change_history(
                target_day, symbol=symbol, limit=limit
            )
        except (ValueError, ProbeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.put("/api/v1/watchlist")
    async def set_watchlist(
        body: WatchlistRequest, request: Request
    ) -> dict[str, Any]:
        require_loopback(request)
        try:
            return await engine.update_watchlist(body.symbols)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.put("/api/v1/symbols/{symbol}/name")
    async def set_symbol_name(
        symbol: str, body: SymbolNameRequest, request: Request
    ) -> dict[str, Any]:
        require_loopback(request)
        try:
            return await engine.update_symbol_name(symbol, body.name)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/monitor/start")
    async def start_monitor(request: Request) -> dict[str, Any]:
        require_loopback(request)
        try:
            return await engine.start_monitoring("local-manual")
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/v1/monitor/stop")
    async def stop_monitor(request: Request) -> dict[str, Any]:
        require_loopback(request)
        return await engine.stop_monitoring("local-manual")

    @app.get("/api/v1/wind/status")
    async def wind_status(request: Request) -> dict[str, Any]:
        require_loopback(request)
        await engine.refresh_wind_status()
        return engine.snapshot_event()

    @app.post("/api/v1/wind/start")
    async def start_wind(request: Request) -> dict[str, Any]:
        require_loopback(request)
        try:
            return await engine.start_wind("local-manual")
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/v1/wind/shutdown-cleanup")
    async def shutdown_wind(request: Request) -> dict[str, Any]:
        require_loopback(request)
        try:
            return await engine.shutdown_wind_and_cleanup("local-manual")
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/v1/pcf")
    async def pcf_summaries() -> dict[str, Any]:
        return {
            "server_time": now_iso(),
            "items": [
                engine.pcf_service.summary_for(symbol) for symbol in config.symbols
            ],
        }

    @app.get("/api/v1/pcf/{symbol}")
    async def pcf_detail(symbol: str, request: Request) -> dict[str, Any]:
        try:
            host = request.client.host if request.client is not None else ""
            try:
                allow_refresh = ipaddress.ip_address(host).is_loopback
            except ValueError:
                allow_refresh = host == "localhost"
            return await engine.pcf_detail(symbol, allow_refresh=allow_refresh)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/pcf/refresh")
    async def refresh_pcf(request: Request) -> dict[str, Any]:
        require_loopback(request)
        return await engine.refresh_pcf()

    @app.websocket("/ws/v1/changes")
    async def websocket_changes(websocket: WebSocket) -> None:
        await engine.manager.connect(websocket)
        try:
            await engine.refresh_wind_status()
            await websocket.send_json(engine.snapshot_event())
            while True:
                message = await websocket.receive_json()
                message_type = message.get("type")
                if message_type == "get_snapshot":
                    await engine.refresh_wind_status()
                    await engine.poll_once()
                    await websocket.send_json(engine.snapshot_event())
                elif message_type == "ping":
                    await websocket.send_json(
                        {"type": "pong", "server_time": now_iso()}
                    )
        except Exception:
            pass
        finally:
            await engine.manager.disconnect(websocket)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(
        _request: Request, exc: HTTPException
    ) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--host", help="override configured listen host")
    parser.add_argument("--port", type=int, help="override configured listen port")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_file_logging()
    config = ConfigStore(args.config)
    host = args.host or str(config.data["network"].get("host", "0.0.0.0"))
    port = args.port or int(config.data["network"].get("port", 6787))
    app = create_app(args.config)
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
