#!/usr/bin/env python3
"""Fill missing focused SZSE/SSE PCFs for the latest two calendar months.

The two exchanges are processed in parallel, while every exchange keeps its
own serial request queue.  The existing ``SzsePcfStore`` lock, cooldown and
cache files remain the source of truth, so the command is safe to stop and
rerun: a cached XML is skipped on the next run.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

import szse_pcf


ROOT = Path(__file__).resolve().parent
SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_CACHE_DIR = ROOT / "szse_pcf_cache"
DEFAULT_INTERVAL_SECONDS = 5


@dataclass(frozen=True)
class PcfTask:
    trading_day: date
    exchange: str
    fund_code: str


@dataclass
class BackfillStats:
    exchange: str
    attempted: int = 0
    cached: int = 0
    unavailable: int = 0
    failed: int = 0
    stopped_for_cooldown: bool = False


def subtract_calendar_months(value: date, months: int) -> date:
    """Return the same calendar day ``months`` earlier, clamped to month-end."""
    if months < 0:
        raise ValueError("months 不能为负数")
    year = value.year
    month = value.month - months
    while month <= 0:
        year -= 1
        month += 12
    first_next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    last_day = (first_next_month - timedelta(days=1)).day
    return date(year, month, min(value.day, last_day))


def weekday_range(start_day: date, end_day: date) -> Iterable[date]:
    current = start_day
    while current <= end_day:
        if current.weekday() < 5:
            yield current
        current += timedelta(days=1)


def build_missing_tasks(
    cache_dir: Path,
    start_day: date,
    end_day: date,
    *,
    interval_seconds: int,
) -> dict[str, list[PcfTask]]:
    """Build cache-only task lists; this stage never accesses an exchange."""
    store = szse_pcf.SzsePcfStore(
        cache_dir,
        min_request_interval_seconds=interval_seconds,
    )
    tasks = {szse_pcf.EXCHANGE_SZSE: [], szse_pcf.EXCHANGE_SSE: []}
    for trading_day in weekday_range(start_day, end_day):
        for item in store.build_focus_day_index(trading_day).items:
            exchange = szse_pcf.normalize_exchange(item.exchange)
            if exchange not in tasks:
                continue
            if not store.is_fund_detail_cached(trading_day, item.fund_code, exchange):
                tasks[exchange].append(PcfTask(trading_day, exchange, item.fund_code))
    return tasks


def is_cooldown_error(error: Exception) -> bool:
    text = str(error)
    return "冷却期" in text or "已暂停" in text


def fill_exchange(
    cache_dir: Path,
    exchange: str,
    tasks: list[PcfTask],
    *,
    interval_seconds: int,
    progress: Callable[[str], None],
) -> BackfillStats:
    """Run one exchange's queue, preserving five-second request spacing."""
    stats = BackfillStats(exchange=exchange)
    store = szse_pcf.SzsePcfStore(
        cache_dir,
        min_request_interval_seconds=interval_seconds,
    )
    total = len(tasks)
    for index, task in enumerate(tasks, start=1):
        try:
            # The cache may have been completed after task discovery, including
            # by another earlier invocation, so always make this final check.
            if store.is_fund_detail_cached(task.trading_day, task.fund_code, task.exchange):
                stats.cached += 1
                continue
            stats.attempted += 1
            store.ensure_fund_xml_cached(task.trading_day, task.fund_code, exchange=task.exchange)
        except szse_pcf.SzsePcfNotFoundError:
            stats.unavailable += 1
            progress(f"[{exchange} {index}/{total}] {task.trading_day} {task.fund_code}：交易所暂无 PCF")
        except szse_pcf.SzsePcfError as exc:
            stats.failed += 1
            progress(f"[{exchange} {index}/{total}] {task.trading_day} {task.fund_code}：{exc}")
            if is_cooldown_error(exc):
                stats.stopped_for_cooldown = True
                progress(f"[{exchange}] 已遵守交易所冷却期停止；稍后直接重跑本命令即可续传。")
                break
        except Exception as exc:  # Defensive: persist through an isolated bad file.
            stats.failed += 1
            progress(f"[{exchange} {index}/{total}] {task.trading_day} {task.fund_code}：未预期错误 {exc}")
        else:
            stats.cached += 1
            progress(f"[{exchange} {index}/{total}] 已缓存 {task.trading_day} {task.fund_code}")
    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="并行补全最近两个月列表内缺失的 SZSE/SSE ETF PCF（默认每交易所每次请求间隔 5 秒）。"
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help=f"PCF 缓存目录（默认：{DEFAULT_CACHE_DIR}）",
    )
    parser.add_argument(
        "--end",
        type=date.fromisoformat,
        default=datetime.now(SHANGHAI).date(),
        help="结束日期 YYYY-MM-DD（默认：北京时间今天）",
    )
    parser.add_argument(
        "--months",
        type=int,
        default=2,
        help="回补的自然月数（默认：2）",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=DEFAULT_INTERVAL_SECONDS,
        help="同一交易所相邻 HTTP 请求的最小间隔秒数（默认：5）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅列出缺失数量，不访问交易所",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.months <= 0:
        raise SystemExit("--months 必须大于 0")
    if args.interval_seconds < 5:
        raise SystemExit("为避免触发交易所限流，--interval-seconds 不能小于 5")
    cache_dir = args.cache_dir.expanduser().resolve()
    end_day = args.end
    start_day = subtract_calendar_months(end_day, args.months)
    tasks = build_missing_tasks(
        cache_dir,
        start_day,
        end_day,
        interval_seconds=args.interval_seconds,
    )
    print(
        f"扫描区间：{start_day:%Y-%m-%d} 至 {end_day:%Y-%m-%d}（仅周一至周五）\n"
        f"缺失 XML：SZSE {len(tasks[szse_pcf.EXCHANGE_SZSE])} 份；"
        f"SSE {len(tasks[szse_pcf.EXCHANGE_SSE])} 份；"
        f"同一交易所请求间隔：{args.interval_seconds} 秒",
        flush=True,
    )
    if args.dry_run:
        return 0

    def progress(message: str) -> None:
        print(message, flush=True)

    stats: list[BackfillStats] = []
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="pcf-backfill") as executor:
        futures = [
            executor.submit(
                fill_exchange,
                cache_dir,
                exchange,
                tasks[exchange],
                interval_seconds=args.interval_seconds,
                progress=progress,
            )
            for exchange in (szse_pcf.EXCHANGE_SZSE, szse_pcf.EXCHANGE_SSE)
            if tasks[exchange]
        ]
        for future in as_completed(futures):
            stats.append(future.result())

    for item in sorted(stats, key=lambda value: value.exchange):
        print(
            f"{item.exchange} 完成：请求 {item.attempted}，已缓存 {item.cached}，"
            f"暂无 {item.unavailable}，失败 {item.failed}"
            + ("，因冷却期提前停止" if item.stopped_for_cooldown else ""),
            flush=True,
        )
    return 0 if all(not item.stopped_for_cooldown for item in stats) else 2


if __name__ == "__main__":
    raise SystemExit(main())
