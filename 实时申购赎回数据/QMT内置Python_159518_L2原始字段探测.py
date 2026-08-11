# -*- coding: utf-8 -*-
"""
QMT 内置 Python：探测 159518.SZ 的普通 Level-2 快照是否透传
深交所 300111 快照中的 ETF 实时申购/赎回字段（xw/xx）。

本脚本不请求 etfstatistics，因此不会触发此前的 -93 权限错误。
它订阅 QMT 已公开的 l2quote 和 l2quoteaux 周期，完整打印首次回调字段，
并持续扫描疑似 ETF 申购/赎回字段。
"""

import time


TARGET = "159518.SZ"
PERIODS = ("l2quote", "l2quoteaux")

SUBSCRIPTION_IDS = []
CALLBACK_COUNTS = {"l2quote": 0, "l2quoteaux": 0}
PRINTED_FULL_PAYLOAD = set()
LAST_HEARTBEAT_AT = 0.0
FOUND_CANDIDATE_FIELDS = {}


# 不同厂商可能使用的字段命名。这里也包含深交所原始条目名 xw/xx。
EXACT_CANDIDATE_KEYS = {
    "xw",
    "xx",
    "buynumber",
    "buyamount",
    "buymoney",
    "sellnumber",
    "sellamount",
    "sellmoney",
    "etfbuycount",
    "etfbuyqty",
    "etfbuymoney",
    "etfsellcount",
    "etfsellqty",
    "etfsellmoney",
    "creationcount",
    "creationqty",
    "creationvolume",
    "redemptioncount",
    "redemptionqty",
    "redemptionvolume",
}


def _compact_key(key):
    return str(key).strip().lower().replace("_", "").replace("-", "").replace(" ", "")


def _is_candidate_key(key):
    text = _compact_key(key)
    if text in EXACT_CANDIDATE_KEYS:
        return True
    if "申购" in str(key) or "赎回" in str(key):
        return True
    if "creation" in text or "redemption" in text or "redeem" in text:
        return True
    if "etf" in text and any(word in text for word in ("buy", "sell", "purchase", "qty", "volume")):
        return True
    return False


def _payload_rows(payload):
    """兼容 result_type='dict' 以及少数旧版本返回的 DataFrame/list。"""
    if payload is None:
        return []
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, (list, tuple)):
        return [item for item in payload if isinstance(item, dict)]
    if hasattr(payload, "to_dict") and hasattr(payload, "columns"):
        try:
            return payload.to_dict("records")
        except Exception:
            try:
                rows = payload.to_dict("index")
                return list(rows.values())
            except Exception:
                return []
    return []


def _print_first_payload(period, stock_code, row):
    marker = (period, stock_code)
    if marker in PRINTED_FULL_PAYLOAD:
        return
    PRINTED_FULL_PAYLOAD.add(marker)

    print("=" * 76)
    print("[%s][首次完整字段] %s，共 %s 个字段" % (period, stock_code, len(row)))
    for key in sorted(row.keys(), key=lambda item: str(item)):
        print("  %s = %r" % (key, row[key]))
    print("=" * 76)


def _scan_candidate_fields(period, stock_code, row):
    candidates = {}
    for key, value in row.items():
        if _is_candidate_key(key):
            candidates[key] = value

    if not candidates:
        return

    FOUND_CANDIDATE_FIELDS[period] = sorted(str(key) for key in candidates)
    print("[发现疑似ETF申赎字段] period=%s code=%s values=%r" % (period, stock_code, candidates))


def _process_callback(period, data):
    CALLBACK_COUNTS[period] = CALLBACK_COUNTS.get(period, 0) + 1

    if not isinstance(data, dict):
        print("[%s][异常回调结构] type=%s raw=%r" % (period, type(data).__name__, data))
        return

    for stock_code, payload in data.items():
        rows = _payload_rows(payload)
        if not rows:
            print("[%s][未识别数据] %s raw=%r" % (period, stock_code, payload))
            continue
        for row in rows:
            _print_first_payload(period, stock_code, row)
            _scan_candidate_fields(period, stock_code, row)


def on_l2quote(data):
    _process_callback("l2quote", data)


def on_l2quoteaux(data):
    _process_callback("l2quoteaux", data)


CALLBACKS = {
    "l2quote": on_l2quote,
    "l2quoteaux": on_l2quoteaux,
}


def _subscribe(C, period):
    try:
        return C.subscribe_quote(
            stock_code=TARGET,
            period=period,
            dividend_type="none",
            result_type="dict",
            callback=CALLBACKS[period],
        )
    except TypeError:
        # 兼容较老的内置 Python 参数签名。
        return C.subscribe_quote(
            TARGET,
            period,
            callback=CALLBACKS[period],
        )


def init(C):
    global SUBSCRIPTION_IDS

    SUBSCRIPTION_IDS = []
    print("=" * 76)
    print("开始探测 %s 的普通 QMT Level-2 原始字段" % TARGET)
    print("本脚本仅请求 l2quote/l2quoteaux，不请求 etfstatistics。")

    for period in PERIODS:
        try:
            sub_id = _subscribe(C, period)
            SUBSCRIPTION_IDS.append((period, sub_id))
            if isinstance(sub_id, int) and sub_id > 0:
                print("[订阅成功] period=%s sub_id=%s" % (period, sub_id))
            else:
                print("[订阅失败或未确认] period=%s sub_id=%r" % (period, sub_id))
        except Exception as exc:
            print("[订阅异常] period=%s %s: %s" % (period, type(exc).__name__, exc))

    C.l2_probe_subscription_ids = SUBSCRIPTION_IDS
    print("等待交易时段行情回调；首次回调会完整打印全部字段。")
    print("=" * 76)


def handlebar(C):
    global LAST_HEARTBEAT_AT

    now = time.time()
    if now - LAST_HEARTBEAT_AT < 30:
        return
    LAST_HEARTBEAT_AT = now

    print(
        "[L2探测心跳] l2quote回调=%s, l2quoteaux回调=%s, 疑似申赎字段=%r"
        % (
            CALLBACK_COUNTS.get("l2quote", 0),
            CALLBACK_COUNTS.get("l2quoteaux", 0),
            FOUND_CANDIDATE_FIELDS,
        )
    )

    if PRINTED_FULL_PAYLOAD and not FOUND_CANDIDATE_FIELDS:
        print(
            "[当前判断] 已收到普通L2数据，但回调中尚未发现xw/xx或ETF申赎字段；"
            "这通常表示QMT的普通l2quote包装层没有透传该扩展字段。"
        )


def stop(C):
    for period, sub_id in SUBSCRIPTION_IDS:
        if isinstance(sub_id, int) and sub_id > 0:
            try:
                C.unsubscribe_quote(sub_id)
                print("[已反订阅] period=%s sub_id=%s" % (period, sub_id))
            except Exception as exc:
                print("[反订阅异常] period=%s %s" % (period, exc))

