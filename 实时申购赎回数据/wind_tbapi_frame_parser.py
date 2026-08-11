#!/usr/bin/env python3
"""Decode JSON captures written by the Wind TBAPI2 v3 probe.

The decoder is intentionally standalone and uses only the Python standard
library.  The frame layout was verified against the post-close snapshot for
159518.SZ captured on 2026-08-11.
"""

from __future__ import annotations

import argparse
import glob
import json
import struct
import sys
from pathlib import Path
from typing import Any


DEFAULT_CAPTURE_GLOB = (
    "/Users/ellis/Library/Containers/com.windin.mac.free/Data/tmp/"
    "wind_tbapi_probe_sub_*.json"
)
SAMPLE_CAPTURE = Path(__file__).resolve().with_name(
    "sample_wind_tbapi_probe_sub_1.json"
)

# Type codes observed in JavaTableFrame field_info on 2026-08-11.
TYPE_INT32 = 0x25
TYPE_INT64 = 0x27
TYPE_FIXED_STRING = 0x52


class FrameFormatError(ValueError):
    """Raised when a capture is truncated or has an unexpected layout."""


def _require(raw: bytes, position: int, size: int, label: str) -> None:
    if position < 0 or size < 0 or position + size > len(raw):
        raise FrameFormatError(
            f"{label} is truncated: need bytes {position}:{position + size}, "
            f"buffer length is {len(raw)}"
        )


def _take_int(
    raw: bytes, position: int, size: int, byteorder: str, label: str
) -> tuple[int, int]:
    _require(raw, position, size, label)
    value = int.from_bytes(raw[position : position + size], byteorder)
    return value, position + size


def parse_field_info(hex_text: str) -> list[dict[str, Any]]:
    """Parse the variable-length JavaTableFrame field descriptor buffer."""

    raw = bytes.fromhex(hex_text)
    field_count, position = _take_int(raw, 0, 2, "big", "field_count")
    fields: list[dict[str, Any]] = []

    for index in range(field_count):
        name_size, position = _take_int(
            raw, position, 2, "big", f"field[{index}].name_size"
        )
        _require(raw, position, name_size + 1, f"field[{index}].name")
        name_bytes = raw[position : position + name_size]
        position += name_size
        if raw[position] != 0:
            raise FrameFormatError(f"field[{index}] name is not NUL terminated")
        position += 1

        type_code, position = _take_int(
            raw, position, 1, "big", f"field[{index}].type_code"
        )
        format_code, position = _take_int(
            raw, position, 2, "big", f"field[{index}].format_code"
        )
        width, position = _take_int(
            raw, position, 8, "big", f"field[{index}].width"
        )
        reserved, position = _take_int(
            raw, position, 8, "big", f"field[{index}].reserved"
        )
        flags, position = _take_int(
            raw, position, 4, "little", f"field[{index}].flags"
        )
        offset, position = _take_int(
            raw, position, 2, "big", f"field[{index}].offset"
        )
        offset_reserved, position = _take_int(
            raw, position, 2, "big", f"field[{index}].offset_reserved"
        )
        field_id, position = _take_int(
            raw, position, 2, "big", f"field[{index}].field_id"
        )

        try:
            name = name_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FrameFormatError(f"field[{index}] name is not UTF-8") from exc

        fields.append(
            {
                "name": name,
                "type_code": type_code,
                "format_code": format_code,
                "width": width,
                "reserved": reserved,
                "flags": flags,
                "offset": offset,
                "offset_reserved": offset_reserved,
                "field_id": field_id,
            }
        )

    if position != len(raw):
        raise FrameFormatError(
            f"field_info has {len(raw) - position} unparsed trailing bytes"
        )
    return fields


def _decode_value(row: bytes, field: dict[str, Any]) -> Any:
    name = field["name"]
    offset = field["offset"]
    width = field["width"]
    type_code = field["type_code"]
    _require(row, offset, width, name)
    value = row[offset : offset + width]

    if type_code == TYPE_INT32:
        if width != 4:
            raise FrameFormatError(f"{name}: INT32 width is {width}, expected 4")
        return struct.unpack("<i", value)[0]
    if type_code == TYPE_INT64:
        if width != 8:
            raise FrameFormatError(f"{name}: INT64 width is {width}, expected 8")
        return struct.unpack("<q", value)[0]
    if type_code == TYPE_FIXED_STRING:
        return value.split(b"\x00", 1)[0].decode("utf-8", errors="replace")

    # Preserve unknown types losslessly for future reverse engineering.
    return {"type_code": type_code, "hex": value.hex()}


