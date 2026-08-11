# -*- coding: utf-8 -*-
"""
QMT 内置 Python：订阅深圳 ETF 实时申购赎回统计数据。

运行位置：QMT 的“模型研究/策略编辑器”，不是独立 Python 终端。
运行方式：实盘行情环境运行；该数据不能用普通历史回测验证。

官方数据周期：etfstatistics
官方字段：申购笔数、申购数量、申购金额、赎回笔数、赎回数量、赎回金额。

注意：官方将此数据标为“迅投研专属”，需要尊享投研端及对应数据权限；
普通 QMT L2 权限不等同于 etfstatistics 权限。
"""

import datetime
import time


# 本脚本只检测 159518.SZ 的实时申购赎回数据。
STOCK_LIST = [
    "159518.SZ",
]

PERIOD = "etfstatistics"

# 官方原生 Python 回调使用英文键；get_market_data_ex 展示时使用中文列名。
# buyMoney 的官方回调样例曾出现尾部空格，下面会先 strip 再匹配。
FIELD_ALIASES = {
    "time": ("time", "时间"),
    "buyNumber": ("buyNumber", "申购笔数"),
    "buyAmount": ("buyAmount", "申购数量"),
    "buyMoney": ("buyMoney", "申购金额"),
    "sellNumber": ("sellNumber", "赎回笔数"),
    "sellAmount": ("sellAmount", "赎回数量"),
    "sellMoney": ("sellMoney", "赎回金额"),
}

SUBSCRIPTION_IDS = []
CALLBACK_COUNT = 0
LAST_CALLBACK_AT = 0.0
LAST_HEARTBEAT_AT = 0.0


def _clean_dict_keys(row):
    cleaned = {}
    for key, value in row.items():
        if isinstance(key, str):
            key = key.strip()
        cleaned[key] = value
    return cleaned


def _pick(row, names):
    for name in names:
        if name in row:
            return row[name]
    return None


def _latest_scalar(value):
    """兼容 result_type='list' 可能产生的单字段列表。"""
    if isinstance(value, (list, tuple)):
        return value[-1] if value else None
    return value


def _rows_from_payload(payload):
    """兼容 QMT 不同版本可能返回的 dict/list/DataFrame 结构。"""
    if payload is None:
        return []

    if isinstance(payload, (list, tuple)):
        return [item for item in payload if isinstance(item, dict)]

    if isinstance(payload, dict):
        # result_type='list'：{字段: [值1, 值2, ...]}
        if payload and any(isinstance(value, (list, tuple)) for value in payload.values()):
            return [{key: _latest_scalar(value) for key, value in payload.items()}]
        return [payload]

    # 默认 result_type 可能是 pandas.DataFrame。
    if hasattr(payload, "to_dict") and hasattr(payload, "columns"):
        try:
            return payload.to_dict("records")
        except Exception:
            try:
                records = payload.to_dict("index")
                return list(records.values())
            except Exception:
                return []

    return []


def _format_time(value):
    if value is None:
        return ""
    try:
        number = float(value)
        # etfstatistics 官方样例为 13 位毫秒时间戳。
        if number > 100000000000:
            number = number / 1000.0
        return datetime.datetime.fromtimestamp(number).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value)


def _normalize_row(row):
    row = _clean_dict_keys(row)
    normalized = {}
    for target_name, aliases in FIELD_ALIASES.items():
        normalized[target_name] = _latest_scalar(_pick(row, aliases))
    return normalized


def on_etf_statistics(data):
    """etfstatistics 实时推送回调。"""
    global CALLBACK_COUNT, LAST_CALLBACK_AT

    CALLBACK_COUNT += 1
    LAST_CALLBACK_AT = time.time()

    if not isinstance(data, dict):
        print("[ETF申赎][异常结构] type=%s raw=%r" % (type(data).__name__, data))
        return

    printed = False
    for stock_code, payload in data.items():
        for raw_row in _rows_from_payload(payload):
            row = _normalize_row(raw_row)
            print(
                "[ETF申赎] %s %s | 申购: 笔数=%s 数量=%s 金额=%s | "
                "赎回: 笔数=%s 数量=%s 金额=%s"
                % (
                    _format_time(row["time"]),
                    stock_code,
                    row["buyNumber"],
                    row["buyAmount"],
                    row["buyMoney"],
                    row["sellNumber"],
                    row["sellAmount"],
                    row["sellMoney"],
                )
            )
            printed = True

    if not printed:
        print("[ETF申赎][未识别结构] raw=%r" % (data,))


def _subscribe_one(C, stock_code):
    """优先使用带 result_type 的新接口，兼容较老的内置 Python 签名。"""
    try:
        return C.subscribe_quote(
            stock_code=stock_code,
            period=PERIOD,
            dividend_type="none",
            result_type="dict",
            callback=on_etf_statistics,
        )
    except TypeError:
        return C.subscribe_quote(
            stock_code,
            PERIOD,
            callback=on_etf_statistics,
        )


def init(C):
    global SUBSCRIPTION_IDS

    SUBSCRIPTION_IDS = []
    print("=" * 72)
    print("开始订阅深圳 ETF 实时申赎数据，period=%s" % PERIOD)
    print("普通 L2 不代表具备该数据权限；官方要求尊享投研端及对应权限。")

    for stock_code in STOCK_LIST:
        try:
            sub_id = _subscribe_one(C, stock_code)
            SUBSCRIPTION_IDS.append((stock_code, sub_id))
            if isinstance(sub_id, int) and sub_id > 0:
                print("[订阅成功] %s sub_id=%s" % (stock_code, sub_id))
            else:
                print("[订阅失败或未确认] %s sub_id=%r" % (stock_code, sub_id))
        except Exception as exc:
            print("[订阅异常] %s %s: %s" % (stock_code, type(exc).__name__, exc))

    # 方便在 QMT 变量观察窗口中检查。
    C.etfstatistics_subscription_ids = SUBSCRIPTION_IDS
    print("订阅初始化完成；交易时段内等待回调。")
    print("=" * 72)


def handlebar(C):
    """不主动轮询，只输出低频心跳，避免阻塞 QMT 的共享策略线程。"""
    global LAST_HEARTBEAT_AT

    now = time.time()
    if now - LAST_HEARTBEAT_AT < 30:
        return
    LAST_HEARTBEAT_AT = now

    if CALLBACK_COUNT:
        age = now - LAST_CALLBACK_AT
        print("[ETF申赎][心跳] 已收到 %s 次回调，距最近回调 %.1f 秒" % (CALLBACK_COUNT, age))
    else:
        print(
            "[ETF申赎][等待] 尚未收到回调。若当前为深市交易时段且持续无数据，"
            "请重点核对尊享投研端、etfstatistics 数据权限和客户端版本。"
        )
