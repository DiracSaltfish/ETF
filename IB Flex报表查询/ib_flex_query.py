#!/usr/bin/env python3
"""Query an Interactive Brokers Activity Flex report.

The module uses only Python's standard library.  Credentials are read from
environment variables and are never printed or written into the report code.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Mapping


FLEX_BASE_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
FLEX_VERSION = "3"
DEFAULT_USER_AGENT = "IBKRFlexQuery/1.0 Python"
RETRYABLE_ERROR_CODES = {
    "1001",
    "1003",
    "1004",
    "1005",
    "1006",
    "1007",
    "1008",
    "1009",
    "1019",
}
LOCAL_ENV_FILE = Path(__file__).with_name("ib_flex.env")


class FlexError(RuntimeError):
    """A sanitized Flex Web Service error."""


@dataclass(frozen=True)
class ServiceResponse:
    status: str
    reference_code: str = ""
    error_code: str = ""
    error_message: str = ""


def parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"日期格式应为 YYYY-MM-DD：{value}") from exc


def _validate_date_range(from_date: date, to_date: date) -> None:
    if to_date < from_date:
        raise ValueError("结束日期不能早于开始日期")
    if (to_date - from_date).days + 1 > 365:
        raise ValueError("IBKR Flex 单次查询最多覆盖 365 天")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(root: ET.Element, name: str) -> str:
    for child in root:
        if _local_name(child.tag) == name:
            return str(child.text or "").strip()
    return ""


def _parse_service_response(payload: bytes) -> ServiceResponse | None:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return None
    status = _child_text(root, "Status")
    if not status:
        return None
    return ServiceResponse(
        status=status,
        reference_code=_child_text(root, "ReferenceCode"),
        error_code=_child_text(root, "ErrorCode"),
        error_message=_child_text(root, "ErrorMessage"),
    )


def _request_bytes(
    endpoint: str,
    params: Mapping[str, str],
    *,
    user_agent: str,
    timeout: float,
) -> bytes:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{FLEX_BASE_URL}/{endpoint}?{query}",
        headers={"User-Agent": user_agent, "Accept": "*/*"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise FlexError(f"IBKR Flex HTTP 请求失败：状态码 {exc.code}") from None
    except urllib.error.URLError as exc:
        reason = type(exc.reason).__name__ if exc.reason is not None else "network error"
        raise FlexError(f"IBKR Flex 网络请求失败：{reason}") from None
    except TimeoutError:
        raise FlexError("IBKR Flex 网络请求超时") from None


def _raise_service_error(response: ServiceResponse) -> None:
    code = response.error_code or "unknown"
    message = response.error_message or "未知错误"
    raise FlexError(f"IBKR Flex 返回错误 {code}：{message}")


def _load_local_env(path: Path = LOCAL_ENV_FILE) -> None:
    """Load the local credential file without overriding explicit env vars."""

    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise FlexError(f"无法读取本地凭据文件：{path}") from exc

    allowed = {"IBKR_FLEX_TOKEN", "IBKR_FLEX_QUERY_ID"}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or key not in allowed:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def query_flex_statement(
    from_date: date,
    to_date: date,
    *,
    token: str | None = None,
    query_id: str | None = None,
    timeout: float = 30.0,
    poll_seconds: float = 5.0,
    max_attempts: int = 12,
    user_agent: str = DEFAULT_USER_AGENT,
) -> bytes:
    """Download an Activity Flex report and return its XML bytes.

    ``token`` and ``query_id`` can be passed explicitly, but the normal usage
    is to read them from ``IBKR_FLEX_TOKEN`` and ``IBKR_FLEX_QUERY_ID``.
    """

    _validate_date_range(from_date, to_date)
    _load_local_env()
    token = str(token if token is not None else os.environ.get("IBKR_FLEX_TOKEN", "")).strip()
    query_id = str(
        query_id if query_id is not None else os.environ.get("IBKR_FLEX_QUERY_ID", "")
    ).strip()
    if not token:
        raise ValueError("未配置 IBKR_FLEX_TOKEN")
    if not query_id:
        raise ValueError("未配置 IBKR_FLEX_QUERY_ID")
    if max_attempts < 1:
        raise ValueError("max_attempts 必须至少为 1")
    if not user_agent.strip():
        raise ValueError("User-Agent 不能为空")

    send_payload = _request_bytes(
        "SendRequest",
        {
            "t": token,
            "q": query_id,
            "fd": from_date.strftime("%Y%m%d"),
            "td": to_date.strftime("%Y%m%d"),
            "v": FLEX_VERSION,
        },
        user_agent=user_agent,
        timeout=timeout,
    )
    send_response = _parse_service_response(send_payload)
    if send_response is None:
        raise FlexError("IBKR Flex SendRequest 返回了无法识别的响应")
    if send_response.status.lower() != "success":
        _raise_service_error(send_response)
    if not send_response.reference_code:
        raise FlexError("IBKR Flex SendRequest 成功但未返回 ReferenceCode")

    if poll_seconds > 0:
        time.sleep(poll_seconds)

    for attempt in range(max_attempts):
        payload = _request_bytes(
            "GetStatement",
            {"t": token, "q": send_response.reference_code, "v": FLEX_VERSION},
            user_agent=user_agent,
            timeout=timeout,
        )
        response = _parse_service_response(payload)
        if response is None:
            return payload
        if response.status.lower() == "success":
            return payload
        if response.error_code not in RETRYABLE_ERROR_CODES:
            _raise_service_error(response)
        if attempt + 1 >= max_attempts:
            raise FlexError(
                f"IBKR Flex 报表在 {max_attempts} 次查询后仍未生成完成"
                f"（最后错误 {response.error_code or 'unknown'}）"
            )
        if poll_seconds > 0:
            time.sleep(min(30.0, poll_seconds * (1.5 ** min(attempt + 1, 4))))

    raise AssertionError("unreachable")


def extract_trade_rows(payload: bytes) -> list[dict[str, str]]:
    """Extract generic Order/Trade attributes from an Activity Flex XML report."""

    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise FlexError(f"报表不是有效 XML：{exc}") from None

    service_response = _parse_service_response(payload)
    if service_response is not None:
        if service_response.status.lower() != "success":
            _raise_service_error(service_response)
        raise FlexError("报表尚未生成完整")

    orders = [element for element in root.iter() if _local_name(element.tag) == "Order"]
    elements = orders or [element for element in root.iter() if _local_name(element.tag) == "Trade"]
    rows: list[dict[str, str]] = []
    for element in elements:
        row = {str(key): str(value) for key, value in element.attrib.items()}
        row["recordType"] = _local_name(element.tag)
        rows.append(row)
    return rows


def _default_filename(from_date: date, to_date: date) -> str:
    return f"ib_activity_{from_date:%Y%m%d}_{to_date:%Y%m%d}.xml"


def main(argv: list[str] | None = None) -> int:
    _load_local_env()
    parser = argparse.ArgumentParser(description="查询 IBKR Activity Flex 报表")
    parser.add_argument("--from", dest="from_date", required=True, type=parse_iso_date)
    parser.add_argument("--to", dest="to_date", required=True, type=parse_iso_date)
    parser.add_argument(
        "--query-id",
        default=os.environ.get("IBKR_FLEX_QUERY_ID", ""),
        help="默认读取 IBKR_FLEX_QUERY_ID",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--max-attempts", type=int, default=12)
    args = parser.parse_args(argv)

    try:
        payload = query_flex_statement(
            args.from_date,
            args.to_date,
            query_id=args.query_id,
            poll_seconds=args.poll_seconds,
            max_attempts=args.max_attempts,
        )
        rows = extract_trade_rows(payload)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        output_path = args.output_dir / _default_filename(args.from_date, args.to_date)
        output_path.write_bytes(payload)
        summary = {
            "from": args.from_date.isoformat(),
            "to": args.to_date.isoformat(),
            "trade_count": len(rows),
            "output": str(output_path),
        }
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    except (FlexError, OSError, ValueError) as exc:
        print(f"查询失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