def parse_data_buffer(
    hex_text: str, fields: list[dict[str, Any]]
) -> dict[str, Any]:
    """Parse buffer_58 into rows using descriptors from field_info."""

    raw = bytes.fromhex(hex_text)
    _require(raw, 0, 12, "data header")
    row_count, row_size, header_word = struct.unpack(">III", raw[:12])
    required_size = 12 + row_count * row_size
    if required_size != len(raw):
        raise FrameFormatError(
            f"data buffer length is {len(raw)}, header requires {required_size}"
        )

    known_value_span = max(
        (field["offset"] + field["width"] for field in fields), default=0
    )
    if known_value_span > row_size:
        raise FrameFormatError(
            f"field values need {known_value_span} bytes, row_size is {row_size}"
        )

    rows: list[dict[str, Any]] = []
    row_tails: list[str] = []
    for index in range(row_count):
        start = 12 + index * row_size
        row = raw[start : start + row_size]
        values = {
            field["name"].lower(): _decode_value(row, field) for field in fields
        }
        rows.append(values)
        # Seven zero bytes were observed after the 104-byte value area in the
        # first capture.  They may be per-field null/status flags; retain them
        # without assigning semantics until a null-valued sample is captured.
        row_tails.append(row[known_value_span:].hex())

    return {
        "row_count": row_count,
        "row_size": row_size,
        "header_word": header_word,
        "known_value_span": known_value_span,
        "row_tail_hex": row_tails,
        "rows": rows,
    }


def decode_probe_capture(capture: dict[str, Any]) -> dict[str, Any]:
    if capture.get("error_code") != 0:
        raise FrameFormatError(
            f"subscription callback error {capture.get('error_code')}: "
            f"{capture.get('error_message', '')}"
        )
    if not capture.get("field_info") or not capture.get("buffer_58"):
        raise FrameFormatError("capture has no field_info or buffer_58")

    fields = parse_field_info(capture["field_info"]["hex"])
    data = parse_data_buffer(capture["buffer_58"]["hex"], fields)
    return {
        "status": capture.get("status"),
        "sub_id": capture.get("sub_id"),
        "callback_seq": capture.get("callback_seq"),
        "fields": fields,
        **data,
    }


def latest_capture() -> Path:
    candidates = [Path(path) for path in glob.glob(DEFAULT_CAPTURE_GLOB)]
    if not candidates:
        capture_dir = Path(DEFAULT_CAPTURE_GLOB).parent
        if not capture_dir.exists():
            detail = f"Wind capture directory does not exist: {capture_dir}"
        elif not capture_dir.is_dir():
            detail = f"Wind capture path is not a directory: {capture_dir}"
        else:
            detail = (
                "Wind capture directory is present, but it contains no "
                "wind_tbapi_probe_sub_*.json callback file"
            )
        raise FileNotFoundError(
            f"{detail}. A live subscription callback must be captured first. "
            f"To test only the decoder, run this program with --sample."
        )
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "capture",
        nargs="?",
        type=Path,
        help="probe JSON capture; defaults to the newest Wind sandbox capture",
    )
    parser.add_argument(
        "--metadata",
        action="store_true",
        help="include full field metadata instead of printing only decoded rows",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="decode the bundled fixed sample instead of a live Wind capture",
    )
    args = parser.parse_args()

    if args.sample and args.capture:
        parser.error("capture and --sample cannot be used together")

    try:
        path = SAMPLE_CAPTURE if args.sample else (args.capture or latest_capture())
        with path.open("r", encoding="utf-8") as handle:
            result = decode_probe_capture(json.load(handle))
    except (FileNotFoundError, PermissionError, json.JSONDecodeError, FrameFormatError) as exc:
        print(f"wind_tbapi_frame_parser: {exc}", file=sys.stderr)
        return 2
    output: Any = result if args.metadata else result["rows"]
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
