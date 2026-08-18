#!/usr/bin/env python3

import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wind_etf_realtime_ui import ProbeController, ProbeError


class ProbeControllerTest(unittest.TestCase):
    def test_cleanup_generated_dylibs_is_bounded_and_requires_stopped_wind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / (
                "libwind_tbapi_runtime_159518_SZ_123_1723456789_abcdef12.dylib"
            )
            generated.write_bytes(b"probe")
            keep_base = root / "libwind_tbapi_runtime_probe.dylib"
            keep_base.write_bytes(b"base")
            keep_other = root / "notes.txt"
            keep_other.write_text("keep", encoding="utf-8")
            link = root / (
                "libwind_tbapi_runtime_159866_SZ_123_1723456789_deadbeef.dylib"
            )
            link.symlink_to(keep_other)
            controller = ProbeController(probe_dir=root)

            with patch.object(controller, "_wind_pids", return_value=[]):
                result = controller.cleanup_generated_dylibs()
            self.assertEqual(result, {"deleted_count": 1, "deleted_bytes": 5})
            self.assertFalse(generated.exists())
            self.assertTrue(keep_base.exists())
            self.assertTrue(keep_other.exists())
            self.assertTrue(link.is_symlink())

            with patch.object(controller, "_wind_pids", return_value=[123]):
                with self.assertRaisesRegex(ProbeError, "Wind 仍在运行"):
                    controller.cleanup_generated_dylibs()

    def test_cleanup_helper_times_out_instead_of_blocking_host(self) -> None:
        controller = ProbeController(probe_dir=Path("/private/tmp/probe"))
        with (
            patch.object(controller, "_wind_pids", return_value=[]),
            patch(
                "wind_etf_realtime_ui.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["perl"], 1),
            ),
        ):
            with self.assertRaisesRegex(ProbeError, "清理临时 dylib 超时"):
                controller.cleanup_generated_dylibs(timeout_seconds=1)

    def test_cleanup_refuses_symlink_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            real_root = base / "real"
            real_root.mkdir()
            link_root = base / "link"
            link_root.symlink_to(real_root, target_is_directory=True)
            controller = ProbeController(probe_dir=link_root)
            with patch.object(controller, "_wind_pids", return_value=[]):
                with self.assertRaisesRegex(ProbeError, "符号链接"):
                    controller.cleanup_generated_dylibs()

    def test_terminate_wind_uses_sigterm_and_never_forces_kill(self) -> None:
        controller = ProbeController()
        with (
            patch.object(controller, "_wind_pids", side_effect=[[123], [], []]),
            patch.object(controller, "_is_expected_wind_pid", return_value=True),
            patch.object(os, "kill") as kill,
        ):
            result = controller.terminate_wind(timeout_seconds=1)
        kill.assert_called_once_with(123, signal.SIGTERM)
        self.assertFalse(result["running"])

    def test_same_name_process_with_wrong_executable_is_not_managed(self) -> None:
        controller = ProbeController()
        with (
            patch.object(controller, "_candidate_wind_pids", return_value=[123]),
            patch.object(controller, "_is_expected_wind_pid", return_value=False),
        ):
            self.assertEqual(controller._wind_pids(), [])

    def test_checked_commands_guard_null_dlsym_result(self) -> None:
        commands = ProbeController._checked_call_commands(
            index=2,
            dylib_path=Path("/tmp/probe.dylib"),
            function_name="wind_tbapi_subscribe",
            call_expression=(
                "((long long (*)(const char *, int))$tb_function_2)"
                '(\"159518.SZ\", 1000)'
            ),
        )
        joined = "\n".join(commands)
        self.assertIn("$tb_function_2 ?", joined)
        self.assertIn("$tb_handle_2 ? -9002 : -9001", joined)
        self.assertIn("expr -- $tb_error_2", joined)
        self.assertIn("expr -- $tb_result_2", joined)
        self.assertNotIn("frame variable", joined)

    def test_checked_result_reports_dlopen_error(self) -> None:
        output = (
            '(const char *) $tb_error_0 = 0x1 "code signature rejected"\n'
            "(long long) $tb_result_0 = -9001\n"
        )
        with self.assertRaisesRegex(ProbeError, "code signature rejected"):
            ProbeController._checked_call_result(output, 0)

    def test_checked_result_returns_subscription_id(self) -> None:
        output = (
            "(const char *) $tb_error_0 = 0x0000000000000000\n"
            "(long long) $tb_result_0 = 393220\n"
        )
        self.assertEqual(ProbeController._checked_call_result(output, 0), 393220)

    def test_native_modify_status_is_subscription_success(self) -> None:
        self.assertIn("modify_target", ProbeController.SUBSCRIBED_STATUSES)

    def test_parse_real_subscription_id(self) -> None:
        output = "(long long) $tb_sub_id_2 = 393221\n"
        self.assertEqual(
            ProbeController._parse_integer_variable(output, "$tb_sub_id_2"),
            393221,
        )

    def test_subscription_id_query_uses_probe_getter(self) -> None:
        commands = ProbeController._checked_call_commands(
            index=1,
            dylib_path=Path("/tmp/probe.dylib"),
            function_name="wind_tbapi_subscribe",
            call_expression=(
                "((long long (*)(const char *, int))$tb_function_1)"
                '(\"159518.SZ\", 1000)'
            ),
        )
        ProbeController._append_subscription_id_commands(commands, 1)
        joined = "\n".join(commands)
        self.assertIn("wind_tbapi_subscription_id", joined)
        self.assertEqual(commands[-1], "expr -- $tb_sub_id_1")


if __name__ == "__main__":
    unittest.main()
