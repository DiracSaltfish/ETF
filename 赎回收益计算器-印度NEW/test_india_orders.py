from __future__ import annotations

from datetime import date

from india_calendar import beijing_display
from india_config import IndiaConfig
from india_order_planner import build_inda_close_plan, build_swap_plan, split_inda_close_qty


def test_nifty_rolls_on_last_tuesday() -> None:
    config = IndiaConfig()
    before = build_swap_plan(date(2026, 7, 27), 1, config)
    on_roll = build_swap_plan(date(2026, 7, 28), 1, config)
    assert before[0].symbol == "NIFTYN26"
    assert before[0].contract_month == "202607"
    assert on_roll[0].symbol == "NIFTYQ26"
    assert on_roll[0].contract_month == "202608"


def test_inda_standard_split_is_exact() -> None:
    assert split_inda_close_qty(970, IndiaConfig()) == (364, 606)
    specs = build_inda_close_plan(date(2026, 7, 6), 970, IndiaConfig())
    assert [item.quantity for item in specs] == [364, 606]
    assert beijing_display(specs[0].trigger_dt).endswith("23:30:00 CST")
    assert beijing_display(specs[1].trigger_dt).endswith("03:59:00 CST")
