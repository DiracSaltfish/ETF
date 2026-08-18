#!/usr/bin/env python3
"""Build code-level preliminary valuation dossiers from the maintained PCF list.

This is a preprocessing tool, not a trade-signal generator.  It downloads the
latest directly titled prospectus found in Eastmoney's announcement API,
archives the PDF next to the fund's PCF, and creates a code-level Markdown
dossier.  Cash-substitute timing and final FX are deliberately left as
"manual review required" until the actual prospectus clauses are reviewed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

from pypdf import PdfReader


ROOT = Path(__file__).resolve().parent
PCF_ROOT = ROOT / "预扫描数据" / "2026-07-10"
EASTMONEY_API = "https://api.fund.eastmoney.com/f10/JJGG"
PDF_URL = "https://pdf.dfcfw.com/pdf/H2_{announcement_id}_1.pdf"

if str(ROOT.parent / "赎回收益计算器") not in sys.path:
    sys.path.insert(0, str(ROOT.parent / "赎回收益计算器"))

from szse_pcf import FOCUS_FUND_DISPLAY_NAMES, SSE_FUND_DISPLAY_NAMES  # noqa: E402


FAMILY_BY_CODE = {
    **{code: "油气类" for code in ("159518", "513350")},
    **{
        code: "纳斯达克100指数"
        for code in (
            "159501", "159513", "159632", "159659", "159660", "159696", "159941",
            "513100", "513110", "513300", "513390", "513870",
        )
    },
    **{code: "标普500指数" for code in ("159612", "159655", "513500", "513650")},
    **{code: "日经225指数" for code in ("159866", "513000", "513520", "513880")},
    **{code: "德国DAX指数" for code in ("159561", "513030")},
    **{code: "美国50指数" for code in ("159577", "513850")},
    **{code: "港股创新药" for code in ("159567", "159570")},
    **{code: "中概互联网" for code in ("159605", "159607", "513050", "513220")},
    **{code: "美股生物科技" for code in ("159502", "513290")},
    "159509": "纳斯达克科技",
    "159529": "标普消费",
    "159615": "恒生生物科技",
    "159751": "港股通科技",
    "159792": "港股通互联网",
    **{code: "香港金融" for code in ("513750", "513090")},
    "513230": "港股通消费",
    "513080": "法国CAC40",
    "513360": "全球中国教育",
    "513400": "道琼斯工业",
    "513990": "港股通综合",
}

# Preserve the human-maintained naming of dossiers that were completed before
# this batch generator existed.  The PCF maintenance label for 513350 is too
# short and would otherwise create a duplicate next to the audited oil dossier.
DISPLAY_NAME_OVERRIDES = {"513350": "标普油气ETF富国"}

FAMILY_BASELINES = {
    "油气类": ("美股石油天然气成分", "XOP（代理，须滚动 beta）", "USD/CNY"),
    "纳斯达克100指数": ("美股纳斯达克100成分", "QQQ 或 NQ（代理，须滚动 beta）", "USD/CNY"),
    "标普500指数": ("美股标普500成分", "SPY 或 ES（代理，须滚动 beta）", "USD/CNY"),
    "日经225指数": ("日本股票或日本 ETF 包装证券", "日经225期货/高流动性日经 ETF（待代码级核验）", "JPY/CNY"),
    "德国DAX指数": ("德国 DAX 成分", "DAX 期货/高流动性 DAX ETF（待代码级核验）", "EUR/CNY"),
    "美国50指数": ("美国大盘50成分", "同指数 ETF 或美股大盘代理（待代码级核验）", "USD/CNY"),
    "港股创新药": ("港股创新药成分", "逐只港股或行业 ETF（待代码级核验）", "HKD/CNY"),
    "中概互联网": ("港股与 ADR 中国互联网成分", "KWEB 仅作残差代理；不得覆盖实时港股篮子", "HKD/CNY + USD/CNY"),
    "美股生物科技": ("美国生物科技成分", "行业 ETF 仅作代理（待代码级核验）", "USD/CNY"),
    "纳斯达克科技": ("纳斯达克科技成分", "QQQ/科技行业 ETF 仅作代理（待代码级核验）", "USD/CNY"),
    "标普消费": ("美国消费成分", "消费行业 ETF 仅作代理（待代码级核验）", "USD/CNY"),
    "恒生生物科技": ("恒生生物科技港股成分", "逐只港股或行业 ETF（待代码级核验）", "HKD/CNY"),
    "港股通科技": ("港股科技成分", "逐只港股或行业 ETF（待代码级核验）", "HKD/CNY"),
    "港股通互联网": ("港股互联网成分", "逐只港股或行业 ETF（待代码级核验）", "HKD/CNY"),
    "香港金融": ("香港金融/证券成分", "逐只港股或行业 ETF（待代码级核验）", "HKD/CNY"),
    "港股通消费": ("港股消费成分", "逐只港股或行业 ETF（待代码级核验）", "HKD/CNY"),
    "法国CAC40": ("法国 CAC40 成分", "CAC40 期货/ETF（待代码级核验）", "EUR/CNY"),
    "全球中国教育": ("全球中国教育主题成分", "逐只成分/代理待代码级核验", "多币种"),
    "道琼斯工业": ("道琼斯工业平均成分", "DIA 或 YM（代理，须滚动 beta）", "USD/CNY"),
    "港股通综合": ("广泛港股通成分", "不建议以单一 ETF 作总篮子估值", "HKD/CNY"),
}

KEYWORDS = {
    "现金替代": ("现金替代", "替代金额"),
    "现金差额": ("现金差额", "预估现金"),
    "汇率": ("人民币汇率中间价", "汇率公允价", "中国外汇交易中心"),
    "清算交收": ("T+2", "T+3", "T+6", "10 个工作日", "第 10 个"),
}


@dataclass(frozen=True)
class Fund:
    code: str
    exchange: str
    name: str
    family: str


def request_bytes(url: str, *, referer: str = "") -> bytes:
    headers = {"User-Agent": "Mozilla/5.0"}
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=45) as response:
        return response.read()


def clean_filename(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", text).strip()


def current_prospectus(code: str) -> dict[str, str]:
    url = f"{EASTMONEY_API}?fundcode={code}&pageIndex=1&pageSize=100&type=1"
    payload = json.loads(request_bytes(url, referer=f"https://fundf10.eastmoney.com/jjgg_{code}_1.html"))
    candidates = [item for item in payload.get("Data") or [] if "招募说明书" in str(item.get("TITLE") or "")]
    direct = [
        item for item in candidates
        if "公告" not in str(item.get("TITLE") or "") and "关于" not in str(item.get("TITLE") or "")
    ]
    chosen = max(direct or candidates, key=lambda item: str(item.get("PUBLISHDATE") or ""))
    announcement_id = str(chosen.get("ID") or "")
    return {
        "title": str(chosen.get("TITLE") or ""),
        "date": str(chosen.get("PUBLISHDATEDesc") or ""),
        "id": announcement_id,
        "pdf_url": PDF_URL.format(announcement_id=announcement_id),
        "announcement_url": f"https://fundf10.eastmoney.com/jjgg_{code}.html",
    }


def pcf_path(fund: Fund) -> Path:
    return PCF_ROOT / ("xml" if fund.exchange == "SZSE" else "sse/xml") / f"{fund.code}.xml"


def pcf_summary(fund: Fund) -> dict[str, object]:
    root = ET.parse(pcf_path(fund)).getroot()
    ns = {"x": "http://ts.szse.cn/Fund"} if root.tag.endswith("PCFFile") else {}

    def value(field: str) -> str:
        node = root.find(f"x:{field}", ns) if ns else root.find(field)
        return (node.text or "").strip() if node is not None else ""

    components = root.findall(".//x:Component", ns) if ns else root.findall(".//Component")
    component_codes: list[str] = []
    flags: list[str] = []
    markets: list[str] = []
    for component in components:
        if ns:
            code = (component.findtext("x:UnderlyingSecurityID", default="", namespaces=ns) or "").strip()
            flag = (component.findtext("x:SubstituteFlag", default="", namespaces=ns) or "").strip()
            market = (component.findtext("x:UnderlyingSecurityIDSource", default="", namespaces=ns) or "").strip()
        else:
            code = (component.findtext("InstrumentID", default="") or "").strip()
            flag = (component.findtext("SubstitutionFlag", default="") or "").strip()
            market = (component.findtext("UnderlyingSecurityID", default="") or "").strip()
        if code and code != "159900":
            component_codes.append(code)
        flags.append(flag)
        markets.append(market)
    return {
        "trading_day": value("TradingDay"),
        "pre_trading_day": value("PreTradingDay"),
        "underlying": value("UnderlyingSecurityID"),
        "unit": value("CreationRedemptionUnit"),
        "nav_per_cu": value("NAVperCU"),
        "estimated_cash": value("EstimateCashComponent") or value("EstimatedCashComponent"),
        "actual_cash": value("CashComponent") or value("PreCashComponent"),
        "switch": value("CreationRedemptionSwitch") or f"申购={value('Creation')}; 赎回={value('Redemption')}",
        "mechanism": value("CreationRedemptionMechanism"),
        "components": len(component_codes),
        "flags": ", ".join(f"{item}:{flags.count(item)}" for item in sorted(set(flags))),
        "markets": ", ".join(sorted(set(markets))),
    }


def keyword_pages(pdf_path: Path) -> dict[str, list[int]]:
    reader = PdfReader(str(pdf_path))
    found = {label: [] for label in KEYWORDS}
    for page_no, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        for label, terms in KEYWORDS.items():
            if any(term in text for term in terms):
                found[label].append(page_no)
    return found


def prospectus_file(fund: Fund, meta: dict[str, str]) -> Path:
    folder = ROOT / fund.family
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{fund.code}_{fund.name}_更新招募说明书_{meta['date']}.pdf"


def download_pdf(fund: Fund, meta: dict[str, str], *, refresh: bool) -> Path:
    destination = prospectus_file(fund, meta)
    if destination.exists() and not refresh:
        return destination
    destination.write_bytes(request_bytes(meta["pdf_url"], referer=meta["announcement_url"]))
    if not destination.read_bytes()[:4] == b"%PDF":
        destination.unlink(missing_ok=True)
        raise ValueError(f"{fund.code} 招募说明书下载结果不是 PDF")
    return destination


def family_baseline_markdown(family: str, funds: Iterable[Fund]) -> str:
    securities, proxy, fx = FAMILY_BASELINES[family]
    codes = "、".join(fund.code for fund in funds)
    return f"""# {family} - 预扫描共同估值基线

