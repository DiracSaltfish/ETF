#!/usr/bin/env python3

import unittest
from pathlib import Path

from wind_etf_realtime_ui import ProbeController, ProbeError


class ProbeControllerTest(unittest.TestCase):
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
        self.assertIn("frame variable $tb_error_2 $tb_result_2", joined)

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


if __name__ == "__main__":
    unittest.main()
