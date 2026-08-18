from __future__ import annotations

import argparse
import csv
import getpass
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Mapping


FLEX_BASE_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
FLEX_VERSION = "3"
DEFAULT_USER_AGENT = "ETFRedemptionCalculator/2.0 Python"
DEFAULT_KEYCHAIN_SERVICE = "ETFRedemptionCalculator.IBKRFlex"
RETRYABLE_ERROR_CODES = {
    "1001",  # Statement could not be generated at this time.
    "1003",  # Statement is not available.
    "1004",  # Statement is incomplete.
    "1005",  # Settlement data is not ready.
    "1006",  # FIFO P/L data is not ready.
    "1007",  # MTM P/L data is not ready.
    "1008",  # MTM and FIFO P/L data is not ready.
    "1009",  # Server under heavy load.
    "1019",  # Statement generation in progress.
}
TRADE_RECORD_TAGS = {"Trade", "Order"}
BORROW_FEE_RECORD_TAGS = {"BorrowFeeDetail", "HardToBorrowDetail"}


class FlexError(RuntimeError):
    """A sanitized Flex Web Service error that never includes the access token."""


@dataclass(frozen=True)
class ServiceResponse:
    status: str
    reference_code: str = ""
    error_code: str = ""
    error_message: str = ""


@dataclass(frozen=True)
class StatementSummary:
    format: str
    from_date: str
    to_date: str
    trade_count: int
    borrow_fee_count: int


def parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期格式应为 YYYY-MM-DD：{value}") from exc


def validate_date_range(from_date: date, to_date: date) -> None:
    if to_date < from_date:
        raise ValueError("结束日期不能早于开始日期")
    if (to_date - from_date).days + 1 > 365:
        raise ValueError("Flex 单次日期覆盖最多 365 天，请拆分后下载")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(root: ET.Element, name: str) -> str:
    for child in root:
        if _local_name(child.tag) == name:
            return str(child.text or "").strip()
    return ""


def parse_service_response(payload: bytes) -> ServiceResponse | None:
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


def _service_error(response: ServiceResponse) -> FlexError:
    detail = response.error_message or "未知错误"
    code = response.error_code or "unknown"
    return FlexError(f"IBKR Flex 返回错误 {code}：{detail}")


def load_token(
    env_name: str,
    keychain_service: str,
    keychain_account: str,
) -> tuple[str, str]:
    token = str(os.environ.get(env_name, "")).strip()
    if token:
        return token, f"环境变量 {env_name}"
    if sys.platform != "darwin" or not keychain_service.strip():
        return "", ""
    try:
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-a",
                keychain_account,
                "-s",
                keychain_service,
                "-w",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return "", ""
    token = result.stdout.strip() if result.returncode == 0 else ""
    return (token, f"macOS 钥匙串 {keychain_service}") if token else ("", "")


