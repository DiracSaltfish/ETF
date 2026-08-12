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
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket
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
                "policy_version": 3,
                "timezone": "Asia/Shanghai",
                "weekdays": [0, 1, 2, 3, 4],
                "start": "09:10",
                "stop": "15:00",
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
                if int(loaded_schedule.get("policy_version", 1)) < 3:
                    # One-time migration from the former 09:15-15:10 policy.
                    # The host owns collection; remote clients are read-only.
                    data["schedule"]["start"] = "09:10"
                    data["schedule"]["stop"] = "15:00"
                    data["schedule"]["policy_version"] = 3
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
        self.operation_lock = asyncio.Lock()
        self.pcf_lock = asyncio.Lock()
        self.pcf_last_attempt: dict[str, float] = {}
        self.pcf_attempt_counts: dict[tuple[date, str], int] = {}
        self.capture_read_tasks: dict[
            str, asyncio.Task[tuple[dict[str, Any], float]]
        ] = {}
        self.tasks: list[asyncio.Task[Any]] = []
        self.shutting_down = False

    @property
    def interval(self) -> float:
        return max(0.5, float(self.config.data["refresh_interval_seconds"]))

    def snapshot_event(self) -> dict[str, Any]:
        return {
            "type": "snapshot",
            "protocol": PROTOCOL_VERSION,
            "server_time": now_iso(),
            "monitoring": self.monitoring,
            "started_by": self.started_by,
            "last_error": self.last_error,
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
        }

    async def start_background_tasks(self) -> None:
        await self._load_cached_pcf()
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
        if self.sessions:
            await self.stop_monitoring("shutdown")

    async def start_monitoring(self, reason: str = "manual") -> dict[str, Any]:
        async with self.operation_lock:
            if self.monitoring and self.sessions:
                return self.snapshot_event()
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
                for symbol in symbols:
                    self.states[symbol].status = "error"
                await self.manager.broadcast(self.status_event(f"订阅失败：{exc}"))
                raise
            self.sessions = sessions
            self.monitoring = bool(sessions)
            self.started_by = reason if self.monitoring else None
            if self.monitoring:
                self.last_error = None
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
                self.status_event(
                    f"正在监控 {len(sessions)}/{len(symbols)} 个标的"
                )
            )
            await self.poll_once()
            return self.snapshot_event()

    async def stop_monitoring(self, reason: str = "manual") -> dict[str, Any]:
        async with self.operation_lock:
            sessions = dict(self.sessions)
            if sessions:
                errors = await asyncio.to_thread(
                    self.controller.stop_many, sessions
                )
            else:
                errors = {}
            self.sessions.clear()
            self.monitoring = False
            self.started_by = None
            for symbol, state in self.states.items():
                state.status = "stopped" if symbol not in errors else "error"
                state.sub_id = None
            await self.manager.broadcast(self.status_event(f"监控已停止：{reason}"))
            return self.snapshot_event()

    async def update_watchlist(self, symbols: list[str]) -> dict[str, Any]:
        was_monitoring = self.monitoring
        if was_monitoring:
            await self.stop_monitoring("更新观察列表")
        normalized = self.config.set_symbols(symbols)
        self.states = {
            symbol: self.states.get(
                symbol,
                SymbolState(symbol, custom_name=self.config.symbol_name(symbol)),
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
            return await self.start_monitoring("watchlist-update")
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
        return candidates

    def _read_capture(self, symbol: str) -> tuple[dict[str, Any], float]:
        for path in self._candidate_captures(symbol):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    capture = json.load(handle)
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
                        timestamp_ms = capture.get("callback_epoch_ms")
                        timestamp = (
                            float(timestamp_ms) / 1000.0
                            if timestamp_ms is not None
                            else path.stat().st_mtime
                        )
                        self.last_error = None
                        return values, timestamp
            except (OSError, json.JSONDecodeError, FrameFormatError):
                continue
        raise ProbeError(f"尚无 {display_symbol(symbol)} 数据")

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
            state.last_change_at = now_iso()
            state.last_change = changes
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
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.exception("capture polling failed")
                await self.manager.broadcast(self.status_event(f"读取错误：{exc}"))
            await asyncio.sleep(self.interval)

    def _inside_schedule(self) -> bool:
        schedule = self.config.data["schedule"]
        if not schedule.get("enabled", True):
            return False
        timezone = ZoneInfo(str(schedule.get("timezone", "Asia/Shanghai")))
        now = datetime.now(timezone)
        if now.weekday() not in schedule.get("weekdays", [0, 1, 2, 3, 4]):
            return False
        start = datetime_time.fromisoformat(str(schedule.get("start", "09:10")))
        stop = datetime_time.fromisoformat(str(schedule.get("stop", "15:00")))
        return start <= now.time().replace(tzinfo=None) < stop

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
                if inside and not self.monitoring:
                    try:
                        await self.start_monitoring("schedule")
                    except Exception as exc:
                        LOGGER.warning("scheduled subscription retry failed: %s", exc)
                elif self._past_schedule_stop() and self.monitoring:
                    await self.stop_monitoring("schedule-end")
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("schedule loop failed")
            await asyncio.sleep(15)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            await self.manager.broadcast(
                {
                    "type": "heartbeat",
                    "protocol": PROTOCOL_VERSION,
                    "server_time": now_iso(),
                    "monitoring": self.monitoring,
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
) -> FastAPI:
    config = ConfigStore(config_path)
    engine = MonitorEngine(
        config,
        capture_dir=capture_dir,
        controller=controller,
        pcf_service=pcf_service,
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
        }

    @app.get("/api/v1/snapshot")
    async def snapshot() -> dict[str, Any]:
        await engine.poll_once()
        return engine.snapshot_event()

    @app.get("/api/v1/watchlist")
    async def watchlist() -> dict[str, Any]:
        return {"symbols": [display_symbol(s) for s in config.symbols]}

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
            await websocket.send_json(engine.snapshot_event())
            while True:
                message = await websocket.receive_json()
                message_type = message.get("type")
                if message_type == "get_snapshot":
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
