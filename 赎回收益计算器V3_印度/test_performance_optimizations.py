from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import ib_data_source
import realtime_premium
import redemption_engine as engine


ROOT = Path(__file__).resolve().parent


def _qmt_record(source: str, row_number: int = 1) -> engine.QmtRecord:
    return engine.QmtRecord(
        source=source,
        row_number=row_number,
        trade_day=date(2026, 7, 3),
        contract_no=row_number,
        action="证券买入",
        qty=1_000_000,
        price=Decimal("1"),
        amount=Decimal("1000000"),
        code=engine.TARGET_CODE,
        name="标普油气",
    )


def _ib_trade() -> engine.IbTrade:
    return engine.IbTrade(
        id="ib-1",
        row_number=1,
        dt=datetime(2026, 7, 3, 9, 30),
        qty=-990,
        price=Decimal("150"),
        gross=Decimal("148500"),
        commission=Decimal("1"),
        marker="",
    )


def _realtime_inputs() -> tuple[
    realtime_premium.XopQuote,
    realtime_premium.SinaQuote,
    realtime_premium.CfetsQuote,
    realtime_premium.PremiumValuation,
]:
    now = datetime.fromisoformat("2026-07-06T09:31:02+08:00")
    xop = realtime_premium.XopQuote(
        Decimal("150"), Decimal("150.1"), Decimal("150.05"), now, "Live"
    )
    domestic = realtime_premium.SinaQuote(
        "159518",
        "标普油气",
        Decimal("1.050"),
        Decimal("1.051"),
        100_000,
        80_000,
        Decimal("1.050"),
        now.replace(tzinfo=None),
        now.replace(tzinfo=None),
        bids=(
            realtime_premium.QuoteLevel(Decimal("1.050"), 100_000),
            realtime_premium.QuoteLevel(Decimal("1.049"), 90_000),
        ),
        asks=(
            realtime_premium.QuoteLevel(Decimal("1.051"), 80_000),
            realtime_premium.QuoteLevel(Decimal("1.052"), 70_000),
        ),
    )
    cfets = realtime_premium.CfetsQuote(
        Decimal("7"), date(2026, 7, 6), "10:00", "2026-07-06T10:00:00"
    )
    valuation = realtime_premium.calculate_premium_valuation(
        xop.bid,
        xop.ask,
        cfets.rate,
        domestic.bid,
        domestic.ask,
        estimate_cash_component_cny=Decimal("1234.56"),
        pcf_trading_day=date(2026, 7, 6),
    )
    return xop, domestic, cfets, valuation


