#!/usr/bin/env python3
"""PyQt6 desktop UI for Wind ETF real-time subscription/redemption data."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PyQt6.QtCore import QRegularExpression, QSettings, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QRegularExpressionValidator
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from wind_tbapi_frame_parser import FrameFormatError, decode_probe_capture


SOURCE_DIR = Path(__file__).resolve().parent
# PyInstaller places bundled resources under ``sys._MEIPASS``.  Keep code and
# dylib resources read-only there, while runtime state goes to Application
# Support so a signed/read-only .app can still operate normally.
APP_DIR = Path(getattr(sys, "_MEIPASS", SOURCE_DIR))
if getattr(sys, "frozen", False):
    USER_DATA_DIR = Path.home() / "Library" / "Application Support" / "ETFDelivery"
else:
    USER_DATA_DIR = SOURCE_DIR
APP_LOG_PATH = USER_DATA_DIR / "logs" / "etf_realtime_ui.log"
PROBE_SOURCE = APP_DIR / "wind_tbapi_runtime_probe.c"
PROBE_BINARY = APP_DIR / "libwind_tbapi_runtime_probe.dylib"
WIND_TEMP_DIR = (
    Path.home() / "Library" / "Containers" / "com.windin.mac.free" / "Data" / "tmp"
)
WIND_PROBE_DIR = WIND_TEMP_DIR / "wind_tbapi_probe"
LLDB = Path("/Library/Developer/CommandLineTools/usr/bin/lldb")
WIND_PROCESS_NAME = "WindPersonFree"
WIND_APP_BUNDLE_ID = "com.windin.mac.free"
WIND_OPEN = Path("/usr/bin/open")
WIND_LSOF = Path("/usr/sbin/lsof")
WIND_PS = Path("/bin/ps")
WIND_PERL = Path("/usr/bin/perl")
WIND_EXECUTABLE = Path(
    "/Applications/WindPersonFree.app/Contents/MacOS/WindPersonFree"
)
SYMBOL_PATTERN = re.compile(r"^[0-9]{6}$")
WIND_SYMBOL_PATTERN = re.compile(r"^[0-9]{6}\.SZ$")
GENERATED_PROBE_PATTERN = re.compile(
    r"^libwind_tbapi_runtime_[0-9]{6}_SZ_[0-9]+_[0-9]+_[0-9a-f]{8}\.dylib$"
)
GENERATED_PROBE_CLEANUP_SCRIPT = r"""
use strict;
use warnings;
use Cwd qw(realpath);