def download_statement(
    token: str,
    query_id: str,
    from_date: date,
    to_date: date,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: float = 30,
    poll_seconds: float = 5,
    max_attempts: int = 12,
    request_fn: Callable[[str, Mapping[str, str], str, float], bytes] = _request_bytes,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> bytes:
    validate_date_range(from_date, to_date)
    token = token.strip()
    query_id = query_id.strip()
    if not token:
        raise ValueError("Flex Token 不能为空")
    if not query_id:
        raise ValueError("Flex Query ID 不能为空")
    if not user_agent.strip():
        raise ValueError("User-Agent 不能为空")
    if max_attempts < 1:
        raise ValueError("max_attempts 必须至少为 1")

    send_payload = request_fn(
        "SendRequest",
        {
            "t": token,
            "q": query_id,
            "fd": from_date.strftime("%Y%m%d"),
            "td": to_date.strftime("%Y%m%d"),
            "v": FLEX_VERSION,
        },
        user_agent,
        timeout,
    )
    send_response = parse_service_response(send_payload)
    if send_response is None:
        raise FlexError("IBKR Flex SendRequest 返回了无法识别的响应")
    if send_response.status.lower() != "success":
        raise _service_error(send_response)
    if not send_response.reference_code:
        raise FlexError("IBKR Flex SendRequest 成功但未返回 ReferenceCode")

    if poll_seconds > 0:
        sleep_fn(poll_seconds)
    for attempt in range(max_attempts):
        payload = request_fn(
            "GetStatement",
            {
                "t": token,
                "q": send_response.reference_code,
                "v": FLEX_VERSION,
            },
            user_agent,
            timeout,
        )
        response = parse_service_response(payload)
        if response is None:
            return payload
        if response.status.lower() == "success":
            # A successful GetStatement normally returns the report directly.
            # Keep this branch for forward compatibility with a wrapped response.
            return payload
        if response.error_code not in RETRYABLE_ERROR_CODES:
            raise _service_error(response)
        if attempt + 1 >= max_attempts:
            raise FlexError(
                f"IBKR Flex 报表在 {max_attempts} 次查询后仍未生成完成"
                f"（最后错误 {response.error_code}）"
            )
        if poll_seconds > 0:
            sleep_fn(min(30.0, poll_seconds * (1.5 ** min(attempt + 1, 4))))
    raise AssertionError("unreachable")


def summarize_statement(payload: bytes) -> StatementSummary:
    stripped = payload.lstrip()
    if stripped.startswith(b"<"):
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            raise FlexError(f"下载结果不是有效 XML：{exc}") from None
        trade_count = 0
        borrow_fee_count = 0
        from_date = ""
        to_date = ""
        for element in root.iter():
            name = _local_name(element.tag)
            # IBKR uses <Trade> for Execution output and <Order> for Order
            # output.  Both represent rows from the Activity Flex Trades
            # section and should be reported through the same counter.
            if name in TRADE_RECORD_TAGS:
                trade_count += 1
            # The portal labels this section "Borrow Fees Details", while
            # current Flex XML emits <HardToBorrowDetail> records.  Keep the
            # older name for compatibility with previously saved templates.
            elif name in BORROW_FEE_RECORD_TAGS:
                borrow_fee_count += 1
            elif name == "FlexStatement":
                from_date = from_date or str(element.attrib.get("fromDate") or "")
                to_date = to_date or str(element.attrib.get("toDate") or "")
        return StatementSummary("xml", from_date, to_date, trade_count, borrow_fee_count)

    text = payload.decode("utf-8-sig", errors="replace")
    trade_count = 0
    borrow_fee_count = 0
    for row in csv.reader(text.splitlines()):
        if len(row) < 2 or row[1] != "Data":
            continue
        section = row[0].strip().lower()
        if section in {"交易", "trades", "trade"}:
            trade_count += 1
        elif section in {
            "借入费用详情",
            "borrow fee details",
            "borrowfeedetails",
            "hard to borrow details",
            "hardtoborrowdetails",
        }:
            borrow_fee_count += 1
    return StatementSummary("csv", "", "", trade_count, borrow_fee_count)


def save_statement(payload: bytes, output_dir: Path, from_date: date, to_date: date) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".xml" if payload.lstrip().startswith(b"<") else ".csv"
    destination = output_dir / f"ib_activity_{from_date:%Y%m%d}_{to_date:%Y%m%d}{suffix}"
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(destination)
    return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="通过 IBKR Flex Web Service 下载指定日期段的 Activity Flex Query。"
    )
    parser.add_argument("--from", dest="from_date", required=True, type=parse_iso_date)
    parser.add_argument("--to", dest="to_date", required=True, type=parse_iso_date)
    parser.add_argument(
        "--query-id",
        default=os.environ.get("IBKR_FLEX_QUERY_ID", ""),
        help="Flex Query ID；默认读取 IBKR_FLEX_QUERY_ID。",
    )
    parser.add_argument(
        "--token-env",
        default="IBKR_FLEX_TOKEN",
        help="保存 Token 的环境变量名；默认 IBKR_FLEX_TOKEN。",
    )
    parser.add_argument(
        "--keychain-service",
        default=DEFAULT_KEYCHAIN_SERVICE,
        help="环境变量无 Token 时读取的 macOS 钥匙串服务名。",
    )
    parser.add_argument(
        "--keychain-account",
        default=getpass.getuser(),
        help="macOS 钥匙串账户名；默认当前系统用户。",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("ib_flex_cache"))
    parser.add_argument("--poll-seconds", type=float, default=5)
    parser.add_argument("--max-attempts", type=int, default=12)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只验证日期和凭据是否已配置，不向 IBKR 发送请求。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        validate_date_range(args.from_date, args.to_date)
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2

    token, token_source = load_token(
        args.token_env,
        args.keychain_service,
        args.keychain_account,
    )
    query_id = str(args.query_id or "").strip()
    if not token or not query_id:
        missing = []
        if not token:
            missing.append(f"{args.token_env}/macOS 钥匙串 {args.keychain_service}")
        if not query_id:
            missing.append("IBKR_FLEX_QUERY_ID/--query-id")
        print("缺少 Flex 配置：" + "、".join(missing), file=sys.stderr)
        return 2
    if args.dry_run:
        print(
            f"配置已就绪：{args.from_date} 至 {args.to_date}；"
            f"Query ID {query_id}；Token 来自{token_source}且不会显示。"
        )
        return 0

    try:
        payload = download_statement(
            token,
            query_id,
            args.from_date,
            args.to_date,
            poll_seconds=args.poll_seconds,
            max_attempts=args.max_attempts,
        )
        summary = summarize_statement(payload)
        destination = save_statement(payload, args.output_dir, args.from_date, args.to_date)
    except (FlexError, ValueError, OSError) as exc:
        print(f"Flex 下载失败：{exc}", file=sys.stderr)
        return 1

    period = (
        f"；报表区间 {summary.from_date} 至 {summary.to_date}"
        if summary.from_date or summary.to_date
        else ""
    )
    print(
        f"Flex 下载成功：{destination}；格式 {summary.format.upper()}；"
        f"Trades {summary.trade_count} 条；Borrow Fee Details {summary.borrow_fee_count} 条"
        f"{period}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