适用代码：{codes}

## 共用证券价格腿

```text
证券资产_live(t) = Σ(PCF 数量 × 成分证券可执行价格 × 对应实时人民币汇率)
总篮子资产_live(t) = 证券资产_live(t) + T 日预估现金部分
单位实时估值(t) = 总篮子资产_live(t) ÷ PCF 最小申赎单位
```

- 证券市场：{securities}
- 对冲/价格代理候选：{proxy}
- 人民币换算：{fx}

## 强制分离的代码级字段

本基线不规定现金替代卖出时点、最终汇率、现金差额清算、现金替代到账时间、申赎限额或固定对冲数量。这些字段必须由每只基金自己的招募说明书、当日 PCF 和实际到账记录确定。

## 报价规则

- 买入境内 ETF 用实际 Ask；卖空代理用实际 Bid；买回代理用实际 Ask。
- 夜盘/盘前稀疏报价必须记录报价年龄和可成交数量，禁止用 Last 代替可执行价。
- 实时屏幕只使用当时已发布的汇率；事后才用已发布的 T 日 CFETS 收盘价复核，不得产生未来函数。
"""


def code_markdown(fund: Fund, pcf: dict[str, object], meta: dict[str, str], pdf_name: str, pages: dict[str, list[int]]) -> str:
    securities, proxy, fx = FAMILY_BASELINES[fund.family]
    page_text = "；".join(f"{label}：{', '.join(map(str, values)) or '未自动命中'}" for label, values in pages.items())
    return f"""# {fund.code} {fund.name} - 预扫描估值方案

