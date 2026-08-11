#!/usr/bin/env python3

import json
import unittest
from pathlib import Path

from wind_tbapi_frame_parser import decode_probe_capture


HERE = Path(__file__).resolve().parent


class WindTbapiFrameParserTest(unittest.TestCase):
    def setUp(self) -> None:
        with (HERE / "sample_wind_tbapi_probe_sub_1.json").open(
            "r", encoding="utf-8"
        ) as handle:
            self.result = decode_probe_capture(json.load(handle))

    def test_decodes_page_values(self) -> None:
        self.assertEqual(
            self.result["rows"],
            [
                {
                    "etfbuynumber": 3,
                    "etfbuyamount": 3_000_000,
                    "etfbuymoney": 0,
                    "etfsellnumber": 2,
                    "etfsellamount": 2_000_000,
                    "etfsellmoney": 0,
                    "windcode": "159518.SZ",
                }
            ],
        )

    def test_layout_metadata(self) -> None:
        self.assertEqual(self.result["row_count"], 1)
        self.assertEqual(self.result["row_size"], 111)
        self.assertEqual(self.result["known_value_span"], 104)
        self.assertEqual(self.result["row_tail_hex"], ["00000000000000"])
        self.assertEqual(
            [(field["name"], field["offset"]) for field in self.result["fields"]],
            [
                ("etfbuynumber", 0),
                ("etfbuyamount", 4),
                ("etfbuymoney", 12),
                ("etfsellnumber", 20),
                ("etfsellamount", 24),
                ("etfsellmoney", 32),
                ("Windcode", 40),
            ],
        )


if __name__ == "__main__":
    unittest.main()
