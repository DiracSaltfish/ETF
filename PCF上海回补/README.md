# 上海 PCF 回补

上交所公开下载接口只提供最新 PCF，不能按历史日期查询。本目录按基金公司官网的历史接口回补，并输出与赎回收益计算器兼容的 SSE XML：

`回补文件/YYYY-MM-DD/sse/xml/<基金代码>.xml`

每个文件在写入前都会校验 `TradingDay` 必须等于目录日期。字段可部分缺失；缺失项会记录为 `BackfillMissingFields`，并且不会用其他日期或估算值填充。

已验证的适配器：

- 易方达：`513050`、`513090`、`513000`、`513850`，官网按日完整 PCF 和成分股。
- 华夏：`513230`、`513520`、`513300`，官网按日完整 PCF 和成分股。
- 富国：`513350`、`513870`，官网按日历史净值，仅写入基金代码、名称、管理人、交易日和 NAV。

招商、国泰、广发、华安、华泰柏瑞、博时、南方、鹏华、汇添富的目录与基金公司链接已建立，但尚未验证其历史接口；默认会跳过它们，不会写入不可信的缓存。

当只需要让程序读取基金代码、名称、交易日和 NAV，且接受非完整 PCF 时，可显式开启公开历史净值回退。此类 XML 会写入 `BackfillDataGrade=公开历史净值映射（非基金公司 PCF）` 和 `NAVSourceDay`，不会包含现金差额、最小申赎单位或成分股：

```bash
conda run -n ag python backfill_sh_pcf.py --end 2026-07-10 --include-public-nav-fallback
```

脚本内置上交所 2026 年休市日，回补时不会把调休工作日或节假日写为 PCF 交易日。若旧运行曾生成休市日文件，可安全清理本工具生成的文件：

```bash
conda run -n ag python backfill_sh_pcf.py --purge-closed-days
```

运行三个月回补：

```bash
cd /Users/ellis/Desktop/ETF交割/PCF上海回补
conda run -n ag python backfill_sh_pcf.py --end 2026-07-10
```

确认 `报告/` 与 `回补文件/` 后，使用 `--install` 才会复制进程序实际缓存：

```bash
conda run -n ag python backfill_sh_pcf.py --end 2026-07-10 --install
```

原始响应按日期、基金公司、代码保存到 `原始响应/`，用于审计和后续补充适配器。