> 状态：**预扫描版，禁止直接用于实盘赎回。** 已归档最新 PCF 与招募说明书，并完成证券价格腿和分类；现金替代时点、最终汇率、现金差额到账仍需人工阅读原文和实际流水核验。

## 自动化配置摘要

```yaml
fund_code: "{fund.code}"
fund_name: "{fund.name}"
family: "{fund.family}"
exchange: "{fund.exchange}"
pcf_trading_day: "{pcf['trading_day']}"
pcf_unit_shares: {pcf['unit']}
pcf_underlying_index: "{pcf['underlying']}"
pcf_component_count: {pcf['components']}
primary_live_valuation: "PCF逐只证券可执行价 + 当日预估现金部分"
final_redemption_valuation: "实际卖出/合同未卖部分定值 + 同一T日实际现金差额"
live_fx: "当时已发布的对应人民币汇率"
settlement_fx: "待代码级招募书条款与实际到账核验"
cash_substitute_timing: "待代码级招募书条款与实际到账核验"
hedge_proxy_candidate: "{proxy}"
status: "pre_scan_not_trade_ready"
```

## 当日 PCF 配置（{pcf['trading_day']}）

| 字段 | 数值 |
| --- | ---: |
| 交易所 | {fund.exchange} |
| 标的指数代码 | `{pcf['underlying'] or 'PCF 未披露'}` |
| `PreTradingDay` | {pcf['pre_trading_day']} |
| 最小申赎单位 | {pcf['unit']} 份 |
| 成分证券数 | {pcf['components']} |
| 预估现金部分 | {pcf['estimated_cash']} CNY |
| 历史现金字段 | {pcf['actual_cash']} CNY |
| 申赎开关 | {pcf['switch']} |
| 上交所申赎机制 | {pcf['mechanism'] or '深交所 PCF 未使用该字段'} |
| 现金替代标志分布 | {pcf['flags']} |
| 成分市场编码 | {pcf['markets']} |