class CalculationInputCacheTests(unittest.TestCase):
    def test_cache_reuses_parsers_and_invalidates_in_place_and_atomic_replacements(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            qmt1 = root / "qmt1.xlsx"
            qmt2 = root / "qmt2.xlsx"
            ib_path = root / "ib.csv"
            for path, content in ((qmt1, "q1"), (qmt2, "q2"), (ib_path, "ib")):
                path.write_text(content, encoding="utf-8")

            def load_qmt(_path: Path | str, source: str) -> list[engine.QmtRecord]:
                return [_qmt_record(source, 1 if source == "QMT1" else 2)]

            cache = engine.CalculationInputCache()
            qmt_paths = {"QMT1": qmt1, "QMT2": qmt2}
            with (
                patch.object(engine, "load_qmt_file", side_effect=load_qmt) as qmt_loader,
                patch.object(engine, "load_ib_statement", return_value=([_ib_trade()], [])) as ib_loader,
                patch.object(engine, "load_ib_stock_trades", return_value=[]) as stock_loader,
                patch.object(engine, "_load_qmt_time_hints", return_value={}) as hint_loader,
            ):
                first = cache.load(qmt_paths, ib_path)
                second = cache.load(qmt_paths, ib_path)
                self.assertEqual(first, second)
                self.assertEqual(qmt_loader.call_count, 2)
                self.assertEqual(ib_loader.call_count, 1)
                self.assertEqual(stock_loader.call_count, 1)
                self.assertEqual(hint_loader.call_count, 1)

                qmt1.write_text("q1-expanded", encoding="utf-8")
                cache.load(qmt_paths, ib_path)
                self.assertEqual(qmt_loader.call_count, 3)
                self.assertEqual(ib_loader.call_count, 1)

                replacement = root / "qmt2.replacement"
                replacement.write_text("q2", encoding="utf-8")
                os.replace(replacement, qmt2)
                cache.load(qmt_paths, ib_path)
                self.assertEqual(qmt_loader.call_count, 4)

                ib_path.write_text("ib-expanded", encoding="utf-8")
                cache.load(qmt_paths, ib_path)
                self.assertEqual(ib_loader.call_count, 2)
                self.assertEqual(stock_loader.call_count, 2)

    def test_time_hint_cache_invalidates_when_a_hint_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            qmt1 = root / "qmt1.xlsx"
            ib_path = root / "ib.csv"
            qmt1.write_text("q1", encoding="utf-8")
            ib_path.write_text("ib", encoding="utf-8")
            hint_dir = root / "hints" / "20260703"
            hint_dir.mkdir(parents=True)
            hint_path = hint_dir / "QMT成交时间.csv"
            hint_path.write_text("first", encoding="utf-8")
            cache = engine.CalculationInputCache()

            with (
                patch.object(engine, "load_qmt_file", return_value=[_qmt_record("QMT1")]),
                patch.object(engine, "load_ib_statement", return_value=([], [])),
                patch.object(engine, "load_ib_stock_trades", return_value=[]),
                patch.object(engine, "_load_qmt_time_hints", return_value={}) as hint_loader,
            ):
                cache.load({"QMT1": qmt1}, ib_path, root / "hints")
                cache.load({"QMT1": qmt1}, ib_path, root / "hints")
                self.assertEqual(hint_loader.call_count, 1)

                hint_path.write_text("second-and-longer", encoding="utf-8")
                cache.load({"QMT1": qmt1}, ib_path, root / "hints")
                self.assertEqual(hint_loader.call_count, 2)

    def test_current_business_result_is_identical_with_and_without_cache(self) -> None:
        config_path = ROOT / "config.json"
        if not config_path.exists():
            self.skipTest("当前项目配置不存在")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        qmt_paths = {
            "QMT1": config.get("qmt1_path"),
            "QMT2": config.get("qmt2_path") or None,
            "QMT3": config.get("qmt3_path") or None,
        }
        if str(config.get("ib_data_source_mode") or "flex_auto") == "flex_auto":
            ib_path = ib_data_source.cached_csv_path(config["ib_flex_cache_dir"])
        else:
            ib_path = Path(str(config.get("ib_path") or ""))
        required = [Path(str(path)) for path in qmt_paths.values() if path]
        required.append(Path(ib_path))
        if any(not path.is_file() for path in required):
            self.skipTest("当前配置的数据文件不完整")

        overrides = engine.load_overrides(ROOT / "ib_mapping_overrides.json")
        annotations = engine.load_strategy_annotations(ROOT / "ib_strategy_annotations.json")
        holidays = [date.fromisoformat(value) for value in config.get("market_holidays", [])]
        kwargs = {
            "qmt_paths": qmt_paths,
            "ib_path": ib_path,
            "fx_rate": Decimal(str(config["fx_rate"])),
            "overrides": overrides,
            "market_holidays": holidays,
            "transfer_contract_gap": int(config.get("transfer_contract_gap") or 1000),
            "qmt_time_root": config.get("shared_folder_path") or None,
            "strategy_annotations": annotations,
        }
        uncached = engine.calculate(**kwargs)
        cache = engine.CalculationInputCache()
        cached_first = engine.calculate(**kwargs, input_cache=cache)
        cached_second = engine.calculate(**kwargs, input_cache=cache)
        self.assertEqual(asdict(uncached), asdict(cached_first))
        self.assertEqual(asdict(uncached), asdict(cached_second))


class RealtimeFileOptimizationTests(unittest.TestCase):
    def setUp(self) -> None:
        realtime_premium._clear_schema_document_cache()

    def test_schema_document_is_idempotent_and_repairs_change_or_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            now = datetime.fromisoformat("2026-07-06T09:31:02+08:00")
            original_writer = realtime_premium._atomic_write_text
            with patch.object(
                realtime_premium, "_atomic_write_text", wraps=original_writer
            ) as writer:
                path = realtime_premium.write_schema_document(temp_dir, generated_at=now)
                realtime_premium.write_schema_document(temp_dir, generated_at=now)
                self.assertEqual(writer.call_count, 1)

                path.write_text("corrupted", encoding="utf-8")
                realtime_premium.write_schema_document(temp_dir, generated_at=now)
                self.assertEqual(writer.call_count, 2)
                self.assertEqual(path.read_text(encoding="utf-8"), realtime_premium.REALTIME_SCHEMA_DOCUMENT)

                path.unlink()
                realtime_premium.write_schema_document(temp_dir, generated_at=now)
                self.assertEqual(writer.call_count, 3)

    def test_concurrent_schema_requests_perform_one_atomic_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            now = datetime.fromisoformat("2026-07-06T09:31:02+08:00")
            original_writer = realtime_premium._atomic_write_text
            errors: list[BaseException] = []

            def write() -> None:
                try:
                    realtime_premium.write_schema_document(temp_dir, generated_at=now)
                except BaseException as exc:  # pragma: no cover - diagnostic capture
                    errors.append(exc)

            with patch.object(
                realtime_premium, "_atomic_write_text", wraps=original_writer
            ) as writer:
                threads = [threading.Thread(target=write) for _ in range(8)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                self.assertEqual(errors, [])
                self.assertEqual(writer.call_count, 1)

    def test_repeated_json_snapshots_write_schema_only_once_and_keep_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            xop, domestic, cfets, valuation = _realtime_inputs()
            now = datetime.fromisoformat("2026-07-06T09:31:02+08:00")
            original_writer = realtime_premium._atomic_write_text
            with patch.object(
                realtime_premium, "_atomic_write_text", wraps=original_writer
            ) as writer:
                json_path, document_path = realtime_premium.write_realtime_files(
                    temp_dir, xop, domestic, cfets, valuation, generated_at=now
                )
                first_payload = json.loads(json_path.read_text(encoding="utf-8"))
                realtime_premium.write_realtime_files(
                    temp_dir, xop, domestic, cfets, valuation, generated_at=now
                )
                second_payload = json.loads(json_path.read_text(encoding="utf-8"))

            self.assertEqual(writer.call_count, 3)  # JSON twice, schema once.
            self.assertTrue(document_path.is_file())
            self.assertEqual(first_payload, second_payload)
            self.assertEqual(first_payload["schema_version"], 2)
            self.assertEqual(first_payload["valuation"]["basket"]["bid_cny"], 1_047_034.56)
            self.assertEqual(first_payload["valuation"]["nav"]["bid"], 1.04703456)
            self.assertEqual(first_payload["order_book"]["bid1"]["price"], 1.05)
            self.assertNotIn("volume", json_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