my $root = shift @ARGV;
if (!-e $root) {
    print "0\t0\n";
    exit 0;
}
if (-l $root) {
    print STDERR "SYMLINK_ROOT\n";
    exit 3;
}
if (!-d $root) {
    print STDERR "NOT_DIRECTORY\n";
    exit 4;
}
my $resolved = realpath($root);
if (!defined $resolved) {
    print STDERR "RESOLVE_FAILED:$!\n";
    exit 5;
}
opendir(my $dir_handle, $resolved) or die "OPEN_FAILED:$!\n";
my ($deleted_count, $deleted_bytes) = (0, 0);
while (my $name = readdir($dir_handle)) {
    next unless $name =~
        m{\Alibwind_tbapi_runtime_[0-9]{6}_SZ_[0-9]+_[0-9]+_[0-9a-f]{8}\.dylib\z};
    my $path = "$resolved/$name";
    my @metadata = lstat($path);
    next unless @metadata;
    next if -l _;
    next unless -f _;
    my $size = $metadata[7] || 0;
    unlink($path) or die "UNLINK_FAILED:$name:$!\n";
    $deleted_count += 1;
    $deleted_bytes += $size;
}
closedir($dir_handle);
print "$deleted_count\t$deleted_bytes\n";
"""


def normalize_symbol(code: str) -> str:
    """Convert the six-digit UI code to the full Wind Shenzhen code."""

    value = code.strip().upper()
    # Accept the old saved setting during migration, but the visible editor
    # and all new user input use only six digits.
    if WIND_SYMBOL_PATTERN.fullmatch(value):
        return value
    if SYMBOL_PATTERN.fullmatch(value):
        return f"{value}.SZ"
    raise ValueError("请输入 6 位深圳证券代码，例如 159518。")


def display_symbol(windcode: str) -> str:
    return windcode[:-3] if windcode.endswith(".SZ") else windcode


def safe_code(symbol: str) -> str:
    return symbol.replace(".", "_")


def live_capture_path(symbol: str) -> Path:
    return WIND_TEMP_DIR / f"wind_tbapi_live_{safe_code(symbol)}.json"


def live_status_path(symbol: str) -> Path:
    return WIND_TEMP_DIR / f"wind_tbapi_live_{safe_code(symbol)}_status.json"


@dataclass
class SubscriptionSession:
    symbol: str
    pid: int
    dylib_path: Path
    sub_id: int


class ProbeError(RuntimeError):
    pass


class ProbeController:
    LLDB_DLOPEN_FAILED = -9001
    LLDB_DLSYM_FAILED = -9002
    SUBSCRIBED_STATUSES = {"subscribed", "modify_target"}

    def __init__(self, probe_dir: Path = WIND_PROBE_DIR) -> None:
        self.probe_dir = probe_dir

    @staticmethod
    def _candidate_wind_pids() -> list[int]:
        try:
            result = subprocess.run(
                ["pgrep", "-x", WIND_PROCESS_NAME],
                text=True,
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        return [int(line) for line in result.stdout.splitlines() if line.isdigit()]

    @staticmethod
    def _is_expected_wind_pid(pid: int) -> bool:
        """Require the exact official app executable, not only a process name."""

        if not WIND_PS.exists():
            return False
        try:
            result = subprocess.run(
                [str(WIND_PS), "-p", str(pid), "-o", "command="],
                text=True,
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode != 0:
            return False
        try:
            tokens = shlex.split(result.stdout.strip())
        except ValueError:
            return False
        expected = WIND_EXECUTABLE.resolve(strict=False)
        for token in tokens:
            if not token.startswith("/"):
                continue
            try:
                if Path(token).resolve(strict=False) == expected:
                    return True
            except OSError:
                continue
        return False

    @classmethod
    def _wind_pids(cls) -> list[int]:
        return [
            pid
            for pid in cls._candidate_wind_pids()
            if cls._is_expected_wind_pid(pid)
        ]

    @staticmethod
    def _tbapi_loaded(pid: int) -> bool:
        """Check readiness without attaching a debugger or injecting code."""

        if not WIND_LSOF.exists():
            return False
        try:
            result = subprocess.run(
                [str(WIND_LSOF), "-Fn", "-p", str(pid)],
                text=True,
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return "libWind.Cosmos.TBAPI2.dylib" in result.stdout

    def wind_process_status(self) -> dict[str, Any]:
        pids = self._wind_pids()
        return {
            "running": bool(pids),
            "pids": pids,
            "tbapi_loaded": any(self._tbapi_loaded(pid) for pid in pids),
        }

    def launch_wind(self, timeout_seconds: float = 30.0) -> dict[str, Any]:
        current = self.wind_process_status()
        if current["running"]:
            return current
        if not WIND_OPEN.exists():
            raise ProbeError(f"找不到 macOS open 命令：{WIND_OPEN}")
        try:
            result = subprocess.run(
                [str(WIND_OPEN), "-b", WIND_APP_BUNDLE_ID],
                text=True,
                capture_output=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProbeError(f"启动 Wind 失败：{exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise ProbeError(f"启动 Wind 失败：{detail or result.returncode}")

        deadline = time.monotonic() + max(1.0, timeout_seconds)
        while time.monotonic() < deadline:
            current = self.wind_process_status()
            if current["running"]:
                return current
            time.sleep(0.25)
        raise ProbeError("已调用 Wind，但等待进程启动超时。")

    def terminate_wind(self, timeout_seconds: float = 30.0) -> dict[str, Any]:
        """Request termination with SIGTERM; never use SIGKILL automatically."""

        pids = self._wind_pids()
        for pid in pids:
            if not self._is_expected_wind_pid(pid):
                raise ProbeError(
                    f"Wind 进程 {pid} 的应用身份发生变化，拒绝发送关闭信号。"
                )
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            except PermissionError as exc:
                raise ProbeError(f"没有权限关闭 Wind 进程 {pid}。") from exc
        deadline = time.monotonic() + max(1.0, timeout_seconds)
        while self._wind_pids() and time.monotonic() < deadline:
            time.sleep(0.25)
        remaining = self._wind_pids()
        if remaining:
            raise ProbeError(
                "Wind 在温和关闭请求后仍未退出；为保护数据，未强制结束也未清理 dylib。"
            )
        return {"running": False, "pids": [], "tbapi_loaded": False}

    def cleanup_generated_dylibs(
        self, timeout_seconds: float = 15.0
    ) -> dict[str, int]:
        """Remove generated dylibs in a killable helper after Wind has exited.

        macOS can block an ordinary filesystem call indefinitely while an
        App Data/Full Disk Access prompt is awaiting user input.  Keeping all
        directory access inside a bounded helper process lets the host report
        the permission problem and continue running instead of leaking a
        permanently blocked Python worker thread.
        """

        if self._wind_pids():
            raise ProbeError("Wind 仍在运行，拒绝清理可能仍被加载的 dylib。")
        if not WIND_PERL.exists():
            raise ProbeError(f"找不到隔离清理工具：{WIND_PERL}")
        if not self.probe_dir.is_absolute():
            raise ProbeError("探针临时目录必须是绝对路径，已停止清理。")
        try:
            result = subprocess.run(
                [
                    str(WIND_PERL),
                    "-e",
                    GENERATED_PROBE_CLEANUP_SCRIPT,
                    str(self.probe_dir),
                ],
                text=True,
                capture_output=True,
                timeout=max(1.0, float(timeout_seconds)),
            )
        except subprocess.TimeoutExpired as exc:
            raise ProbeError(
                "清理临时 dylib 超时；macOS 可能正在等待“App 数据/完全磁盘访问”授权。"
            ) from exc
        except OSError as exc:
            raise ProbeError(f"无法启动隔离清理工具：{exc}") from exc
        detail = (result.stderr or result.stdout).strip()
        if result.returncode == 3:
            raise ProbeError("探针临时目录是符号链接，已停止清理。")
        if result.returncode == 4:
            raise ProbeError("探针临时路径不是目录，已停止清理。")
        if result.returncode != 0:
            raise ProbeError(f"清理临时 dylib 失败：{detail or result.returncode}")
        parts = result.stdout.strip().split("\t")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise ProbeError("隔离清理工具返回了无法识别的结果。")
        return {"deleted_count": int(parts[0]), "deleted_bytes": int(parts[1])}

    def build_probe(self) -> None:
        if not PROBE_SOURCE.exists():
            raise ProbeError(f"找不到探针源码：{PROBE_SOURCE}")
        # The packaged application ships a prebuilt arm64 dylib.  Never try to
        # compile inside the application bundle at runtime.
        if getattr(sys, "frozen", False) and PROBE_BINARY.exists():
            return
        needs_build = (
            not PROBE_BINARY.exists()
            or PROBE_BINARY.stat().st_mtime_ns < PROBE_SOURCE.stat().st_mtime_ns
        )
        if not needs_build:
            return
        command = [
            "clang",
            "-arch",
            "arm64",
            "-O2",
            "-Wall",
            "-Wextra",
            "-dynamiclib",
            "-o",
            str(PROBE_BINARY),
            str(PROBE_SOURCE),
            "-Wno-unused-parameter",
        ]
        result = subprocess.run(command, text=True, capture_output=True, timeout=30)
        if result.returncode != 0:
            raise ProbeError(f"探针编译失败：\n{result.stdout}{result.stderr}")

    def find_wind_pid(self) -> int:
        pids = self._wind_pids()
        if not pids:
            raise ProbeError("未找到 WindPersonFree，请先启动并登录 Wind。")
        return pids[0]

    def _copy_unique_probe(self, symbol: str, pid: int) -> Path:
        try:
            self.probe_dir.mkdir(parents=True, exist_ok=True)
            name = (
                f"libwind_tbapi_runtime_{safe_code(symbol)}_{pid}_"
                f"{int(time.time())}_{uuid.uuid4().hex[:8]}.dylib"
            )
            destination = self.probe_dir / name
            shutil.copy2(PROBE_BINARY, destination)
            return destination
        except PermissionError as exc:
            raise ProbeError(
                "无法写入 Wind 数据目录。请在 macOS“系统设置 → 隐私与安全性 → "
                "完全磁盘访问权限”中允许当前启动程序（图形版为 ETF监控主机）。"
            ) from exc

    @staticmethod
    def _run_lldb(
        pid: int, commands: list[str], timeout_seconds: int = 45
    ) -> str:
        if not LLDB.exists():
            raise ProbeError(f"找不到 lldb：{LLDB}")
        argv = [str(LLDB), "--batch", "-p", str(pid)]
        for command in commands:
            argv.extend(["-o", command])
        try:
            result = subprocess.run(
                argv, text=True, capture_output=True, timeout=timeout_seconds
            )
        except subprocess.TimeoutExpired as exc:
            try:
                os.kill(pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
            raise ProbeError("lldb 注入超时，已尝试恢复 Wind 运行。") from exc

        output = result.stdout + result.stderr
        # A failed LLDB command can leave the target stopped. LLDB has exited at
        # this point, so SIGCONT is a safe recovery action.
        if result.returncode != 0:
            try:
                os.kill(pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
            raise ProbeError(f"lldb 执行失败：\n{output.strip()}")
        if f"Process {pid} detached" not in output:
            try:
                os.kill(pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
            raise ProbeError(f"lldb 未确认 detach：\n{output.strip()}")
        return output

    @classmethod
    def _checked_call_commands(
        cls,
        *,
        index: int,
        dylib_path: Path,
        function_name: str,
        call_expression: str,
    ) -> list[str]:
        """Build an LLDB call that never invokes a null function pointer."""

        handle = f"$tb_handle_{index}"
        error = f"$tb_error_{index}"
        function = f"$tb_function_{index}"
        result = f"$tb_result_{index}"
        return [
            f'expr -- void *{handle} = (void *)dlopen("{dylib_path}", 0x6)',
            (
                f"expr -- const char *{error} = {handle} ? "
                "(const char *)0 : (const char *)dlerror()"
            ),
            (
                f'expr -- void *{function} = {handle} ? (void *)dlsym('
                f'{handle}, "{function_name}") : (void *)0'
            ),
            (
                f"expr -- long long {result} = {function} ? "
                f"(long long)({call_expression}) : "
                f"({handle} ? {cls.LLDB_DLSYM_FAILED} : {cls.LLDB_DLOPEN_FAILED})"
            ),
            # `$tb_*` names are LLDB persistent expression variables, not
            # source variables in the selected stack frame.  `frame variable`
            # rejects them as undeclared; query them through the expression
            # evaluator so their original names remain available to parsers.
            f"expr -- {error}",
            f"expr -- {result}",
        ]

    @classmethod
    def _checked_call_result(cls, output: str, index: int) -> int:
        result_match = re.search(
            rf"\$tb_result_{index}\s*=\s*(-?\d+)", output
        )
        if result_match is None:
            raise ProbeError(f"LLDB 未返回探针调用结果：\n{output.strip()}")
        result = int(result_match.group(1))
        if result == cls.LLDB_DLOPEN_FAILED:
            error_match = re.search(
                rf'\$tb_error_{index}\s*=.*?"([^"]+)"', output
            )
            detail = error_match.group(1) if error_match else "未知加载错误"
            raise ProbeError(f"探针 dylib 加载失败：{detail}")
        if result == cls.LLDB_DLSYM_FAILED:
            raise ProbeError("探针已加载，但找不到订阅函数。")
        return result

    @staticmethod
    def _parse_integer_variable(output: str, var_name: str) -> int | None:
        match = re.search(rf"{re.escape(var_name)}\s*=\s*(-?\d+)", output)
        return int(match.group(1)) if match else None

    @classmethod
    def _append_subscription_id_commands(
        cls, commands: list[str], index: int
    ) -> None:
        """Read the real native id after Create(empty) -> Modify(target).

        CJAVAModifySubscription returns an operation result, not necessarily
        the subscription id.  The probe already exposes its retained id via
        wind_tbapi_subscription_id(), so query that in the same LLDB attach.
        """
        # The last two commands emitted by `_checked_call_commands` print the
        # loader error and call result.  Define the id variables before those
        # reads, then print the retained native id separately.
        output_start = len(commands) - 2
        commands.insert(
            output_start,
            f'expr -- void *$tb_id_function_{index} = $tb_handle_{index} ? '
            f'(void *)dlsym($tb_handle_{index}, '
            '"wind_tbapi_subscription_id") : (void *)0',
        )
        commands.insert(
            output_start + 1,
            f"expr -- long long $tb_sub_id_{index} = "
            f"$tb_id_function_{index} ? "
            f"((long long (*)(void))$tb_id_function_{index})() : "
            f"{cls.LLDB_DLSYM_FAILED}",
        )
        commands.append(f"expr -- $tb_sub_id_{index}")

    @staticmethod
    def _wait_for_status(symbol: str, newer_than_ns: int) -> dict[str, Any]:
        path = live_status_path(symbol)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                if path.stat().st_mtime_ns > newer_than_ns:
                    with path.open("r", encoding="utf-8") as handle:
                        return json.load(handle)
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            time.sleep(0.1)
        raise ProbeError(f"订阅命令已执行，但未收到状态文件：{path}")

    def _best_effort_stop_paths(self, pid: int, paths: list[Path]) -> bool:
        """Terminate probe subscriptions that Python could not register.

        A protocol/status parsing failure may happen after TBAPI has already
        created a live subscription.  Reloading the same path resolves to the
        existing in-process image, allowing its stop entry point to terminate
        that otherwise orphaned subscription.
        """
        if not paths:
            return True
        commands: list[str] = []
        for index, path in enumerate(paths):
            commands.extend(
                self._checked_call_commands(
                    index=index,
                    dylib_path=path,
                    function_name="wind_tbapi_stop",
                    call_expression=(
                        f"((long long (*)(void))$tb_function_{index})()"
                    ),
                )
            )
        commands.append("process detach")
        try:
            self._run_lldb(
                pid,
                commands,
                timeout_seconds=max(45, 20 + len(paths) * 8),
            )
            return True
        except Exception:
            # Preserve the original subscription failure.  Cleanup is a safety
            # net and must not replace the actionable root-cause message.
            return False

    def _cleanup_stale_probe_subscriptions(self, pid: int) -> None:
        """Terminate subscriptions left by a prior failed host operation."""
        try:
            paths = sorted(
                self.probe_dir.glob(f"libwind_tbapi_runtime_*_{pid}_*.dylib")
            )
        except OSError:
            return
        # Bound each LLDB attach and only remove paths after their stop calls
        # completed.  Keeping failed paths allows a later retry to clean them.
        for start in range(0, len(paths), 8):
            batch = paths[start : start + 8]
            if not self._best_effort_stop_paths(pid, batch):
                continue
            for path in batch:
                try:
                    path.unlink()
                except OSError:
                    pass

    def subscribe(self, symbol: str, latency_ms: int) -> SubscriptionSession:
        if not WIND_SYMBOL_PATTERN.fullmatch(symbol):
            raise ProbeError("内部 Wind 代码格式错误，预期格式为 000000.SZ。")
        self.build_probe()
        pid = self.find_wind_pid()
        self._cleanup_stale_probe_subscriptions(pid)
        dylib_path = self._copy_unique_probe(symbol, pid)
        status_path = live_status_path(symbol)
        previous_mtime = status_path.stat().st_mtime_ns if status_path.exists() else 0
        dylib_text = str(dylib_path)

        commands = self._checked_call_commands(
            index=0,
            dylib_path=Path(dylib_text),
            function_name="wind_tbapi_subscribe",
            call_expression=(
                "((long long (*)(const char *, int))$tb_function_0)"
                f'(\"{symbol}\", {latency_ms})'
            ),
        )
        self._append_subscription_id_commands(commands, 0)
        commands.append("process detach")
        try:
            output = self._run_lldb(pid, commands)
            self._checked_call_result(output, 0)
            status = self._wait_for_status(symbol, previous_mtime)
            operation_code = int(status.get("code", -1))
            sub_id = self._parse_integer_variable(output, "$tb_sub_id_0")
            if (
                status.get("status") not in self.SUBSCRIBED_STATUSES
                or operation_code < 0
                or sub_id is None
                or sub_id < 0
            ):
                raise ProbeError(
                    f"TBAPI2 订阅失败：status={status.get('status')} "
                    f"code={operation_code} sub_id={sub_id} "
                    f"message={status.get('message', '')}"
                )
        except Exception:
            self._best_effort_stop_paths(pid, [dylib_path])
            raise
        return SubscriptionSession(symbol, pid, dylib_path, sub_id)

    def stop(self, session: SubscriptionSession) -> None:
        current_pid = self.find_wind_pid()
        if current_pid != session.pid:
            return
        path_text = str(session.dylib_path)
        commands = self._checked_call_commands(
            index=0,
            dylib_path=Path(path_text),
            function_name="wind_tbapi_stop",
            call_expression="((long long (*)(void))$tb_function_0)()",
        )
        commands.append("process detach")
        output = self._run_lldb(session.pid, commands)
        self._checked_call_result(output, 0)

    def subscribe_many(
        self, symbols: list[str], latency_ms: int
    ) -> tuple[dict[str, SubscriptionSession], dict[str, str]]:
        unique_symbols = list(dict.fromkeys(symbols))
        if not unique_symbols:
            raise ProbeError("观察列表为空，请先添加标的。")
        for symbol in unique_symbols:
            if not WIND_SYMBOL_PATTERN.fullmatch(symbol):
                raise ProbeError(f"内部 Wind 代码格式错误：{symbol}")

        self.build_probe()
        pid = self.find_wind_pid()
        self._cleanup_stale_probe_subscriptions(pid)
        plans: list[tuple[str, Path, int]] = []
        commands: list[str] = []
        for index, symbol in enumerate(unique_symbols):
            dylib_path = self._copy_unique_probe(symbol, pid)
            status_path = live_status_path(symbol)
            previous_mtime = (
                status_path.stat().st_mtime_ns if status_path.exists() else 0
            )
            plans.append((symbol, dylib_path, previous_mtime))
            commands.extend(
                self._checked_call_commands(
                    index=index,
                    dylib_path=dylib_path,
                    function_name="wind_tbapi_subscribe",
                    call_expression=(
                        f"((long long (*)(const char *, int))$tb_function_{index})"
                        f'(\"{symbol}\", {latency_ms})'
                    ),
                )
            )
            self._append_subscription_id_commands(commands, index)
        commands.append("process detach")
        try:
            output = self._run_lldb(
                pid,
                commands,
                timeout_seconds=max(45, 20 + len(unique_symbols) * 10),
            )
        except Exception:
            self._best_effort_stop_paths(
                pid, [dylib_path for _symbol, dylib_path, _mtime in plans]
            )
            raise

        sessions: dict[str, SubscriptionSession] = {}
        errors: dict[str, str] = {}
        failed_paths: list[Path] = []
        for index, (symbol, dylib_path, previous_mtime) in enumerate(plans):
            try:
                self._checked_call_result(output, index)
                status = self._wait_for_status(symbol, previous_mtime)
                operation_code = int(status.get("code", -1))
                sub_id = self._parse_integer_variable(
                    output, f"$tb_sub_id_{index}"
                )
                if (
                    status.get("status") not in self.SUBSCRIBED_STATUSES
                    or operation_code < 0
                    or sub_id is None
                    or sub_id < 0
                ):
                    raise ProbeError(
                        f"status={status.get('status')} code={operation_code} "
                        f"sub_id={sub_id}"
                    )
                sessions[symbol] = SubscriptionSession(
                    symbol, pid, dylib_path, sub_id
                )
            except Exception as exc:
                errors[symbol] = str(exc)
                failed_paths.append(dylib_path)
        self._best_effort_stop_paths(pid, failed_paths)
        return sessions, errors

    def stop_many(
        self, sessions: dict[str, SubscriptionSession]
    ) -> dict[str, str]:
        if not sessions:
            return {}
        try:
            current_pid = self.find_wind_pid()
        except ProbeError:
            return {}
        active = [session for session in sessions.values() if session.pid == current_pid]
        if not active:
            return {}

        commands: list[str] = []
        for index, session in enumerate(active):
            commands.extend(
                self._checked_call_commands(
                    index=index,
                    dylib_path=session.dylib_path,
                    function_name="wind_tbapi_stop",
                    call_expression=(
                        f"((long long (*)(void))$tb_function_{index})()"
                    ),
                )
            )
        commands.append("process detach")
        try:
            output = self._run_lldb(
                current_pid,
                commands,
                timeout_seconds=max(45, 20 + len(active) * 8),
            )
            errors: dict[str, str] = {}
            for index, session in enumerate(active):
                try:
                    self._checked_call_result(output, index)
                except Exception as exc:
                    errors[session.symbol] = str(exc)
            return errors
        except Exception as exc:
            return {session.symbol: str(exc) for session in active}


class OperationThread(QThread):
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, operation: Callable[[], object]) -> None:
        super().__init__()
        self.operation = operation

    def run(self) -> None:
        try:
            self.succeeded.emit(self.operation())
        except Exception as exc:  # UI boundary: convert every error to a message.
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(
        self,
        capture_dir: Path = WIND_TEMP_DIR,
        log_path: Path = APP_LOG_PATH,
    ) -> None:
        super().__init__()
        self.capture_dir = capture_dir
        self.log_path = log_path
        self.log_lines: list[str] = []
        self.log_dialog: QDialog | None = None
        self.log_view: QTextEdit | None = None
        self.controller = ProbeController()
        self.session: SubscriptionSession | None = None
        self.worker: OperationThread | None = None
        self.last_source_path: Path | None = None
        self.settings = QSettings("ETFDelivery", "WindETFRealtime")

        self.setWindowTitle("ETF 实时数据")
        self.resize(780, 560)
        self._build_ui()
        self._apply_style()

        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_data)
        self._restore_settings()
        self._update_timer()
        QTimer.singleShot(0, self.refresh_data)

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("appRoot")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(16)

        controls = QFrame()
        controls.setObjectName("card")
        grid = QGridLayout(controls)
        grid.setContentsMargins(18, 16, 18, 16)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)

        grid.addWidget(QLabel("证券代码"), 0, 0)
        self.symbol_input = QLineEdit("159518")
        self.symbol_input.setPlaceholderText("例如 159518")
        validator = QRegularExpressionValidator(
            QRegularExpression(r"[0-9]{0,6}")
        )
        self.symbol_input.setValidator(validator)
        grid.addWidget(self.symbol_input, 0, 1)

        grid.addWidget(QLabel("刷新频率"), 0, 2)
        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(0.5, 60.0)
        self.interval_spin.setSingleStep(0.5)
        self.interval_spin.setDecimals(1)
        self.interval_spin.setSuffix(" 秒")
        self.interval_spin.setValue(1.0)
        self.interval_spin.valueChanged.connect(self._update_timer)
        grid.addWidget(self.interval_spin, 0, 3)

        buttons = QHBoxLayout()
        self.start_button = QPushButton("开始订阅")
        self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self.start_subscription)
        self.stop_button = QPushButton("停止订阅")
        self.stop_button.clicked.connect(self.stop_subscription)
        self.stop_button.setEnabled(False)
        self.refresh_button = QPushButton("立即刷新")
        self.refresh_button.clicked.connect(self.refresh_data)
        self.log_button = QPushButton("查看日志")
        self.log_button.clicked.connect(self.show_log_dialog)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.stop_button)
        buttons.addWidget(self.refresh_button)
        buttons.addWidget(self.log_button)
        buttons.addStretch()
        grid.addLayout(buttons, 1, 0, 1, 4)
        layout.addWidget(controls)

        status_card = QFrame()
        status_card.setObjectName("card")
        status_layout = QHBoxLayout(status_card)
        status_layout.setContentsMargins(18, 12, 18, 12)
        self.connection_label = QLabel("● 未建立新订阅")
        self.connection_label.setObjectName("statusNeutral")
        self.freshness_label = QLabel("等待数据")
        self.source_label = QLabel("—")
        self.source_label.setAlignment(
            self.source_label.alignment() | self.source_label.alignment().AlignRight
        )
        status_layout.addWidget(self.connection_label)
        status_layout.addSpacing(12)
        status_layout.addWidget(self.freshness_label)
        status_layout.addStretch()
        status_layout.addWidget(self.source_label)
        layout.addWidget(status_card)

        self.table = QTableWidget(3, 2)
        self.table.setObjectName("dataTable")
        self.table.setHorizontalHeaderLabels(["申购", "赎回"])
        self.table.setVerticalHeaderLabels(["笔数", "金额", "份额"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setMinimumHeight(250)
        for row in range(3):
            for column in range(2):
                item = QTableWidgetItem("—")
                item.setTextAlignment(0x0084)  # AlignHCenter | AlignVCenter
                self.table.setItem(row, column, item)
        layout.addWidget(self.table)

        self.detail_label = QLabel("尚未读取捕获文件")
        self.detail_label.setObjectName("detail")
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.detail_label)

        self.setCentralWidget(root)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget#appRoot { background: #f5f7fa; color: #1f2937; }
            QLabel { background: transparent; color: #253044; }
            QFrame#card { background: #ffffff; border: 1px solid #dce3ec;
                          border-radius: 10px; }
            QLineEdit, QDoubleSpinBox { color: #172033; background: #ffffff;
                          border: 1px solid #cbd5e1; border-radius: 6px;
                          padding: 8px; min-height: 20px; selection-background-color: #bcd2ff; }
            QLineEdit:focus, QDoubleSpinBox:focus { border: 1px solid #3b72e8; }
            QPushButton { color: #283548; background: #f8fafc;
                          border: 1px solid #cbd5e1; border-radius: 6px;
                          padding: 8px 15px; }
            QPushButton:hover { background: #eef3f9; border-color: #aab7c7; }
            QPushButton:disabled { color: #9aa5b4; background: #f1f4f8;
                                   border-color: #e1e6ed; }
            QPushButton#primaryButton { color: #ffffff; background: #2f6feb;
                                        border-color: #2f6feb; }
            QPushButton#primaryButton:hover { background: #255fcf; }
            QTableWidget#dataTable { color: #172033; background: #ffffff;
                          alternate-background-color: #f8fafc;
                          border: 1px solid #dce3ec; border-radius: 10px;
                          gridline-color: #dce3ec; font-size: 20px; font-weight: 600; }
            QTableWidget#dataTable::item { background: #ffffff; color: #172033; }
            QHeaderView::section { background: #eef2f7; color: #526074;
                          border: none; border-right: 1px solid #dce3ec;
                          border-bottom: 1px solid #dce3ec; padding: 8px;
                          font-size: 14px; font-weight: 600; }
            QTableCornerButton::section { background: #eef2f7;
                                          border: 1px solid #dce3ec; }
            QTextEdit { background: #ffffff; border: 1px solid #dce3ec;
                        border-radius: 8px; padding: 8px; color: #526074;
                        font-family: Menlo, monospace; font-size: 11px; }
            QLabel#detail { color: #68758a; }
            """
        )

    def _restore_settings(self) -> None:
        saved = str(self.settings.value("symbol", "159518"))
        self.symbol_input.setText(display_symbol(saved))
        self.interval_spin.setValue(float(self.settings.value("interval", 1.0)))

    def _update_timer(self) -> None:
        interval_ms = int(self.interval_spin.value() * 1000)
        self.refresh_timer.start(interval_ms)

    def append_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"
        self.log_lines.append(line)
        if len(self.log_lines) > 500:
            self.log_lines = self.log_lines[-500:]
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass
        if self.log_view is not None:
            self.log_view.append(line)

    def show_log_dialog(self) -> None:
        if self.log_dialog is not None:
            self.log_dialog.show()
            self.log_dialog.raise_()
            self.log_dialog.activateWindow()
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("运行日志")
        dialog.resize(760, 360)
        layout = QVBoxLayout(dialog)
        view = QTextEdit()
        view.setReadOnly(True)
        view.setPlainText("\n".join(self.log_lines))
        close_button = QPushButton("关闭")
        close_button.clicked.connect(dialog.close)
        layout.addWidget(view)
        layout.addWidget(close_button)
        dialog.finished.connect(self._log_dialog_closed)
        self.log_dialog = dialog
        self.log_view = view
        dialog.show()

    def _log_dialog_closed(self, _result: int) -> None:
        self.log_dialog = None
        self.log_view = None

    def current_symbol(self) -> str:
        try:
            return normalize_symbol(self.symbol_input.text())
        except ValueError:
            return ""

    def start_subscription(self) -> None:
        symbol = self.current_symbol()
        if not WIND_SYMBOL_PATTERN.fullmatch(symbol):
            QMessageBox.warning(self, "代码格式错误", "请输入 6 位深圳代码，例如 159518。")
            return
        if self.worker and self.worker.isRunning():
            return
        if self.session and self.session.symbol != symbol:
            QMessageBox.information(
                self,
                "请先停止",
                f"当前正在管理 {self.session.symbol}，请先停止后再切换标的。",
            )
            return

        latency_ms = int(self.interval_spin.value() * 1000)
        self.settings.setValue("symbol", display_symbol(symbol))
        self.settings.setValue("interval", self.interval_spin.value())
        self.start_button.setEnabled(False)
        self.symbol_input.setEnabled(False)
        self.connection_label.setText("● 正在连接 Wind…")
        self.append_log(
            f"为 {display_symbol(symbol)} 创建 TBAPI2 订阅，LATENCY={latency_ms}ms"
        )
        self.worker = OperationThread(
            lambda: self.controller.subscribe(symbol, latency_ms)
        )
        self.worker.succeeded.connect(self._subscription_started)
        self.worker.failed.connect(self._operation_failed)
        self.worker.start()

    def _subscription_started(self, session: object) -> None:
        assert isinstance(session, SubscriptionSession)
        self.session = session
        self.connection_label.setText(
            f"● 已订阅 {display_symbol(session.symbol)} · ID {session.sub_id}"
        )
        self.connection_label.setStyleSheet("color: #55d68b;")
        self.stop_button.setEnabled(True)
        self.append_log(f"订阅成功：sub_id={session.sub_id}, Wind PID={session.pid}")
        self.refresh_data()

    def stop_subscription(self) -> None:
        if not self.session or (self.worker and self.worker.isRunning()):
            return
        session = self.session
        self.stop_button.setEnabled(False)
        self.connection_label.setText("● 正在停止订阅…")
        self.worker = OperationThread(lambda: self.controller.stop(session))
        self.worker.succeeded.connect(self._subscription_stopped)
        self.worker.failed.connect(self._operation_failed)
        self.worker.start()

    def _subscription_stopped(self, _result: object) -> None:
        if self.session:
            self.append_log(f"已停止 {display_symbol(self.session.symbol)} 的订阅")
        self.session = None
        self.connection_label.setText("● 未建立新订阅")
        self.connection_label.setStyleSheet("")
        self.start_button.setEnabled(True)
        self.symbol_input.setEnabled(True)
        self.stop_button.setEnabled(False)

    def _operation_failed(self, message: str) -> None:
        self.append_log(message)
        self.connection_label.setText("● 操作失败")
        self.connection_label.setStyleSheet("color: #ff6b73;")
        self.start_button.setEnabled(True)
        self.symbol_input.setEnabled(True)
        self.stop_button.setEnabled(bool(self.session))
        QMessageBox.critical(self, "Wind 订阅操作失败", message)

    def _candidate_captures(self, symbol: str) -> list[Path]:
        live = self.capture_dir / f"wind_tbapi_live_{safe_code(symbol)}.json"
        candidates = [live] if live.exists() else []
        try:
            legacy = sorted(
                self.capture_dir.glob("wind_tbapi_probe_sub_*.json"),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
            candidates.extend(legacy)
        except PermissionError:
            pass
        return candidates

    def _read_symbol_data(self, symbol: str) -> tuple[dict[str, Any], Path, float]:
        permission_error: PermissionError | None = None
        for path in self._candidate_captures(symbol):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    capture = json.load(handle)
                decoded = decode_probe_capture(capture)
                for row in decoded["rows"]:
                    if str(row.get("windcode", "")).upper() == symbol:
                        timestamp_ms = capture.get("callback_epoch_ms")
                        timestamp = (
                            float(timestamp_ms) / 1000.0
                            if timestamp_ms is not None
                            else path.stat().st_mtime
                        )
                        return row, path, timestamp
            except PermissionError as exc:
                permission_error = exc
            except (OSError, json.JSONDecodeError, FrameFormatError):
                continue
        if permission_error:
            raise ProbeError(
                "无法读取 Wind 沙盒。若从 PyCharm 启动，请开启 PyCharm 的完全磁盘访问权限。"
            ) from permission_error
        raise ProbeError(f"尚未找到 {symbol} 的回调数据。请点击“开始订阅”。")

    @staticmethod
    def _format_number(value: Any, show_wan: bool = False) -> str:
        if not isinstance(value, (int, float)):
            return str(value)
        base = f"{value:,.0f}"
        if show_wan and value and value % 10000 == 0:
            return f"{base}  ({value / 10000:g}万)"
        return base

    def refresh_data(self) -> None:
        symbol = self.current_symbol()
        if not WIND_SYMBOL_PATTERN.fullmatch(symbol):
            return
        try:
            row, path, timestamp = self._read_symbol_data(symbol)
        except ProbeError as exc:
            self.freshness_label.setText("暂无数据")
            self.detail_label.setText(str(exc))
            return

        values = [
            (row.get("etfbuynumber"), row.get("etfsellnumber"), False),
            (row.get("etfbuymoney"), row.get("etfsellmoney"), False),
            (row.get("etfbuyamount"), row.get("etfsellamount"), True),
        ]
        for row_index, (buy, sell, show_wan) in enumerate(values):
            self.table.item(row_index, 0).setText(
                self._format_number(buy, show_wan)
            )
            self.table.item(row_index, 1).setText(
                self._format_number(sell, show_wan)
            )

        age = max(0.0, time.time() - timestamp)
        stale_after = max(self.interval_spin.value() * 3, 10.0)
        capture_time = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
        if age <= stale_after:
            self.freshness_label.setText(f"● 最新 · {age:.1f} 秒前")
            self.freshness_label.setStyleSheet("color: #55d68b;")
        else:
            self.freshness_label.setText(f"● 缓存/过期 · {age:.0f} 秒前")
            self.freshness_label.setStyleSheet("color: #f0b35a;")
        self.source_label.setText(display_symbol(symbol))
        self.detail_label.setText(
            f"数据时间：{capture_time}　份额按原始整数值展示"
        )
        if path != self.last_source_path:
            self.append_log(f"读取数据：{path}")
            self.last_source_path = path

    def closeEvent(self, event: Any) -> None:
        self.settings.setValue("symbol", self.symbol_input.text().strip())
        self.settings.setValue("interval", self.interval_spin.value())
        if self.session:
            result = QMessageBox.question(
                self,
                "订阅仍在运行",
                "关闭界面不会自动停止 Wind 内的订阅。是否仍要退出？",
            )
            if result != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()


def main() -> int:
    # Keep the original entry point working while the launcher and new users
    # move to the multi-symbol monitor.
    from wind_etf_multi_monitor_ui import main as multi_monitor_main

    return multi_monitor_main()


if __name__ == "__main__":
    raise SystemExit(main())