`历史现金字段`不等于当前 T 日实际现金差额。只有后续 PCF 明确将其 `PreTradingDay`（或等价字段）匹配到本次赎回 T 日，才能替换当日预估现金部分。

## 共用估值公式

```text
证券资产_live(t) = Σ(PCF 数量_i × 成分证券可执行价_i(t) × 对应实时人民币汇率_i(t))
总篮子资产_live(t) = 证券资产_live(t) + EstimateCashComponent_T
单位实时估值(t) = 总篮子资产_live(t) ÷ 最小申赎单位份额
```

- 证券市场：{securities}
- 对冲代理候选：{proxy}
- 汇率篮子：{fx}
- 共同基线：[查看 {fund.family} 基线](估值基线_预扫描.md)

## 招募说明书原文待核验项目

| 项目 | 自动定位结果 | 纳入实盘前的要求 |
| --- | --- | --- |
| 现金替代 | {pages['现金替代'] or '未命中'} 页 | 确认 T 日是否由管理人代买/代卖、未成交如何定值 |
| 现金差额 | {pages['现金差额'] or '未命中'} 页 | 确认实际现金差额公告、清算与交收日期 |
| 汇率 | {pages['汇率'] or '未命中'} 页 | 确认 PCF 估值汇率与赎回结算汇率是否不同 |
| 清算交收 | {pages['清算交收'] or '未命中'} 页 | 确认现金替代款发送、清算和实际到账时点 |

本预扫描版只记录关键词所在页，不能把同族其他基金的 T+6、T+10、CFETS 收盘价或固定对冲比例移植到本基金。

## 原始输入

- 招募说明书：[{meta['title']}]({pdf_name})，公告日 {meta['date']}，公告 ID `{meta['id']}`。
- 招募书公告页：[{meta['announcement_url']}]({meta['announcement_url']})。
- 招募书原始 PDF：[{meta['pdf_url']}]({meta['pdf_url']})。
- PCF：`{fund.code}_{fund.name}_PCF_{pcf['trading_day'][:4]}-{pcf['trading_day'][4:6]}-{pcf['trading_day'][6:]}.xml`。
"""


def all_funds() -> list[Fund]:
    funds = [
        Fund(code, "SZSE", DISPLAY_NAME_OVERRIDES.get(code, name), FAMILY_BY_CODE[code])
        for code, name in FOCUS_FUND_DISPLAY_NAMES.items()
    ]
    funds += [
        Fund(code, "SSE", DISPLAY_NAME_OVERRIDES.get(code, name), FAMILY_BY_CODE[code])
        for code, name in SSE_FUND_DISPLAY_NAMES.items()
    ]
    missing = [fund.code for fund in funds if fund.code not in FAMILY_BY_CODE]
    if missing:
        raise ValueError(f"未分类代码：{missing}")
    return funds


def main() -> int:
    parser = argparse.ArgumentParser(description="批量生成维护列表的预扫描估值方案")
    parser.add_argument("--refresh-pdf", action="store_true", help="重新下载已存在的招募说明书")
    parser.add_argument("--overwrite-md", action="store_true", help="覆盖已有代码级 Markdown（默认保护已人工完成方案）")
    parser.add_argument("--codes", default="", help="仅处理逗号分隔的基金代码；用于可续跑批次")
    args = parser.parse_args()

    catalog = all_funds()
    selected_codes = {code.strip() for code in args.codes.split(",") if code.strip()}
    funds = [fund for fund in catalog if not selected_codes or fund.code in selected_codes]
    unknown_codes = selected_codes - {fund.code for fund in catalog}
    if unknown_codes:
        raise SystemExit(f"维护列表中不存在代码：{', '.join(sorted(unknown_codes))}")
    missing_pcf = [fund.code for fund in funds if not pcf_path(fund).is_file()]
    if missing_pcf:
        raise SystemExit(f"缺少预扫描 PCF：{', '.join(missing_pcf)}")

    metadata: dict[str, dict[str, str]] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {pool.submit(current_prospectus, fund.code): fund for fund in funds}
        for job in as_completed(jobs):
            fund = jobs[job]
            metadata[fund.code] = job.result()

    registry: list[dict[str, object]] = []
    created = 0
    for fund in funds:
        print(f"处理 {fund.exchange}{fund.code} {fund.name}", flush=True)
        pcf = pcf_summary(fund)
        meta = metadata[fund.code]
        pdf = download_pdf(fund, meta, refresh=args.refresh_pdf)
        pages = keyword_pages(pdf)
        folder = ROOT / fund.family
        pcf_target = folder / f"{fund.code}_{fund.name}_PCF_{pcf['trading_day'][:4]}-{pcf['trading_day'][4:6]}-{pcf['trading_day'][6:]}.xml"
        if not pcf_target.exists():
            pcf_target.write_bytes(pcf_path(fund).read_bytes())
        md_path = folder / f"{fund.code}_{fund.name}_估值方案.md"
        if args.overwrite_md or not md_path.exists():
            md_path.write_text(code_markdown(fund, pcf, meta, pdf.name, pages), encoding="utf-8")
            created += 1
        registry.append({"code": fund.code, "exchange": fund.exchange, "name": fund.name, "family": fund.family, "pcf": pcf, "prospectus": meta, "keyword_pages": pages, "md": str(md_path.relative_to(ROOT))})

    for family in sorted({fund.family for fund in catalog}):
        folder = ROOT / family
        baseline = folder / "估值基线_预扫描.md"
        baseline.write_text(family_baseline_markdown(family, [fund for fund in catalog if fund.family == family]), encoding="utf-8")
    registry_path = ROOT / "预扫描数据" / "2026-07-10" / "估值方案来源索引.json"
    existing = []
    if registry_path.exists():
        try:
            existing = json.loads(registry_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = []
    merged = {str(item.get("code")): item for item in existing if isinstance(item, dict)}
    merged.update({str(item["code"]): item for item in registry})
    registry_path.write_text(
        json.dumps([merged[code] for code in sorted(merged)], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"完成：{len(funds)} 只基金；新建 Markdown：{created}；预扫描版不可直接用于实盘。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
